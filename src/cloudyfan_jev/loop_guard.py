"""迴圈守衛裁判（ADR 0034 第 10 點第 2、4、5 點；#74）。

作答模型在同一次作答內反覆撞上同一個錯誤時：

1. 程式偵測：同一錯誤指紋（工具名＋錯誤 code＋category）出現 ≥ ``repeat_threshold`` 次，
   或工具回報的驗收失敗檢查碼與上次相同。運作性失敗（限流、無回應、逾時、環境故障）不算。
2. 觸發後呼叫開源裁判一次（每次作答上限 ``judge_calls``），輸入是 ≤ ``digest_limit_bytes``
   的公開摘要（先經 ``routing.project_public``），輸出嚴格 JSON
   ``{"stuck": bool, "confidence": 0..1, "redirect": str}``。
3. ``active`` 模式且 ``stuck`` 為真、把握度 ≥ ``confidence_threshold`` 才插一句換方向提示；
   插過後又撞上已重複的錯誤，就結束本次作答（``stop='loop_guard_stuck'``），交給呼叫端改派。
4. ``shadow`` 模式照樣偵測、呼叫裁判並記錄，但絕不插提示、不提前結束。

本模組不綁定路徑二：``guarded_attempt`` 只需要 Core 的 ``_attempt(..., turn_hook=...)``
掛鉤與一份裁判設定，路徑一加強版（#79）可直接沿用。
所有數值都是起始設定，不是驗證過的最佳值。
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
import json

from . import client as transport
from . import routing
from .privacy import PolicyDenied

VERSION = 'loop-guard-v1'
MODES = ('active', 'shadow')
STOP = 'loop_guard_stuck'
JUDGE_SYSTEM = (
    "You are a loop-guard judge for an AI agent working on a task with tools. You receive a public "
    "digest: what triggered the check, the deduplicated errors with counts, and the two most recent "
    "tool calls. Decide whether the agent is stuck repeating the same failing approach. Reply with "
    "exactly one JSON object and nothing else: {\"stuck\": true|false, \"confidence\": <number from "
    "0 to 1>, \"redirect\": <one short sentence telling the agent a different approach to try, or "
    "an empty string when not stuck>}. Do not solve the task yourself."
)
REDIRECT_PREFIX = 'Loop guard: you appear to be repeating the same failing step. '
# 運作性失敗：屬供應商或環境問題，不是作答模型的解題方向錯誤。
OPERATIONAL_CATEGORIES = frozenset({
    'operational', 'provider', 'rate_limit', 'rate_limited', 'timeout', 'unavailable',
    'engineering', 'transport', 'network', 'capacity'})
OPERATIONAL_CODES = frozenset({
    'rate_limited', 'provider_rate_limited', 'provider_unavailable', 'timeout',
    'unavailable', 'no_response', 'empty_response'})
MAX_REDIRECT_CHARS = 500
_VERDICT_KEYS = {'stuck', 'confidence', 'redirect'}
_STRING_CAPS = (2000, 500, 200, 64)


@dataclass(frozen=True)
class LoopGuardConfig:
    """迴圈守衛設定；凍結在 manifest。預設是影子模式（只記錄、不插提示）。"""
    mode: str = 'shadow'
    repeat_threshold: int = 2
    confidence_threshold: float = 0.7
    digest_limit_bytes: int = 8192

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError('loop_guard mode must be active or shadow')
        if type(self.repeat_threshold) is not int or self.repeat_threshold < 2:
            raise ValueError('repeat_threshold must be an integer of at least 2')
        if (type(self.confidence_threshold) not in (int, float)
                or not 0 < self.confidence_threshold <= 1):
            raise ValueError('confidence_threshold must be in (0, 1]')
        if type(self.digest_limit_bytes) is not int or not 1024 <= self.digest_limit_bytes <= 8192:
            raise ValueError('digest_limit_bytes must be from 1024 to 8192')

    def descriptor(self):
        return {'version': VERSION, 'judge_system': JUDGE_SYSTEM, 'redirect_prefix': REDIRECT_PREFIX,
                'operational_categories': sorted(OPERATIONAL_CATEGORIES),
                'operational_codes': sorted(OPERATIONAL_CODES), **asdict(self)}


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode()


def failure_signal(name, result):
    """從單一工具結果取出偵測訊號；回傳 ``(kind, key)`` 或 None。

    - ``tool_error``：``{"ok": false, "error": {"code", "category"}}``，key 是指紋 dict。
    - ``validation``：``{"passed": false, "checks": [...]}``，key 是排序去重後的檢查碼。
    運作性失敗回 None。
    """
    if not isinstance(result, dict):
        return None
    error = result.get('error')
    if result.get('ok') is False and isinstance(error, dict):
        code, category = error.get('code'), error.get('category')
        code = code if isinstance(code, (str, int)) and not isinstance(code, bool) else None
        category = category if isinstance(category, str) else None
        if (category in OPERATIONAL_CATEGORIES
                or (isinstance(code, str) and code.lower() in OPERATIONAL_CODES)):
            return None
        return 'tool_error', {'tool': name, 'code': code, 'category': category}
    checks = result.get('checks')
    if result.get('passed') is False and isinstance(checks, list) and checks and all(
            isinstance(c, str) for c in checks):
        return 'validation', {'tool': name, 'checks': sorted(set(checks))}
    return None


class Detector:
    """同一次作答內的錯誤指紋計數；``seed_checks`` 供路徑一加強版帶入上次驗收檢查碼。"""

    def __init__(self, threshold=2, seed_checks=None):
        self.threshold = threshold
        self.counts = Counter()
        self.fingerprints = {}
        self.last_checks = tuple(sorted(set(seed_checks))) if seed_checks else None

    def observe(self, name, result):
        """記錄一個工具結果；回傳本次觀察造成的觸發（dict）或 None。

        工具錯誤：同一指紋累計達門檻即觸發。驗收檢查碼：與上一次（含帶入的
        ``seed_checks``）完全相同即觸發。
        """
        signal = failure_signal(name, result)
        if signal is None:
            return None
        kind, fingerprint = signal
        if kind == 'validation':
            fingerprint = {'checks': fingerprint['checks']}
            hit = tuple(fingerprint['checks']) == self.last_checks
            self.last_checks = tuple(fingerprint['checks'])
        ident = _encoded({'kind': kind, **fingerprint})
        self.counts[ident] += 1
        self.fingerprints[ident] = {'kind': kind, 'fingerprint': fingerprint}
        if kind == 'tool_error':
            hit = self.counts[ident] >= self.threshold
        return {**self.fingerprints[ident], 'count': self.counts[ident]} if hit else None

    def deduplicated(self):
        return [{**self.fingerprints[k], 'count': n} for k, n in sorted(
            self.counts.items(), key=lambda item: (-item[1], item[0]))]


def _cap_strings(value, cap):
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + '…'
    if isinstance(value, dict):
        return {k: _cap_strings(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_strings(v, cap) for v in value]
    return value


def build_digest(task_prompt, trigger, errors, recent_calls, limit=8192):
    """組裁判輸入摘要：經 ``project_public`` 擋私有欄位，再逐步截短到 ≤ ``limit`` 位元組。"""
    recent = [{'name': a.get('name'), 'arguments': a.get('arguments'), 'result': a.get('result')}
              for a in recent_calls[-2:]]
    state = routing.project_public({'task': task_prompt, 'tool_results': recent,
                                    'public_diagnostics': {'trigger': trigger, 'errors': errors}})
    candidates = [state] + [_cap_strings(state, cap) for cap in _STRING_CAPS]
    lean = deepcopy(candidates[-1])
    lean['observations']['public_diagnostics']['errors'] = errors[:8]
    lean['observations']['tool_results'] = [{'name': c['name']} for c in recent]
    candidates.append(lean)
    for digest in candidates:
        if len(_encoded(digest)) <= limit:
            return digest
    raise ValueError('Loop guard digest cannot fit its byte limit')


def parse_verdict(content):
    """嚴格解析裁判 JSON；不合規回 None。"""
    try:
        body = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return None
    if not isinstance(body, dict) or set(body) != _VERDICT_KEYS:
        return None
    stuck, confidence, redirect = body['stuck'], body['confidence'], body['redirect']
    if (type(stuck) is not bool or type(confidence) not in (int, float)
            or not 0 <= confidence <= 1 or not isinstance(redirect, str)
            or len(redirect) > MAX_REDIRECT_CHARS or (stuck and not redirect.strip())):
        return None
    return {'stuck': stuck, 'confidence': confidence, 'redirect': redirect}


class LoopGuard:
    """單次作答的守衛；以 ``turn_hook`` 接進 ``Core._attempt`` 的每回合結尾。"""

    def __init__(self, core, judge, settings, *, prefix, index, tier, judge_calls=1,
                 seed_checks=None, judge_tier='judge'):
        self.core, self.judge, self.settings = core, judge, settings
        self.prefix, self.index, self.tier = prefix, index, tier
        self.judge_calls, self.judge_tier = judge_calls, judge_tier
        self.detector = Detector(settings.repeat_threshold, seed_checks)
        self.calls = []
        self.record = None
        self.redirected = False
        self.seen = 0

    def turn_hook(self, *, turn, actions, messages):
        """回傳 None 表示照常繼續；回傳停止原因字串表示結束本次作答。"""
        fresh, self.seen = actions[self.seen:], len(actions)
        trigger = None
        for action in fresh:
            hit = self.detector.observe(action['name'], action.get('result'))
            trigger = hit or trigger
        if trigger is None:
            return None
        if self.redirected:
            self.record.update(action='redirected_still_stuck', ended_attempt=True,
                               errors=self.detector.deduplicated(),
                               still_stuck_trigger=trigger, ended_turn=turn)
            return STOP
        if self.record is not None or len(self.calls) >= self.judge_calls:
            return None
        self.record = self._consult(trigger, messages, fresh, turn, actions)
        if self.record['action'] == 'redirected':
            self.redirected = True
            messages.append({'role': 'user', 'content': REDIRECT_PREFIX + self.record['judge']['redirect']})
        return None

    def _consult(self, trigger, messages, fresh, turn, actions):
        from .advisor import judge_call_id
        prompt = next(m['content'] for m in messages if m['role'] == 'user')
        digest = build_digest(prompt, trigger, self.detector.deduplicated(), actions,
                              self.settings.digest_limit_bytes)
        profile = self.judge
        if self.core.execution_policy is not None:
            location = profile.get('execution_location')
            self.core.execution_policy.require('generation', location)
            if not isinstance(location, str) or not location.startswith('cloud:'):
                raise PolicyDenied('generation', 'backend_location_mismatch')
        judge_messages = [{'role': 'system', 'content': JUDGE_SYSTEM},
                          {'role': 'user', 'content': _encoded(digest).decode()}]
        cid = judge_call_id(self.prefix, self.index)
        payload = self.core.generation_payload(judge_messages, profile, self.prefix, None)
        record, info = self.core._checked(cid, transport.CHAT, payload, profile['accepted_models'],
                                          profile['provider_name'], allow_rejection=True)
        self.calls.append({'tier': self.judge_tier, **info})
        verdict, outcome = None, 'judge_rejected'
        if not info.get('provider_rejected'):
            message = json.loads(record['raw'])['choices'][0]['message']
            verdict = parse_verdict(message.get('content'))
            outcome = 'judge_invalid' if verdict is None else 'verdict'
        would = (verdict is not None and verdict['stuck']
                 and verdict['confidence'] >= self.settings.confidence_threshold)
        if outcome != 'verdict':
            action = outcome
        elif not verdict['stuck']:
            action = 'not_stuck'
        elif not would:
            action = 'below_threshold'
        else:
            action = 'redirected' if self.settings.mode == 'active' else 'shadow_recorded'
        return {'version': VERSION, 'mode': self.settings.mode, 'attempt': self.index, 'tier': self.tier,
                'turn': turn, 'trigger': trigger, 'errors': self.detector.deduplicated(),
                'judge': {'call_id': cid, 'outcome': outcome, **(verdict or {}),
                          'digest_bytes': len(_encoded(digest))},
                'would_redirect': would, 'action': action, 'ended_attempt': False}


def guarded_attempt(core, env, messages, tier, prefix, index, actions, directory, *, judge, settings,
                    judge_calls=1, seed_checks=None):
    """以迴圈守衛包住 ``core._attempt``；路徑二與路徑一加強版共用。

    回傳的作答紀錄多一個 ``loop_guard`` 欄位（未觸發時為 ``{'triggered': False, 'calls': []}``）；
    守衛紀錄另存為不可覆寫證據 ``loop-guard-a<i>.json``。
    """
    from .engine import _save
    guard = LoopGuard(core, judge, settings, prefix=prefix, index=index, tier=tier,
                      judge_calls=judge_calls, seed_checks=seed_checks)
    attempt = core._attempt(env, messages, tier, prefix, index, actions, directory,
                            turn_hook=guard.turn_hook)
    model = next((c.get('model') for c in attempt['calls'] if c.get('model')), None)
    if guard.record is None:
        attempt['loop_guard'] = {'triggered': False, 'calls': guard.calls}
    else:
        attempt['loop_guard'] = {'triggered': True, **guard.record, 'model': model, 'calls': guard.calls}
        _save(directory / f'loop-guard-a{index}.json', attempt['loop_guard'])
    return attempt


def experience_findings(result):
    """把一題結果中的守衛紀錄轉成契約經驗庫（ADR 0036）可收的結構化發現。

    只含公開資訊：模型、層級、錯誤指紋與次數、裁判判斷與處置；不含題目內容。
    """
    findings = []
    for attempt in result.get('attempts', []):
        guard = attempt.get('loop_guard') or {}
        if not guard.get('triggered'):
            continue
        judge = guard['judge']
        findings.append({'kind': 'loop_guard', 'version': guard['version'], 'mode': guard['mode'],
                         'run_id': result.get('run_id'), 'attempt': guard['attempt'],
                         'setting': attempt.get('setting'), 'tier': guard['tier'], 'model': guard.get('model'),
                         'trigger': deepcopy(guard['trigger']), 'errors': deepcopy(guard['errors']),
                         'judge_outcome': judge['outcome'], 'stuck': judge.get('stuck'),
                         'confidence': judge.get('confidence'), 'action': guard['action'],
                         'ended_attempt': guard['ended_attempt'],
                         'final_public_pass': attempt['public']['passed']})
    return findings
