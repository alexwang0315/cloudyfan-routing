"""顧問先答流程（路徑二，ADR 0034 第 1、2、4、10 點）。

流程：開源顧問先看公開題目，回固定 JSON（直接回答或改派），並一律附 ≥ 2 組
池內建議。直接回答沒通過公開驗收，或顧問選擇改派，就依建議順序派送池內固定設定；
每題最多 ``max_attempts`` 次作答（含顧問直接回答）。建議用完、全部作廢或 JSON
解析失敗即結束並照實記錄，不設預設接手、不回頭走 Jev。

作答模型只收原題（之後由 #76 附上解題事實），不收顧問的判斷理由。
迴圈守衛裁判（#74）與契約經驗（#76）只透過 Core 上的兩個掛鉤接入，本模組不實作。
"""
import hashlib
import json
from pathlib import Path
from time import perf_counter

from . import client as transport
from . import routing
from .advisor import ADVISOR_FIRST, advisor_call_id
from .privacy import PolicyDenied

FLOW_VERSION = 'advisor-first-flow-v1'
POOL_PREFIX = 'pool:'
ADVISOR_SYSTEM = (
    "You are the advisor of a model-routing service. Read the public task and the list of "
    "in-pool model settings. Either answer the task yourself (mode \"answer\") or delegate it "
    "(mode \"delegate\"). In both modes you MUST recommend at least two in-pool settings in "
    "priority order as fallbacks. Reply with exactly one JSON object and nothing else: "
    "{\"mode\": \"answer\"|\"delegate\", \"answer\": <final answer text, only in answer mode>, "
    "\"recommendations\": [{\"setting\": <pool setting id>, \"tier\": <its tier>, "
    "\"reason\": <short reason>}, ...]}. Never recommend a setting that is not in the pool."
)
_DECISION_KEYS = {'mode', 'answer', 'recommendations'}
_RECOMMENDATION_KEYS = {'setting', 'tier', 'reason'}
_POOL_FIELDS = ('model_version', 'effort', 'provider_name')


def pool_label(setting):
    """作答收據的 tier 標記；Core._generation_profile 依此找回凍結的池內設定。"""
    return POOL_PREFIX + setting


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def parse_decision(content, pool):
    """嚴格解析顧問 JSON，逐項比對池內設定；不合規的建議作廢並記錄原因。

    回傳的 ``outcome`` 為 ``answer``／``delegate``／``incomplete``。解析失敗或
    建議全部作廢都視為顧問未完成（ADR 0034 第 4 點），此時不採用其回答。
    """
    def incomplete(reason, voided=()):
        return {'outcome': 'incomplete', 'incomplete_reason': reason, 'answer': None,
                'recommendations': [], 'voided': list(voided)}

    try:
        body = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return incomplete('invalid_json')
    mode = body.get('mode')
    if set(body) - _DECISION_KEYS or mode not in ('answer', 'delegate'):
        return incomplete('invalid_schema')
    answer = body.get('answer')
    if (mode == 'answer' and not _text(answer)) or (mode == 'delegate' and answer is not None):
        return incomplete('invalid_schema')
    items = body.get('recommendations')
    if not isinstance(items, list) or len(items) < 2:
        return incomplete('too_few_recommendations')
    valid, voided, seen = [], [], set()
    for rank, item in enumerate(items):
        if (not isinstance(item, dict) or set(item) != _RECOMMENDATION_KEYS
                or not all(_text(item[k]) for k in _RECOMMENDATION_KEYS)):
            reason = 'malformed'
        elif item['setting'] not in pool:
            reason = 'not_in_pool'
        elif pool[item['setting']]['tier'] != item['tier']:
            reason = 'tier_mismatch'
        elif item['setting'] in seen:
            reason = 'duplicate'
        else:
            seen.add(item['setting'])
            valid.append({'rank': rank, **item})
            continue
        voided.append({'rank': rank, 'recommendation': item, 'reason': reason})
    if not valid:
        return incomplete('no_valid_recommendations', voided)
    return {'outcome': mode, 'incomplete_reason': None, 'answer': answer if mode == 'answer' else None,
            'recommendations': valid, 'voided': voided}


def advisor_messages(task, pool):
    """顧問輸入：只放公開題目與池內設定識別；私有欄位一律擋下。"""
    routing._reject_private(task)
    public = routing.project_public({'task': task})
    listing = [{'setting': key, 'tier': profile['tier'], **{k: profile[k] for k in _POOL_FIELDS}}
               for key, profile in pool.items()]
    content = {'task': public['task'], 'pool': listing}
    routing._reject_private(content)
    return [{'role': 'system', 'content': ADVISOR_SYSTEM},
            {'role': 'user', 'content': json.dumps(content, sort_keys=True, ensure_ascii=False)}]


