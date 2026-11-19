"""顧問先答路徑（路徑二，ADR 0034）的設定、凍結描述與角色記帳規則。

本模組只定義設定與規則，不呼叫任何模型；顧問先答的實際流程在 ``advisor_flow``（#73）。
沒有提供 ``AdvisorConfig`` 時，Core、預算與記帳行為完全不變。
"""
from copy import deepcopy
from dataclasses import dataclass, field
import re

from .generation_request import reasoning_fields
from .loop_guard import LoopGuardConfig

ADVISOR_FIRST = 'advisor_first'
VERSION = 'advisor-first-config-v1'
ROLES = ('advisor', 'judge')
POOL_TIERS = ('low', 'mid', 'high')
# 起始設定（ADR 0034 第 10 點），不是驗證過的最佳值。
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_JUDGE_CALLS_PER_ATTEMPT = 1

_IDENTITY = ('model', 'model_version', 'effort', 'provider', 'provider_name')


def _check_identity(profile, label):
    if not isinstance(profile, dict):
        raise ValueError(label + ' profile must be a mapping')
    for name in _IDENTITY:
        if not isinstance(profile.get(name), str) or not profile[name].strip():
            raise ValueError(label + ' profile requires explicit ' + name)
    reasoning_fields(profile)
    accepted = profile.get('accepted_models')
    if not isinstance(accepted, list) or not accepted or any(not isinstance(x, str) or not x for x in accepted):
        raise ValueError(label + ' profile requires explicit accepted_models')
    if profile['model_version'] not in accepted:
        raise ValueError(label + ' pinned model version must be accepted')


def _check_role(profile, role):
    _check_identity(profile, role)
    # 零模型費用情境只認設定中的明示標記，不從模型名稱推測。
    if type(profile.get('open_source')) is not bool:
        raise ValueError(role + ' profile requires explicit open_source boolean')


@dataclass(frozen=True)
class AdvisorConfig:
    """路徑二的顧問、裁判、評分快照與作答上限；建構時即驗證。

    ``pool`` 是顧問可建議的池內固定設定（設定識別 → 含 ``tier`` 的設定）。
    省略時，由 Core 以現行低／中／高設定補上並一起凍結。
    """
    advisor: dict
    judge: dict
    score_snapshot: str
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    judge_calls_per_attempt: int = DEFAULT_JUDGE_CALLS_PER_ATTEMPT
    pool: dict | None = field(default=None)
    loop_guard: LoopGuardConfig = field(default_factory=LoopGuardConfig)
    # 契約經驗（#76，ADR 0036 第 10、11 點）：ExperienceLink；None 時路徑二行為不變。
    experience: object = field(default=None)
    # 由 from_snapshot 建立時記錄評分快照的有效期限；過期後路徑二在任何呼叫前拒跑。
    snapshot_valid_until: str | None = field(default=None)

    def __post_init__(self):
        for role in ROLES:
            _check_role(getattr(self, role), role)
            object.__setattr__(self, role, deepcopy(getattr(self, role)))
        if not isinstance(self.score_snapshot, str) or not self.score_snapshot.strip():
            raise ValueError('score_snapshot identity required')
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= DEFAULT_MAX_ATTEMPTS:
            raise ValueError('max_attempts must be an integer from 1 to 3')
        if type(self.judge_calls_per_attempt) is not int or not 0 <= self.judge_calls_per_attempt <= 1:
            raise ValueError('judge_calls_per_attempt must be 0 or 1')
        if not isinstance(self.loop_guard, LoopGuardConfig):
            raise ValueError('loop_guard requires a LoopGuardConfig')
        if self.pool is not None:
            object.__setattr__(self, 'pool', deepcopy(self.pool))
        if self.experience is not None:
            from .experience_card import ExperienceLink
            if not isinstance(self.experience, ExperienceLink):
                raise ValueError('experience requires an ExperienceLink')

    def resolved_pool(self, profiles):
        """回傳要凍結的池內設定；每項都必須標明低／中／高層級。"""
        pool = (self.pool if self.pool is not None
                else {tier: {**profiles[tier], 'tier': tier} for tier in POOL_TIERS if tier in profiles})
        if not isinstance(pool, dict) or not pool:
            raise ValueError('advisor pool requires at least one setting')
        for key, profile in pool.items():
            if not isinstance(key, str) or not key:
                raise ValueError('advisor pool setting identity required')
            _check_identity(profile, 'pool ' + key)
            if profile.get('tier') not in POOL_TIERS:
                raise ValueError('advisor pool setting requires tier low, mid or high')
        return deepcopy(pool)

    def descriptor(self, profiles):
        from .advisor_flow import ADVISOR_SYSTEM, FLOW_VERSION
        return {'version': VERSION, 'flow': {'version': FLOW_VERSION, 'advisor_system': ADVISOR_SYSTEM},
                'advisor': deepcopy(self.advisor), 'judge': deepcopy(self.judge),
                'score_snapshot': self.score_snapshot, 'max_attempts': self.max_attempts,
                'judge_calls_per_attempt': self.judge_calls_per_attempt,
                'loop_guard': self.loop_guard.descriptor(),
                'pool': self.resolved_pool(profiles),
                # 由評分快照建立的候選，另凍結快照身分與有效期；手動設定保持原描述。
                **({} if self.snapshot_valid_until is None else {'pool_source': {
                    'kind': 'score_snapshot', 'snapshot_id': self.score_snapshot,
                    'valid_until': self.snapshot_valid_until}}),
                # 只凍結卡片規則；經驗庫內容是執行期狀態，以每題凍結的卡片為準。
                **({} if self.experience is None else {'experience': self.experience.descriptor()})}

    @classmethod
    def from_snapshot(cls, snapshot, *, now, advisor, judge, requirements=None, **fields):
        """以評分快照的低／中／高池建立顧問候選（ADR 0034 第 2 點）。

        候選就是快照中已入池的固定設定，鍵為 ``config_id``；快照須通過雜湊驗證且在
        有效期內。自動換池（ADR 0035）停用的池不列入；開源池不是可建議的品質池。
        """
        return cls(advisor=advisor, judge=judge, score_snapshot=snapshot['snapshot_id'],
                   pool=pool_from_snapshot(snapshot, now=now, requirements=requirements),
                   snapshot_valid_until=snapshot['valid_until'], **fields)

    def refreshed(self, snapshot, *, now, requirements=None):
        """自動換池發布新快照後，回傳以新快照候選組成的**新**設定；快照沒變就回傳自己。

        顧問、裁判、作答上限、迴圈守衛與經驗庫連結沿用。主服務要把新設定註冊成新的契約
        （新的 Core／契約 ID），舊契約保留給已建立的題目：任務服務以建題時凍結的設定核對，
        原地換掉設定會讓舊題的結果讀不出來（``frozen_configuration_changed``）。
        """
        if snapshot['snapshot_id'] == self.score_snapshot:
            return self
        from dataclasses import replace
        return replace(self, score_snapshot=snapshot['snapshot_id'],
                       pool=pool_from_snapshot(snapshot, now=now, requirements=requirements),
                       snapshot_valid_until=snapshot['valid_until'])


