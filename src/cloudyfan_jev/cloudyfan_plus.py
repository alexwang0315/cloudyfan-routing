"""路徑一加強版（``cloudyfan_plus``；ADR 0034 第 8 點、ADR 0036 第 10 點；#79）。

明示選用、預設關閉、有版本的路由模式。Jev 升級路徑（純 ``cloudyfan``）一字不改，
只多兩樣：

1. 迴圈守衛裁判（#74 ``loop_guard.guarded_attempt``）：只在單次作答的工具迴圈內運作；
   插過一次提示仍卡住就結束本次作答，交回 Jev 照原規則升級。
2. 契約經驗的解題事實：建題時凍結一份只含解題事實的卡片，以參考區塊整塊附在原題後面。

模型成績照常回寫經驗庫，但只記錄、不進 Jev 或 QCS（ADR 0017）。
沒有提供 ``CloudyfanPlusConfig`` 時，Core、預算與記帳行為完全不變。
"""
from dataclasses import dataclass, field

from .loop_guard import LoopGuardConfig

CLOUDYFAN_PLUS = 'cloudyfan_plus'
VERSION = 'cloudyfan-plus-v1'
BASE_MODE = 'cloudyfan'


@dataclass(frozen=True)
class CloudyfanPlusConfig:
    """裁判設定、迴圈守衛設定與（選用的）契約經驗庫連結；建構時即驗證。"""
    judge: dict
    loop_guard: LoopGuardConfig = field(default_factory=LoopGuardConfig)
    judge_calls_per_attempt: int = 1
    # ExperienceLink（#76）；None 時不附解題事實、不回寫。
    experience: object = field(default=None)

    def __post_init__(self):
        from copy import deepcopy
        from .advisor import _check_role
        _check_role(self.judge, 'judge')
        object.__setattr__(self, 'judge', deepcopy(self.judge))
        if not isinstance(self.loop_guard, LoopGuardConfig):
            raise ValueError('loop_guard requires a LoopGuardConfig')
        if type(self.judge_calls_per_attempt) is not int or not 0 <= self.judge_calls_per_attempt <= 1:
            raise ValueError('judge_calls_per_attempt must be 0 or 1')
        if self.experience is not None:
            from .experience_card import ExperienceLink
            if not isinstance(self.experience, ExperienceLink):
                raise ValueError('experience requires an ExperienceLink')

    def descriptor(self):
        """凍結進每題 manifest 的設定（只在 ``cloudyfan_plus`` 題目寫入）。"""
        from copy import deepcopy
        from .experience_card import FACTS_HEADER, MAX_CARD_BYTES
        return {'version': VERSION, 'base_mode': BASE_MODE, 'judge': deepcopy(self.judge),
                'judge_calls_per_attempt': self.judge_calls_per_attempt,
                'loop_guard': self.loop_guard.descriptor(),
                'stats_policy': STATS_POLICY,
                **({} if self.experience is None else {'experience': {
                    'facts_version': FACTS_VERSION, 'max_facts_bytes': MAX_CARD_BYTES,
                    'facts_header': FACTS_HEADER, 'stats_snapshot': STATS_SNAPSHOT}})}


# 經驗庫成績只記錄、不進 Jev 或 QCS（ADR 0017）；凍結在描述中，改變此政策要換版本。
STATS_POLICY = 'record_only_not_routed'
FACTS_VERSION = 'cloudyfan-plus-facts-v1'
# 路徑一沒有模型池快照；成績以模式版本代碼標記，避免與路徑二的池內成績混算。
STATS_SNAPSHOT = VERSION
SUPPORT_EXCLUDED = frozenset({'LOOP_GUARD_STUCK'})


def check_experience(config, experience):
    """有傳經驗身分時，設定必須接上經驗庫；身分只收代碼型字串。"""
    if experience is None:
        return
    if config.experience is None:
        raise ValueError('Contract experience requires an ExperienceLink in CloudyfanPlusConfig')
    from .experience_card import check_context
    check_context(experience)


def manifest_fields(config, experience):
    fields = {'cloudyfan_plus': config.descriptor()}
    if experience is not None:
        fields['experience'] = dict(experience)
    return fields


def _view_hash(view):
    import hashlib
    from .experience_card import _encoded
    return hashlib.sha256(_encoded(view)).hexdigest()