def _consult(core, prefix, task, pool):
    """呼叫顧問一次（同 call_id 續跑讀舊收據，不重付）並解析。"""
    profile = core.advisor_first.advisor
    if core.execution_policy is not None:
        location = profile.get('execution_location')
        core.execution_policy.require('generation', location)
        if not isinstance(location, str) or not location.startswith('cloud:'):
            raise PolicyDenied('generation', 'backend_location_mismatch')
    messages = advisor_messages(task, pool)
    cid = advisor_call_id(prefix)
    payload = core.generation_payload(messages, profile, prefix, None)
    record, info = core._checked(cid, transport.CHAT, payload, profile['accepted_models'],
                                 profile['provider_name'], allow_rejection=True)
    calls = [{'tier': 'advisor', **info}]
    if info.get('provider_rejected'):
        return calls, {'outcome': 'incomplete', 'incomplete_reason': 'provider_rejected', 'answer': None,
                       'recommendations': [], 'voided': []}
    message = json.loads(record['raw'])['choices'][0]['message']
    return calls, parse_decision(message.get('content'), pool)


def run(core, run_id, task, environment_factory, *, contract_version):
    from .engine import _encoded, _lock, _read, _save
    config = core.advisor_first
    pool = config.resolved_pool(core.profiles)
    # 任何 I/O 前先擋私有欄位。
    advisor_messages(task, pool)
    directory = Path(core.client.root) / 'runs' / run_id
    if directory.is_symlink():
        raise ValueError('Run directory cannot be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock(directory / '.lock'):
        if (directory / '.retired.json').exists() or (directory / '.retired.json').is_symlink():
            raise ValueError('Retired run cannot be read or dispatched again')
        frozen = {**core.configuration_descriptor(), 'task': task, 'mode': ADVISOR_FIRST,
                  'contract_version': contract_version}
        _save(directory / 'manifest.json', frozen)
        completed = directory / 'result.json'
        if completed.exists():
            return core._with_costs(_read(completed))
        prefix = 'run-' + run_id
        resumed = any((Path(core.client.root) / 'calls').glob(prefix + '-*'))
        began = perf_counter()
        env = environment_factory()
        replayable = getattr(env, 'replayable_tools', ())
        if (not isinstance(replayable, (tuple, list, set, frozenset))
                or any(not isinstance(name, str) or not name for name in replayable)):
            raise ValueError('replayable_tools must explicitly name deterministic local tools')
        _save(directory / 'tool-replay-policy.json', {'replayable_tools': sorted(set(replayable))})

        advisor_calls, decision = _consult(core, prefix, task, pool)
        # 顧問決定（含作廢原因與理由）只留在紀錄，不進任何作答模型的輸入。
        _save(directory / 'advisor-decision.json', decision)
        actions, attempts = [], []
        if decision['outcome'] == 'answer':
            started = perf_counter()
            public = core.diagnostic_contract.project(env.validate(decision['answer']))
            _save(directory / 'validation-a0.json', public)
            attempts.append({'attempt': 0, 'source': 'advisor_answer', 'tier': 'advisor', 'calls': [],
                             'final': decision['answer'], 'stop': 'advisor_answer', 'public': public,
                             'verification_ms': (perf_counter() - started) * 1000,
                             'cumulative_actions': 0})
        end_reason = 'advisor_incomplete' if decision['outcome'] == 'incomplete' else 'recommendations_exhausted'
        for order, recommendation in enumerate(decision['recommendations'], start=1):
            if attempts and attempts[-1]['public']['passed'] is True:
                break
            if len(attempts) >= config.max_attempts:
                end_reason = 'attempt_limit'
                break
            setting = recommendation['setting']
            attempt = core._advisor_delegated_attempt(
                env, core._advisor_answer_messages(task), pool_label(setting), prefix, order, actions, directory)
            attempt.update(source='delegated', setting=setting, pool_tier=pool[setting]['tier'],
                           recommendation_rank=recommendation['rank'])
            attempts.append(attempt)
        passed = attempts[-1]['public']['passed'] if attempts else None
        if passed is True:
            end_reason = 'passed'
        status = ('advisor_incomplete' if not attempts else 'passed' if passed is True
                  else 'failed' if passed is False else 'unverifiable')
        result = {'run_id': run_id, 'mode': ADVISOR_FIRST,
                  'advisor': {'calls': advisor_calls, 'decision': decision,
                              'score_snapshot': config.score_snapshot},
                  'attempts': attempts, 'actions': actions, 'selection': None,
                  'first_public_pass': attempts[0]['public']['passed'] if attempts else None,
                  'final_public_pass': passed, 'status': status, 'end_reason': end_reason,
                  'resumed': resumed, 'wall_seconds': None if resumed else perf_counter() - began,
                  'config_sha256': hashlib.sha256(_encoded(frozen)).hexdigest()}
        _save(completed, result)
        return core._with_costs(result)
