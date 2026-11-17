"""模型體檢（ADR 0035 第 1–5 點）：離線出題、判分、考官檢查、觸發與結果紀錄。

本模組沒有任何網路傳輸：開源顧問出題器、考官、受測模型都是呼叫端注入的函式。
本模組只產生體檢紀錄，不改動任何模型池（換池屬 #78）。
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import json
import math
import random

from ..sql import SqlContract
from .catalog import digest, timestamp
from .sql_exam import TABLES, dataset, definition

VERSION = 'model-checkup-v1'
K_RANGE = range(1, 7)


@dataclass(frozen=True)
class Variant:
    """題庫原題的變體：同一題型與語意，換門檻 k 與資料種子，避免背題。"""
    base_id: str
    family: str
    variant: int
    k: int
    data_seed: int
    source: str  # 'advisor'（開源顧問出題）或 'program'（程式決定性出題）

    def _definition(self):
        return definition(self.family, self.variant, self.k)

    def contract(self):
        columns, _, oracle, prompt, _ = self._definition()
        samples = []
        for i in range(3):
            tables = dataset(self.data_seed + i, empty=(i == 2))
            samples.append({'id': f'case-{i}', 'tables': tables, 'expected': oracle(tables)})
        return SqlContract(version=f'{VERSION}:{self.base_id}:k{self.k}:s{self.data_seed}',
                           prompt=prompt, tables=TABLES, expected_columns=columns, samples=samples)

    def reference_answer(self, *, mutant=False):
        return json.dumps({'sql': self._definition()[4 if mutant else 1]})

    def descriptor(self):
        return {'base_id': self.base_id, 'k': self.k, 'data_seed': self.data_seed, 'source': self.source}


def make_variant(base, generator, *, seed):
    """以注入的出題器產生變體；出題器只能選門檻與資料種子，不能改判分標準。"""
    proposal = generator(base, seed)
    if not isinstance(proposal, dict) or proposal.get('base_id') != base.id:
        raise ValueError('Variant must derive from the given bank case')
    k, data_seed = proposal.get('k'), proposal.get('data_seed')
    if type(k) is not int or k not in K_RANGE or type(data_seed) is not int or data_seed < 0:
        raise ValueError('Variant parameters out of bank range')
    return Variant(base.id, base.family, base.variant, k, data_seed, 'advisor')


def program_variant(base, *, seed):
    """不經顧問的決定性變體：同家族迴避或出題器失效時使用。"""
    rng = random.Random(f'{VERSION}:{base.id}:{seed}')
    return Variant(base.id, base.family, base.variant, rng.choice(K_RANGE),
                   rng.randrange(1, 10**6), 'program')


def grade_program(variant, answer):
    """程式判分：在凍結的 SQLite 工作程序實際執行並比對預期列。"""
    result = variant.contract().environment().validate(answer)
    return {'passed': result['passed'] is True, 'checks': result['checks'], 'grader': 'program'}


@dataclass(frozen=True)
class OpenItem:
    """程式無法判分的題目；附固定對照答案與故意做錯的 mutant 答案，用來檢查考官。"""
    id: str
    prompt: str
    control_answer: str
    mutant_answer: str


def check_examiner(examiner, calibration, *, advisor_version):
    """考官也要先考：對照答案全部判對、mutant 全部判錯才算通過；沒有校準題一律不通過。"""
    controls = sum(examiner(item, item.control_answer) is True for item in calibration)
    mutants = sum(examiner(item, item.mutant_answer) is False for item in calibration)
    return {'advisor_version': advisor_version,
            'passed': bool(calibration) and controls == mutants == len(calibration),
            'controls_accepted': controls, 'mutants_rejected': mutants, 'items': len(calibration)}


def examiner_gate(previous, examiner, calibration, *, advisor_version):
    """顧問版本不變就沿用上次檢查結果；換版（或從未檢查）才重新檢查。"""
    if previous is not None and previous.get('advisor_version') == advisor_version:
        return previous
    return check_examiner(examiner, calibration, advisor_version=advisor_version)


def _index_models(snapshot):
    indexed = {}
    for entry in snapshot:
        model_id = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(model_id, str) or model_id in indexed:
            raise ValueError('Model list snapshot needs unique string ids')
        indexed[model_id] = digest(entry)
    return indexed


def checkup_triggers(*, now, last_checkup_at, interval_days, previous_models, current_models):
    """離線判斷是否該體檢：固定週期到期，或模型清單（免費 GET 的快照）有變動。

    快照由呼叫端提供；本函式不連網。回傳觸發原因清單，空清單代表不必體檢。
    """
    triggers = []
    if last_checkup_at is None or timestamp(now) - timestamp(last_checkup_at) >= timedelta(days=interval_days):
        triggers.append({'reason': 'schedule'})
    before, after = _index_models(previous_models), _index_models(current_models)
    change = {'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()),
              'changed': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}
    if any(change.values()):
        triggers.append({'reason': 'model_list_changed', **change})
    return triggers


def wilson_interval(passed, graded, *, z=1.96):
    """二項比例的 Wilson 95% 信賴區間；題數少時比常態近似穩。"""
    if graded == 0:
        return {'method': 'wilson', 'level': 0.95, 'low': None, 'high': None}
    p = passed / graded
    denominator = 1 + z * z / graded
    center = (p + z * z / (2 * graded)) / denominator
    half = z * math.sqrt(p * (1 - p) / graded + z * z / (4 * graded * graded)) / denominator
    return {'method': 'wilson', 'level': 0.95,
            'low': round(max(0.0, center - half), 4), 'high': round(min(1.0, center + half), 4)}


def _cost(value):
    if not isinstance(value, str):
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def run_checkup(*, target, answer, bank, now, seed, advisor=None, open_items=(), examiner_check=None,
                triggers=()):
    """對一個模型設定做一次體檢，回傳可追溯紀錄；不改動任何模型池。

    target: {'configuration_id', 'family'}；answer(task) 是受測模型（呼叫端注入），
    只收到公開題目 {'item_id', 'kind', 'prompt'}，回傳 {'answer', 'cost_usd'}。
    advisor: {'id', 'version', 'family', 'generator', 'examiner'} 或 None。
    examiner_check: 來自 examiner_gate；版本須與顧問相同且通過，考官才會閱卷。
    """
    same_family = advisor is not None and advisor['family'] == target['family']
    may_write = advisor is not None and not same_family and advisor.get('generator') is not None
    examiner_used = (advisor is not None and not same_family and advisor.get('examiner') is not None
                     and examiner_check is not None and examiner_check.get('passed') is True
                     and examiner_check.get('advisor_version') == advisor['version'])
    rows, fallbacks, known, unknown = [], [], Decimal(0), 0

    def ask(item_id, kind, prompt):
        nonlocal known, unknown
        reply = answer({'item_id': item_id, 'kind': kind, 'prompt': prompt})
        amount = _cost(reply.get('cost_usd'))
        if amount is None:
            unknown += 1
        else:
            known += amount
        return reply.get('answer'), (None if amount is None else str(amount))

    for base in bank:
        variant = None
        if may_write:
            try:
                variant = make_variant(base, advisor['generator'], seed=seed)
            except ValueError:
                fallbacks.append(base.id)
        variant = variant or program_variant(base, seed=seed)
        text, cost = ask(base.id, 'program', variant.contract().task()['prompt'])
        graded = grade_program(variant, text if isinstance(text, str) else '')
        rows.append({'item_id': base.id, 'kind': 'program', 'variant': variant.descriptor(),
                     'grader': 'program', 'passed': graded['passed'], 'checks': graded['checks'],
                     'cost_usd': cost})
    for item in open_items:
        text, cost = ask(item.id, 'open', item.prompt)
        verdict = advisor['examiner'](item, text) is True if examiner_used else None
        rows.append({'item_id': item.id, 'kind': 'open', 'variant': None,
                     'grader': 'examiner' if examiner_used else None, 'passed': verdict, 'cost_usd': cost})
    graded = [r for r in rows if r['passed'] is not None]
    passed = sum(r['passed'] for r in graded)
    total = len(rows)
    body = {'schema_version': VERSION, 'created_at': timestamp(now).isoformat(),
            'configuration_id': target['configuration_id'], 'target_family': target['family'],
            'advisor': None if advisor is None else {k: advisor[k] for k in ('id', 'version', 'family')},
            'triggers': list(triggers), 'same_family_avoided': same_family,
            'examiner_check': examiner_check, 'examiner_used': examiner_used,
            'variant_fallbacks': fallbacks, 'items': rows,
            'graded': len(graded), 'passed': passed,
            'score': round(passed / len(graded), 4) if graded else None,
            'confidence_interval': wilson_interval(passed, len(graded)),
            'known_cost_usd': str(known), 'unknown_cost_count': unknown,
            # 完整任務成本：含答錯、交白卷的呼叫；有任何未知費用就不給總數。
            'complete_task_cost_usd': None if unknown else str(known),
            'cost_per_task_usd': None if unknown or not total else str(known / total)}
    return {**body, 'record_id': digest(body)}
