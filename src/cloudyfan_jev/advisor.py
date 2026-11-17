"""顧問先答路徑（路徑二，ADR 0034）的設定、凍結描述與角色記帳規則。

本模組只定義設定與規則，不呼叫任何模型；顧問先答的實際流程在 ``advisor_flow``（#73）。
沒有提供 ``AdvisorConfig`` 時，Core、預算與記帳行為完全不變。
"""
from copy import deepcopy
from dataclasses import dataclass, field
import re

from .generation_request import reasoning_fields

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
    # 契約經驗（#76，ADR 0036 第 10、11 點）：ExperienceLink；None 時路徑二行為不變。
    experience: object = field(default=None)

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
                'pool': self.resolved_pool(profiles),
                # 只凍結卡片規則；經驗庫內容是執行期狀態，以每題凍結的卡片為準。
                **({} if self.experience is None else {'experience': self.experience.descriptor()})}


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
