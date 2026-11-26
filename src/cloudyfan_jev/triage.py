"""顧問先分流模式（``advisor_triage``；ADR 0038 第 1～7 點）。

明示選用、預設關閉、有版本的第三種路由模式。每題先由分流顧問（Haiku 5.5 xhigh，
第 3 版分流提示）判斷「能驗收」走路徑一、「開放題」走路徑二，判斷與使用者覆寫一起凍結；
之後這題只走那一條路徑，不中途切換、不融合（延續 ADR 0034 第 1、5 點）。

- 路徑一：交給 Core 既有的 ``cloudyfan``／``cloudyfan_plus`` 流程（Jev 升級，規則一字不改）。
- 路徑二：池內單一設定直接作答（預設 Sonnet 5.5 xhigh，高風險 Fable 5.2 high）。
- 分流回覆格式錯就用同一提示重問一次；仍錯就走路徑二。
- 環境回報這題建不出驗收器時，路徑一改走路徑二，並照實記為 ``no_verifier``。

使用者覆寫（``overrides``）只在建立時凍結；不帶覆寫時，任何既有模式的行為都不變。
本模組不呼叫 Core 以外的任何傳輸；所有付費呼叫都經過 Core 的收據與預算預留。
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import re
from time import perf_counter

ADVISOR_TRIAGE = 'advisor_triage'
VERSION = 'advisor-triage-v1'
PROMPT_VERSION = 'route-system-v3'
# 第 3 版（2026-11-26，cb97090）：有預期結果就走路徑一，測試本來就不完整、漏的交給審查；
# 沒有預期結果或主要靠人判斷才走路徑二。72 題中有回分類的都分對（plans/…-11-24 §11）。
ROUTE_SYSTEM_V3 = (
    'You route incoming tasks in a model-routing system. Each task arrives alone. Decide which path it takes.\n'
    '- "path1": the task asks for a concrete deliverable such as code, a query or an exact output, AND it states '
    'expected results the answer can be checked against, such as expected outputs, assertions or an exact target '
    'value. Inputs listed without their expected results do not count as checks. Tests are never complete: do not '
    'choose path2 just because the given checks miss edge cases or rules documented elsewhere in the code; a '
    'later review step handles those.\n'
    '- "path2": the task states no expected results, or its main point is something people have to judge, such as '
    'analysis, a diagnosis, a design, a recommendation, ranking quality or style, even if a few checks are given.\n'
    'Reply with only a JSON object: {"route": "path1" or "path2", "reason": "one short sentence"}.')
ROUTES = ('path1', 'path2')
# 一次分流加一次重問（ADR 0038 第 3 點）；兩次都計入預算預留（第 6 點）。
TRIAGE_CALLS = 2
PATH1_MODES = ('cloudyfan', 'cloudyfan_plus')
PATH1_FIRST_TIER = 'open_source'
# 開放題輸出上限至少 32,000 token（ADR 0038 第 5 點）。
MIN_PATH2_MAX_TOKENS = 32000
SETTING_PREFIX = 'triage:'
POOL_TIERS = ('low', 'mid', 'high')
MAX_RAW_EXCERPT = 300

OVERRIDE_FIELDS = ('risk', 'route', 'setting', 'escalation', 'max_usd')
OVERRIDE_CHOICES = {'risk': ('normal', 'high'), 'route': ROUTES, 'escalation': ('auto', 'off')}
_MONEY = re.compile(r'(0|[1-9][0-9]{0,5})(\.[0-9]{1,6})?')


class OverrideError(ValueError):
    """覆寫不合規；``code`` 是回給呼叫端的明確錯誤碼，``status`` 是 HTTP 狀態。"""
    STATUS = {'override_not_allowed': 403, 'max_usd_below_worst_case': 409}

    def __init__(self, code, field=None):
        self.code, self.field = code, field
        self.status = self.STATUS.get(code, 422)
        super().__init__(code if field is None else code + ': ' + field)


def parse_route(text):
    """分流顧問回覆：取第一個 JSON 物件，``route`` 必須是 path1／path2；否則回 None。
    與 2026-11-26 測驗（``pilot.route_classify``）用同一個解析，量到的準確率才適用。"""
    match = re.search(r'\{.*\}', text or '', re.S)
    try:
        value = json.loads(match.group(0)) if match else None
    except ValueError:
        value = None
    if not isinstance(value, dict) or value.get('route') not in ROUTES:
        return None
    return {'route': value['route'], 'reason': str(value.get('reason', ''))}


@dataclass(frozen=True)
class TriageConfig:
    """分流顧問、路徑二的池內設定與路徑一所用的既有模式；建構時即驗證。

    ``pool`` 是路徑二與使用者「指定模型」可用的池內固定設定（設定識別 → 含 ``tier`` 的設定）；
    池外的設定一律拒絕（ADR 0034 第 2 點、ADR 0038 第 7 點）。
    """
    triager: dict
    pool: dict
    path2_default: str
    path2_high_risk: str
    path1_mode: str = 'cloudyfan'
    path2_max_tokens: int = MIN_PATH2_MAX_TOKENS

    def __post_init__(self):
        from .advisor import _check_identity, _check_role
        _check_role(self.triager, 'triager')
        object.__setattr__(self, 'triager', deepcopy(self.triager))
        if not isinstance(self.pool, dict) or not self.pool:
            raise ValueError('triage pool requires at least one setting')
        for key, profile in self.pool.items():
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', key):
                raise ValueError('triage pool setting identity must be a short identifier')
            _check_identity(profile, 'pool ' + key)
            if profile.get('tier') not in POOL_TIERS:
                raise ValueError('triage pool setting requires tier low, mid or high')
        object.__setattr__(self, 'pool', deepcopy(self.pool))
        for name in ('path2_default', 'path2_high_risk'):
            if getattr(self, name) not in self.pool:
                raise ValueError(name + ' must name a pool setting')
        if self.path1_mode not in PATH1_MODES:
            raise ValueError('path1_mode must be cloudyfan or cloudyfan_plus')
        if type(self.path2_max_tokens) is not int or self.path2_max_tokens < MIN_PATH2_MAX_TOKENS:
            raise ValueError('path2_max_tokens must be an integer of at least 32000')

    def descriptor(self):
        return {'version': VERSION, 'prompt': {'version': PROMPT_VERSION, 'system': ROUTE_SYSTEM_V3},
                'triage_calls': TRIAGE_CALLS, 'triager': deepcopy(self.triager),
                'pool': deepcopy(self.pool), 'path1_mode': self.path1_mode,
                'path2': {'default': self.path2_default, 'high_risk': self.path2_high_risk,
                          'max_tokens': self.path2_max_tokens}}


def normalize_overrides(raw, config, *, allowed=OVERRIDE_FIELDS, max_usd_ceiling=None):
    """逐項檢查覆寫；不認得、不允許、池外或超過上限的一律拒絕，不猜、不默默忽略。

    回傳內容與輸入相同的新 dict（不轉型），以便原請求照原樣凍結。
    """
    if not isinstance(raw, dict):
        raise OverrideError('overrides_not_object')
    if not raw:
        raise OverrideError('overrides_empty')
    for name in raw:
        if name not in OVERRIDE_FIELDS:
            raise OverrideError('unsupported_override_field', str(name))
    for name in raw:
        if name not in allowed:
            raise OverrideError('override_not_allowed', name)
    for name, choices in OVERRIDE_CHOICES.items():
        if name in raw and raw[name] not in choices:
            raise OverrideError('invalid_override_value', name)
    if 'setting' in raw:
        if not isinstance(raw['setting'], str):
            raise OverrideError('invalid_override_value', 'setting')
        if raw['setting'] not in config.pool:
            raise OverrideError('setting_not_in_pool', 'setting')
    if 'max_usd' in raw:
        value = raw['max_usd']
        if not isinstance(value, str) or not _MONEY.fullmatch(value) or Decimal(value) <= 0:
            raise OverrideError('invalid_override_value', 'max_usd')
        if max_usd_ceiling is not None and Decimal(value) > Decimal(max_usd_ceiling):
            raise OverrideError('max_usd_exceeds_contract', 'max_usd')
    # 指定模型就是固定只用這個設定作答，與「自動升級」互相矛盾。
    if 'setting' in raw and raw.get('escalation') == 'auto':
        raise OverrideError('override_conflict', 'escalation')
    return deepcopy(raw)


def effective_plan(config, triage_route, overrides, verifier_available):
    """由分流結果、覆寫與驗收器有無，決定這題實際走的路徑與作答方式（純函式）。"""
    o = overrides or {}
    risk, escalation = o.get('risk', 'normal'), o.get('escalation', 'auto')
    if 'route' in o:
        route, source = o['route'], 'override'
    elif triage_route is not None:
        route, source = triage_route, 'triage'
    else:
        route, source = 'path2', 'triage_fallback'
    if route == 'path1' and not verifier_available:
        route, source = 'path2', 'no_verifier'
    plan = {'route': route, 'route_source': source, 'risk': risk, 'escalation': escalation}
    if 'setting' in o:
        plan.update(executor='single', setting=o['setting'], setting_source='override')
    elif route == 'path2':
        plan.update(executor='single', setting_source='risk_' + risk,
                    setting=config.path2_high_risk if risk == 'high' else config.path2_default)
    elif escalation == 'off':
        plan.update(executor='single', setting=None, setting_source='path1_first_answer')
    else:
        plan.update(executor='ladder', setting=None, setting_source='path1_ladder')
    plan['tier'] = (None if plan['executor'] == 'ladder' else
                    PATH1_FIRST_TIER if plan['setting'] is None else SETTING_PREFIX + plan['setting'])
    return plan


def reachable_branches(config, overrides):
    """預算預留要涵蓋的分支：``('single', tier)`` 或 ``('ladder', mode)``。

    路徑二永遠可到達（分流失敗、建不出驗收器都會轉過去），除非使用者已指定模型。
    """
    o = overrides or {}
    if 'setting' in o:
        return [('single', SETTING_PREFIX + o['setting'])]
    path2 = config.path2_high_risk if o.get('risk') == 'high' else config.path2_default
    branches = [('single', SETTING_PREFIX + path2)]
    if o.get('route') != 'path2':
        branches.append(('single', PATH1_FIRST_TIER) if o.get('escalation') == 'off'
                        else ('ladder', config.path1_mode))
    return branches


def triage_messages(task):
    from . import routing
    routing._reject_private(task)
    return [{'role': 'system', 'content': ROUTE_SYSTEM_V3}, {'role': 'user', 'content': task['prompt']}]


def _triage(core, prefix, task):
    """呼叫分流顧問；格式錯（含供應商拒收）重問一次，仍錯就回 ``route=None``（由呼叫端走路徑二）。
    同 call_id 續跑讀舊收據，不重付。"""
    from . import client as transport
    profile = core.advisor_triage.triager
    if core.execution_policy is not None:
        from .privacy import PolicyDenied
        location = profile.get('execution_location')
        core.execution_policy.require('generation', location)
        if not isinstance(location, str) or not location.startswith('cloud:'):
            raise PolicyDenied('generation', 'backend_location_mismatch')
    messages = triage_messages(task)
    calls, replies = [], []
    for attempt in range(1, TRIAGE_CALLS + 1):
        cid = prefix + '-triage' + ('' if attempt == 1 else f'-{attempt}')
        payload = core.generation_payload(messages, profile, prefix, None)
        record, info = core._checked(cid, transport.CHAT, payload, profile['accepted_models'],
                                     profile['provider_name'], allow_rejection=True)
        calls.append({'tier': 'triage', **info})
        if info.get('provider_rejected'):
            replies.append({'call_id': cid, 'outcome': 'provider_rejected'})
            continue
        content = json.loads(record['raw'])['choices'][0]['message'].get('content')
        verdict = parse_route(content if isinstance(content, str) else None)
        if verdict is None:
            replies.append({'call_id': cid, 'outcome': 'invalid',
                            'raw_excerpt': (content if isinstance(content, str) else '')[:MAX_RAW_EXCERPT]})
            continue
        replies.append({'call_id': cid, 'outcome': 'classified', **verdict})
        return {'calls': calls, 'replies': replies, 'route': verdict['route'], 'reason': verdict['reason'],
                'retried': attempt > 1, 'fallback': False}
    return {'calls': calls, 'replies': replies, 'route': None, 'reason': None, 'retried': True, 'fallback': True}


def _single_engine(core, tier):
    """路徑二與指定模型用較高的輸出上限；路徑一首答照 Core 原設定。"""
    if not tier.startswith(SETTING_PREFIX):
        return core
    from copy import copy
    from dataclasses import replace
    engine = copy(core)
    engine.limits = replace(core.limits, max_tokens=max(core.advisor_triage.path2_max_tokens,
                                                        core.limits.max_tokens))
    return engine


def run(core, run_id, task, environment_factory, *, contract_version, overrides=None):
    from .engine import _encoded, _lock, _read, _save
    import hashlib
    config = core.advisor_triage
    if len(run_id) > 77:
        raise ValueError('advisor_triage run_id must leave room for the path1 sub-run suffix')
    if overrides is not None:
        # Core 再驗一次（不信任呼叫端）：池外設定、不認得的欄位一律拒絕。
        overrides = normalize_overrides(overrides, config)
    triage_messages(task)
    directory = Path(core.client.root) / 'runs' / run_id
    if directory.is_symlink():
        raise ValueError('Run directory cannot be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock(directory / '.lock'):
        if (directory / '.retired.json').exists() or (directory / '.retired.json').is_symlink():
            raise ValueError('Retired run cannot be read or dispatched again')
        frozen = {**core.configuration_descriptor(), 'task': task, 'mode': ADVISOR_TRIAGE,
                  'contract_version': contract_version}
        if config.path1_mode == 'cloudyfan_plus':
            frozen['cloudyfan_plus'] = core.cloudyfan_plus.descriptor()
        if overrides is not None:
            frozen['overrides'] = overrides
        _save(directory / 'manifest.json', frozen)
        completed = directory / 'result.json'
        if completed.exists():
            return core._with_costs(_read(completed))
        prefix = 'run-' + run_id
        resumed = any((Path(core.client.root) / 'calls').glob(prefix + '-*'))
        began = perf_counter()
        env = environment_factory()
        check = getattr(env, 'verifier_available', None)
        if not callable(check):
            raise ValueError('advisor_triage requires the environment to declare verifier_available(task)')
        has_verifier = check(task)
        if type(has_verifier) is not bool:
            raise ValueError('verifier_available(task) must return a boolean')

        triage = _triage(core, prefix, task)
        plan = effective_plan(config, triage['route'], overrides, has_verifier)
        # 分流顧問原判斷與覆寫後的結果一起保存；這些紀錄只供校正提示，不自動改路由（ADR 0017）。
        _save(directory / 'triage-decision.json', {'triage': triage, 'plan': plan, 'overrides': overrides,
                                                   'verifier_available': has_verifier})
        if plan['executor'] == 'ladder':
            branch = {'kind': 'ladder', 'mode': config.path1_mode, 'run_id': run_id + '-p1'}
            sub = core.run(branch['run_id'], task, environment_factory, contract_version=contract_version,
                           mode=config.path1_mode)
            attempts, actions, selection, status = sub['attempts'], sub['actions'], sub['selection'], sub['status']
        else:
            branch = {'kind': 'single', 'tier': plan['tier']}
            replayable = getattr(env, 'replayable_tools', ())
            if (not isinstance(replayable, (tuple, list, set, frozenset))
                    or any(not isinstance(name, str) or not name for name in replayable)):
                raise ValueError('replayable_tools must explicitly name deterministic local tools')
            _save(directory / 'tool-replay-policy.json', {'replayable_tools': sorted(set(replayable))})
            messages = [{'role': 'system', 'content': core.system}, {'role': 'user', 'content': task['prompt']}]
            actions, selection = [], None
            attempt = _single_engine(core, plan['tier'])._attempt(env, messages, plan['tier'], prefix, 0,
                                                                   actions, directory)
            attempts = [attempt]
            passed = attempt['public']['passed']
            status = 'passed' if passed is True else 'failed' if passed is False else 'unverifiable'
        result = {'run_id': run_id, 'mode': ADVISOR_TRIAGE, 'triage': triage, 'plan': plan, 'branch': branch,
                  **({'overrides': overrides} if overrides is not None else {}),
                  'attempts': attempts, 'actions': actions, 'selection': selection,
                  'first_public_pass': attempts[0]['public']['passed'],
                  'final_public_pass': attempts[-1]['public']['passed'], 'status': status,
                  'resumed': resumed, 'wall_seconds': None if resumed else perf_counter() - began,
                  'config_sha256': hashlib.sha256(_encoded(frozen)).hexdigest()}
        _save(completed, result)
        return core._with_costs(result)


def routing_summary(result, profile_of):
    """每題一行回報的資料（ADR 0038 第 7 點）：路徑、模型、風險、分流原判斷與覆寫了哪些項目。"""
    plan, triage = result['plan'], result['triage']
    last = result['attempts'][-1]['tier'] if result['attempts'] else None
    profile = profile_of(last) if last is not None else None
    return {'version': VERSION, 'route': plan['route'], 'route_source': plan['route_source'],
            'triage_route': triage['route'], 'triage_reason': triage['reason'],
            'triage_retried': triage['retried'], 'triage_fallback': triage['fallback'],
            'risk': plan['risk'], 'escalation': plan['escalation'], 'setting': plan['setting'],
            'final_tier': last, 'model': None if profile is None else profile['model_version'],
            'effort': None if profile is None else profile['effort'],
            'overridden': sorted(result.get('overrides') or {})}
