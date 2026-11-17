"""依體檢自動換池（ADR 0035 第 6–9 點，方案 A 修訂 ADR 0001／0007）。

排行榜仍做初步分群；體檢分數（附信賴區間）有統計上顯著、且超過遲滯門檻的差距時，
才覆寫分群。覆寫一律產生新的評分快照，記錄原分群、覆寫後分群與依據；
體檢分數永遠不與排行榜分數相加或平均。

本模組不連網、不呼叫模型：體檢紀錄來自 checkup.run_checkup（#77），
費用上界（model_bounds）與 BudgetedCore 使用的是同一份對照表。
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path

from .catalog import TIERS, digest, timestamp, verify_snapshot
from .checkup import VERSION as CHECKUP_VERSION
from .snapshots import SnapshotStore, _locked

OVERRIDE_SCHEMA = 'cloudyfan-auto-pool-v1'
POLICY_SCHEMA = 'auto-pool-policy-v1'
LOG_SCHEMA = 'cloudyfan-auto-pool-log-v1'


def _policy(policy):
    if not isinstance(policy, dict) or policy.get('schema_version') != POLICY_SCHEMA:
        raise ValueError('Explicit auto-pool policy required')
    h = policy.get('hysteresis')
    if type(h) not in (int, float) or not 0 <= h < 1:
        raise ValueError('Hysteresis must be a number in [0, 1)')
    for key in ('min_graded', 'max_checkup_age_days'):
        if type(policy.get(key)) is not int or policy[key] < 1:
            raise ValueError(key + ' must be a positive integer')


def _valid_record(record):
    try:
        body = {k: v for k, v in record.items() if k != 'record_id'}
        return (record.get('schema_version') == CHECKUP_VERSION and record.get('record_id') == digest(body)
                and isinstance(record.get('configuration_id'), str))
    except (AttributeError, TypeError, ValueError):
        return False


def _usable(record, policy, now):
    """體檢紀錄能否作為換池依據：完整、題數足夠、有信賴區間、未過期也不在未來。"""
    ci = record.get('confidence_interval') or {}
    created = timestamp(record['created_at'])
    return (type(record.get('graded')) is int and record['graded'] >= policy['min_graded']
            and type(ci.get('low')) in (int, float) and type(ci.get('high')) in (int, float)
            and created <= now and now - created < timedelta(days=policy['max_checkup_age_days']))


def _cost(value):
    try:
        amount = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        return None
    return amount if amount is not None and amount.is_finite() and amount > 0 else None


def _evidence(record):
    """換池依據：分數、信賴區間、完整任務成本與能力價格比（分數／每題完整成本）。"""
    ci = record['confidence_interval']
    cost = _cost(record.get('cost_per_task_usd'))
    ratio = None if cost is None else {
        'low': str(Decimal(str(ci['low'])) / cost), 'high': str(Decimal(str(ci['high'])) / cost),
        'unit': 'score_per_usd'}
    return {'checkup_record_id': record['record_id'], 'checked_at': record['created_at'],
            'score': record.get('score'), 'graded': record['graded'],
            'confidence_interval': deepcopy(ci),
            'complete_task_cost_usd': record.get('complete_task_cost_usd'),
            'cost_per_task_usd': record.get('cost_per_task_usd'), 'capability_price_ratio': ratio}


def _history(records, policy, now):
    """依固定設定分組、依時間排序；壞紀錄直接忽略，不當作分數。"""
    by_config = {}
    for record in records:
        if _valid_record(record) and _usable(record, policy, now):
            by_config.setdefault(record['configuration_id'], []).append(record)
    for rows in by_config.values():
        rows.sort(key=lambda r: timestamp(r['created_at']))
    return by_config


def _grouping(pools):
    return {tier: sorted(e['config_id'] for e in pools[tier]) for tier in TIERS}


def plan_override(base, records, policy, *, now, model_bounds, unpause=()):
    """依體檢紀錄規劃下一份快照；不寫檔。回傳 status 為 'unchanged' 時 snapshot 為 None。

    - 只移動已在 base 快照池內的設定（ADR 0007 資格、固定版本與供應商身分已由快照把關）；
      被排除、未核准或不在快照內的設定，分數再高也不入池。
    - 每次最多跨一階：比上一階「所有」有體檢的設定都顯著較好（信賴區間不重疊且差距大於
      遲滯門檻）才升；比下一階所有有體檢的設定都顯著較差才降。
    - 被移動的設定必須有已確認的費用上界（model_bounds，以 profile.model 為鍵）。
    - 同一固定設定前後兩次體檢顯著下降 → 暫停入池；某池因此清空 → 停用並告警。
    """
    _policy(policy)
    instant = timestamp(now)
    verify_snapshot(base, now=now)
    previous = base.get('pool_override') or {}
    original = previous.get('original_grouping') or _grouping(base['pools'])
    paused = {row['config_id']: deepcopy(row) for row in previous.get('paused', [])}
    history = _history(records, policy, instant)
    latest = {cid: rows[-1] for cid, rows in history.items()}
    tier_of = {e['config_id']: tier for tier in TIERS for e in base['pools'][tier]}
    entries = {e['config_id']: e for tier in TIERS for e in base['pools'][tier]}
    h = policy['hysteresis']
    alerts, blocked, moves, newly_paused, resumed = [], [], [], [], []

    def ci(cid):
        return latest[cid]['confidence_interval']

    # 降智偵測：同一 configuration_id（固定版本）最近兩次體檢顯著下降。
    for cid in sorted(tier_of):
        rows = history.get(cid, [])
        if len(rows) >= 2:
            before, after = rows[-2]['confidence_interval'], rows[-1]['confidence_interval']
            if before['low'] - after['high'] > h:
                paused[cid] = {'config_id': cid, 'from': tier_of[cid], 'paused_at': instant.isoformat(),
                               'reason': 'significant_checkup_drop', 'entry': deepcopy(entries[cid]),
                               'evidence': {'before': _evidence(rows[-2]), 'after': _evidence(rows[-1])}}
                newly_paused.append(cid)
    for cid in sorted(set(unpause)):
        if cid not in paused:
            raise ValueError('Only a paused configuration can be resumed: ' + cid)
        resumed.append(paused.pop(cid))

    # 顯著性＋遲滯：比較時用 base 分群，結果與處理順序無關。
    checked = {tier: [cid for cid in tier_of if tier_of[cid] == tier and cid in latest and cid not in paused]
               for tier in TIERS}
    for index, tier in enumerate(TIERS):
        for cid in checked[tier]:
            target = None
            if index + 1 < len(TIERS) and checked[TIERS[index + 1]]:
                upper = TIERS[index + 1]
                if ci(cid)['low'] - max(ci(o)['high'] for o in checked[upper]) > h:
                    target = upper
            if index > 0 and checked[TIERS[index - 1]]:
                lower = TIERS[index - 1]
                if min(ci(o)['low'] for o in checked[lower]) - ci(cid)['high'] > h:
                    target = lower
            if target is None:
                continue
            reason = ('significant_checkup_gain' if TIERS.index(target) > index
                      else 'significant_checkup_loss')
            if entries[cid]['profile']['model'] not in model_bounds:
                blocked.append({'config_id': cid, 'from': tier, 'to': target, 'reason': 'fee_bound_unconfirmed'})
                continue
            moves.append({'config_id': cid, 'from': tier, 'to': target, 'reason': reason})

    target_tier = dict(tier_of)
    for move in moves:
        target_tier[move['config_id']] = move['to']
    for row in resumed:
        cid = row['config_id']
        # 回到排行榜原分群；快照內仍需有這筆設定（資格由原快照提供）。
        home = next((t for t in TIERS if cid in original[t]), row['from'])
        target_tier[cid] = home
        entries[cid] = row['entry']
    pools = {tier: [] for tier in TIERS}
    for cid, tier in target_tier.items():
        if cid not in paused:
            pools[tier].append(deepcopy(entries[cid]))
    for pool in pools.values():
        pool.sort(key=lambda e: e['config_id'])
    disabled = [tier for tier in TIERS if not pools[tier]]
    for tier in disabled:
        if tier not in previous.get('disabled_tiers', []):
            alerts.append({'kind': 'tier_disabled', 'tier': tier,
                           'reason': 'no_qualified_candidates_after_pause',
                           'fallback_to_other_tier': False})
    for cid in newly_paused:
        alerts.append({'kind': 'configuration_paused', 'config_id': cid, 'reason': 'significant_checkup_drop'})

    if not moves and not newly_paused and not resumed:
        return {'status': 'unchanged', 'snapshot': None, 'moves': [], 'blocked': blocked, 'alerts': alerts}
    evidence = {cid: _evidence(latest[cid]) for cid in sorted(latest) if cid in tier_of}
    override = {'schema_version': OVERRIDE_SCHEMA, 'kind': 'checkup_override',
                'base_snapshot_id': base['snapshot_id'], 'original_grouping': deepcopy(original),
                'overridden_grouping': _grouping(pools), 'moves': moves, 'blocked': blocked,
                'paused': [paused[c] for c in sorted(paused)], 'resumed': [r['config_id'] for r in resumed],
                'disabled_tiers': disabled, 'evidence': evidence, 'policy': deepcopy(policy),
                # 方案 A：體檢只決定是否覆寫分群，從不與排行榜分數合成一個數字。
                'scores_combined': False}
    snapshot = _derive(base, pools, override, instant)
    return {'status': 'changed', 'snapshot': snapshot, 'moves': moves, 'blocked': blocked, 'alerts': alerts}


def _derive(base, pools, override, instant):
    body = {k: deepcopy(v) for k, v in base.items() if k not in ('snapshot_id', 'pool_override')}
    body.update(created_at=instant.isoformat(), pools=pools, pool_override=override)
    snapshot = {**body, 'snapshot_id': digest(body)}
    verify_snapshot(snapshot, now=instant.isoformat())
    return snapshot


class AutoPoolController:
    """一個領域的自動換池：快照庫、換池紀錄（JSON Lines）與總開關。

    root/snapshots 是既有 SnapshotStore；舊快照檔不可變，已綁定舊快照的任務不受影響。
    """
    def __init__(self, root):
        self.root = Path(root)
        self.store = SnapshotStore(self.root / 'snapshots')
        self._log = self.root / 'auto-pool-log.jsonl'
        self._settings = self.root / 'auto-pool-settings.json'

    # ---- 總開關 ----
    def auto_switch_enabled(self):
        if not self._settings.exists():
            return True  # ADR 0035：使用者選擇自動換池；人工保留回復與關閉權。
        value = json.loads(self._settings.read_text()).get('auto_switch_enabled')
        if type(value) is not bool:
            raise ValueError('Corrupt auto-pool settings')
        return value

    def set_auto_switch(self, enabled, *, now, operator):
        if type(enabled) is not bool or not isinstance(operator, str) or not operator.strip():
            raise ValueError('Explicit switch value and operator required')
        with _locked(self.root):
            staged = self.root / 'auto-pool-settings.tmp'
            staged.write_text(json.dumps({'auto_switch_enabled': enabled}))
            os.replace(staged, self._settings)
            self._append({'kind': 'auto_switch', 'at': timestamp(now).isoformat(),
                          'operator': operator, 'enabled': enabled})

    # ---- 換池紀錄 ----
    def changes(self):
        if not self._log.exists():
            return []
        return [json.loads(line) for line in self._log.read_text().splitlines() if line.strip()]

    def _append(self, row):
        line = json.dumps({'schema_version': LOG_SCHEMA, **row}, ensure_ascii=False, sort_keys=True)
        with self._log.open('a') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(line + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    # ---- 自動換池 ----
    def evaluate(self, records, policy, *, now, model_bounds, unpause=()):
        base = self.store.load()
        plan = plan_override(base, records, policy, now=now, model_bounds=model_bounds, unpause=unpause)
        at = timestamp(now).isoformat()
        if plan['status'] == 'unchanged':
            if plan['blocked'] or plan['alerts']:
                with _locked(self.root):
                    self._append({'kind': 'no_change', 'at': at, 'snapshot_id': base['snapshot_id'],
                                  'blocked': plan['blocked'], 'alerts': plan['alerts']})
            return {'status': 'unchanged', 'snapshot_id': base['snapshot_id'],
                    'blocked': plan['blocked'], 'alerts': plan['alerts']}
        if not self.auto_switch_enabled():
            # 總開關關閉：不發布新快照，只把「本來會怎麼換」留下紀錄，供操作員判斷。
            with _locked(self.root):
                self._append({'kind': 'suppressed', 'at': at, 'snapshot_id': base['snapshot_id'],
                              'moves': plan['moves'], 'blocked': plan['blocked'], 'alerts': plan['alerts']})
            return {'status': 'auto_switch_disabled', 'snapshot_id': base['snapshot_id'],
                    'moves': plan['moves'], 'alerts': plan['alerts']}
        snapshot = plan['snapshot']
        self.store.publish(snapshot, now=at, expected_current_id=base['snapshot_id'])
        with _locked(self.root):
            self._append({'kind': 'auto_change', 'at': at, 'before_snapshot_id': base['snapshot_id'],
                          'after_snapshot_id': snapshot['snapshot_id'], 'moves': plan['moves'],
                          'blocked': plan['blocked'], 'alerts': plan['alerts'],
                          'paused': [r['config_id'] for r in snapshot['pool_override']['paused']],
                          'disabled_tiers': snapshot['pool_override']['disabled_tiers'],
                          'evidence': snapshot['pool_override']['evidence']})
        return {'status': 'changed', 'before_snapshot_id': base['snapshot_id'],
                'after_snapshot_id': snapshot['snapshot_id'], 'moves': plan['moves'],
                'blocked': plan['blocked'], 'alerts': plan['alerts']}

    def rollback(self, *, now, operator):
        """一鍵回復上一份快照：以上一份的分群發布一份新快照（快照庫只前進、不倒退）。"""
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError('Operator required')
        current = self.store.load()
        changes = [row for row in self.changes() if row.get('after_snapshot_id') == current['snapshot_id']]
        if not changes:
            raise ValueError('No recorded change to roll back')
        previous = self.store.load(changes[-1]['before_snapshot_id'])
        instant = timestamp(now)
        override = {'schema_version': OVERRIDE_SCHEMA, 'kind': 'rollback',
                    'base_snapshot_id': current['snapshot_id'],
                    'restored_snapshot_id': previous['snapshot_id'], 'operator': operator,
                    'original_grouping': deepcopy((previous.get('pool_override') or {}).get(
                        'original_grouping') or _grouping(previous['pools'])),
                    'overridden_grouping': _grouping(previous['pools']),
                    'paused': deepcopy((previous.get('pool_override') or {}).get('paused', [])),
                    'disabled_tiers': [t for t in TIERS if not previous['pools'][t]],
                    'scores_combined': False}
        # 回復的快照仍受原快照有效期限制；過期就不能回復，只能重新編譯排行榜快照。
        snapshot = _derive(previous, deepcopy(previous['pools']), override, instant)
        self.store.publish(snapshot, now=instant.isoformat(), expected_current_id=current['snapshot_id'])
        with _locked(self.root):
            self._append({'kind': 'rollback', 'at': instant.isoformat(), 'operator': operator,
                          'before_snapshot_id': current['snapshot_id'],
                          'after_snapshot_id': snapshot['snapshot_id'],
                          'restored_snapshot_id': previous['snapshot_id']})
        return {'status': 'rolled_back', 'before_snapshot_id': current['snapshot_id'],
                'after_snapshot_id': snapshot['snapshot_id'], 'restored_snapshot_id': previous['snapshot_id']}