def build_facts(store, *, client_id, scope, settings):
    """只含解題事實的卡片（不含任何成績）；超過上限時從尾端整條省略事實。"""
    from .experience_card import MAX_CARD_BYTES, _encoded, _fact, _identity
    found = store.facts_for(client_id, scope, settings=settings)
    body = {'scope': scope, 'facts': [_fact(i) for i in found['private'] + found['shared']],
            'facts_omitted': 0}
    while True:
        view = {'card': _identity(body), **body}
        if len(_encoded(view)) <= MAX_CARD_BYTES:
            break
        if not body['facts']:
            raise ValueError('experience facts exceed byte limit')
        body['facts'] = body['facts'][:-1]
        body['facts_omitted'] += 1
    return {'version': FACTS_VERSION, 'client_id': client_id, 'settings': dict(settings),
            'store_version': store.export()['body']['version'], 'view': view,
            'view_sha256': _view_hash(view)}


def freeze_facts(path, config, context):
    """建題時組好並存成不可覆寫證據；續跑讀回核對後沿用同一版，不重新讀經驗庫。"""
    from .engine import _read, _save
    if path.is_symlink():
        raise ValueError('Frozen experience facts cannot be a symlink')
    if path.exists():
        card = _read(path)
        if (not isinstance(card, dict) or card.get('version') != FACTS_VERSION
                or _view_hash(card.get('view')) != card.get('view_sha256')
                or card.get('client_id') != context['client_id']
                or card['view'].get('scope') != context['scope']):
            raise ValueError('Frozen experience facts unverifiable')
        return card
    store = config.experience.refresh()
    card = build_facts(store, client_id=context['client_id'], scope=context['scope'],
                       settings=store.freeze_settings(context['client_id']))
    _save(path, card)
    return card


def with_facts(prompt, card):
    """原題不改，解題事實以參考區塊整塊附在後面；沒有事實時原題一字不變。"""
    from .experience_card import facts_block
    block = facts_block(card)
    return prompt if block is None else prompt + block['rendered']


def result_fields(card):
    from .experience_card import facts_block
    block = facts_block(card)
    return {'card': card['view']['card'], 'store_version': card['store_version'],
            'facts_block': None if block is None else block['id'], 'scope': card['view']['scope']}


def attempt(core, env, messages, tier, prefix, index, actions, directory, previous):
    """單次作答；由迴圈守衛包住工具迴圈。上一次作答的公開驗收檢查碼帶入偵測器，
    修復作答若在工具驗收上撞回同樣的檢查碼即觸發。停用裁判時照舊。"""
    config = core.cloudyfan_plus
    if config.judge_calls_per_attempt == 0:
        return core._attempt(env, messages, tier, prefix, index, actions, directory)
    from .loop_guard import guarded_attempt
    seed = None
    public = (previous or {}).get('public') or {}
    checks = public.get('checks')
    if (public.get('passed') is False and isinstance(checks, list)
            and all(isinstance(c, str) for c in checks)):
        seed = [c for c in checks if c not in SUPPORT_EXCLUDED] or None
    return guarded_attempt(core, env, messages, tier, prefix, index, actions, directory,
                           judge=config.judge, settings=config.loop_guard,
                           judge_calls=config.judge_calls_per_attempt, seed_checks=seed)


def outcomes(core, result):
    """各次作答的成績（只記錄）：綁定精確模型版本、強度、供應商與本模式版本代碼。"""
    from .experience_card import _cost, _verdict, provider_token
    rows = []
    for item in result.get('attempts', []):
        profile = core._generation_profile(item['tier'])
        rows.append({'role': 'solver', 'model_version': profile['model_version'], 'effort': profile['effort'],
                     'provider': provider_token(profile['provider_name']),
                     'verdict': _verdict(item['public']['passed']), 'cost_usd': _cost(item.get('calls', [])),
                     'pool_snapshot': STATS_SNAPSHOT})
    return rows


def write_back(core, context, card, result):
    """結果與迴圈守衛發現回寫經驗庫；同一題重寫內容相同時不重複計算。"""
    from .experience_card import findings
    supports = [f for f in findings(result) if not (f['kind'] == 'check_code' and f['code'] in SUPPORT_EXCLUDED)]
    return core.cloudyfan_plus.experience.record({
        'task_id': result['run_id'], 'client_id': context['client_id'], 'scope': context['scope'],
        'settings': dict(card['settings']), 'created_at': context['created_at'],
        'supports': supports, 'contradicts': [], 'outcomes': outcomes(core, result)})