def pool_from_snapshot(snapshot, *, now, requirements=None):
    """回傳 ``{config_id: 設定（含 tier 與 score_selection）}``；缺合格候選即拒絕。"""
    from .scores.catalog import verify_snapshot
    verify_snapshot(snapshot, now=now)
    if requirements is not None:
        if requirements.get('domain') != snapshot['policy']['domain']:
            raise ValueError('Task domain does not match score policy')
        required = set(requirements.get('capabilities') or [])
        context = requirements.get('context_tokens', 0)
    disabled = set((snapshot.get('pool_override') or {}).get('disabled_tiers') or [])
    pool = {}
    for tier in POOL_TIERS:
        if tier in disabled:
            continue
        for entry in snapshot['pools'][tier]:
            if requirements is not None and not (required <= set(entry['capabilities'])
                                                 and context <= entry['context_tokens']):
                continue
            profile = deepcopy(entry['profile'])
            profile['tier'] = tier
            profile.setdefault('capability_card', {})['score_selection'] = {
                'snapshot_id': snapshot['snapshot_id'], 'policy_id': snapshot['policy']['id'],
                'config_id': entry['config_id']}
            pool[entry['config_id']] = profile
    if not pool:
        raise ValueError('No qualified configuration in the score snapshot for the advisor pool')
    return pool


# 收據 call_id 命名約定：顧問 ``<prefix>-advisor``；裁判 ``<prefix>-a<i>-judge``。
_ROLE_PATTERNS = (('advisor', re.compile(r'-advisor(-\d+)?$')),
                  ('judge', re.compile(r'-a\d+-judge(-\d+)?$')))


def advisor_call_id(prefix):
    return prefix + '-advisor'


def judge_call_id(prefix, attempt):
    return f'{prefix}-a{attempt}-judge'


def role_of(call_id):
    """依 call_id 約定判斷顧問／裁判角色；其他呼叫回傳 None。"""
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(call_id):
            return role
    return None
