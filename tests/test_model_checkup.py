"""模型體檢（#77）離線測試：出題器、考官、受測模型都是假的可注入函式。"""
import pytest

from cloudyfan_jev.scores import checkup
from cloudyfan_jev.scores.sql_exam import cases


def fixed_generator(calls):
    """假開源顧問：把題庫原題的門檻改成 k=2、資料種子改成 777。"""
    def generate(base, seed):
        calls.append((base.id, seed))
        return {'base_id': base.id, 'k': 2, 'data_seed': 777}
    return generate


def test_generated_variant_is_program_graded():
    calls = []
    base = cases()[0]
    variant = checkup.make_variant(base, fixed_generator(calls), seed=1)
    assert calls == [('Q001', 1)]
    assert (variant.base_id, variant.k, variant.data_seed, variant.source) == ('Q001', 2, 777, 'advisor')
    assert checkup.grade_program(variant, variant.reference_answer())['passed'] is True
    assert checkup.grade_program(variant, variant.reference_answer(mutant=True))['passed'] is False


@pytest.mark.parametrize('proposal', [
    {'base_id': 'Q001', 'k': 99, 'data_seed': 1},
    {'base_id': 'Q002', 'k': 2, 'data_seed': 1},
    {'base_id': 'Q001', 'k': 2, 'data_seed': -1},
    'free text',
])
def test_variant_outside_bank_is_rejected(proposal):
    with pytest.raises(ValueError):
        checkup.make_variant(cases()[0], lambda base, seed: proposal, seed=1)


def test_program_variant_is_deterministic_and_gradable():
    base = cases()[5]
    one, two = checkup.program_variant(base, seed=3), checkup.program_variant(base, seed=3)
    assert one == two and one.source == 'program' and one.k in checkup.K_RANGE
    assert checkup.grade_program(one, one.reference_answer())['passed'] is True


CALIBRATION = [
    checkup.OpenItem('O1', '說明 NULL 在 SUM 中的行為', control_answer='SUM 會略過 NULL',
                     mutant_answer='SUM 會把 NULL 當成 0 以外的錯誤'),
    checkup.OpenItem('O2', '說明 LEFT JOIN 與 JOIN 的差別', control_answer='LEFT JOIN 保留左表沒有對應的列',
                     mutant_answer='兩者完全相同'),
]


def discerning_examiner(item, answer):
    return answer == item.control_answer


def lenient_examiner(item, answer):
    return True


def test_examiner_that_tells_control_from_mutant_passes_check():
    result = checkup.check_examiner(discerning_examiner, CALIBRATION, advisor_version='oss-1')
    assert result == {'advisor_version': 'oss-1', 'passed': True, 'controls_accepted': 2,
                      'mutants_rejected': 2, 'items': 2}


def test_examiner_that_accepts_mutants_fails_check():
    result = checkup.check_examiner(lenient_examiner, CALIBRATION, advisor_version='oss-1')
    assert result['passed'] is False and result['mutants_rejected'] == 0


def test_examiner_check_is_rerun_only_when_advisor_version_changes():
    calls = []
    def counting(item, answer):
        calls.append(item.id)
        return discerning_examiner(item, answer)
    first = checkup.examiner_gate(None, counting, CALIBRATION, advisor_version='oss-1')
    count = len(calls)
    assert checkup.examiner_gate(first, counting, CALIBRATION, advisor_version='oss-1') == first
    assert len(calls) == count
    upgraded = checkup.examiner_gate(first, lenient_examiner, CALIBRATION, advisor_version='oss-2')
    assert upgraded['advisor_version'] == 'oss-2' and upgraded['passed'] is False


def test_examiner_check_without_calibration_items_fails_closed():
    assert checkup.check_examiner(discerning_examiner, [], advisor_version='oss-1')['passed'] is False


MODELS = [{'id': 'vendor/high-5.1', 'provider': 'A', 'version': '5.1'},
          {'id': 'vendor/mid-2', 'provider': 'B', 'version': '2'}]


def test_no_trigger_within_interval_and_unchanged_model_list():
    assert checkup.checkup_triggers(now='2026-11-17T00:00:00Z', last_checkup_at='2026-11-12T00:00:00Z',
                                    interval_days=7, previous_models=MODELS, current_models=list(MODELS)) == []


def test_first_run_and_elapsed_interval_trigger_schedule():
    for last in (None, '2026-11-10T00:00:00Z'):
        assert checkup.checkup_triggers(now='2026-11-17T00:00:00Z', last_checkup_at=last, interval_days=7,
                                        previous_models=MODELS, current_models=MODELS) == [{'reason': 'schedule'}]


def test_model_list_change_triggers_with_added_removed_changed_ids():
    current = [{'id': 'vendor/high-5.1', 'provider': 'C', 'version': '5.1'},
               {'id': 'vendor/high-5.2', 'provider': 'A', 'version': '5.2'}]
    assert checkup.checkup_triggers(now='2026-11-17T00:00:00Z', last_checkup_at='2026-11-16T00:00:00Z',
                                    interval_days=7, previous_models=MODELS, current_models=current) == [
        {'reason': 'model_list_changed', 'added': ['vendor/high-5.2'], 'removed': ['vendor/mid-2'],
         'changed': ['vendor/high-5.1']}]


def test_duplicate_model_ids_in_snapshot_are_rejected():
    with pytest.raises(ValueError):
        checkup.checkup_triggers(now='2026-11-17T00:00:00Z', last_checkup_at=None, interval_days=7,
                                 previous_models=MODELS, current_models=MODELS + MODELS[:1])


BANK = cases()[:4]
BY_ID = {c.id: c for c in BANK}
NOW = '2026-11-17T00:00:00Z'


def advisor(family='oss-family', version='oss-1', generator=None, examiner=discerning_examiner):
    return {'id': 'oss/advisor', 'version': version, 'family': family,
            'generator': generator or fixed_generator([]), 'examiner': examiner}


def target_answering(correct_ids, costs=None, open_answers=None):
    """假受測模型：只看公開題目；指定題號答對，其餘交 mutant。"""
    def answer(task):
        if task['kind'] == 'open':
            text = (open_answers or {}).get(task['item_id'], '')
        else:
            variant = checkup.make_variant(BY_ID[task['item_id']], fixed_generator([]), seed=0)
            text = variant.reference_answer(mutant=task['item_id'] not in correct_ids)
        return {'answer': text, 'cost_usd': (costs or {}).get(task['item_id'], '0.01')}
    return answer


def run(**overrides):
    options = dict(target={'configuration_id': 'cfg-high', 'family': 'vendor'},
                   answer=target_answering({'Q001', 'Q002'}), bank=BANK, now=NOW, seed=0,
                   advisor=advisor(), triggers=[{'reason': 'schedule'}])
    options.update(overrides)
    return checkup.run_checkup(**options)


def test_record_has_score_wilson_interval_and_complete_task_cost():
    record = run()
    assert record['schema_version'] == 'model-checkup-v1'
    assert record['configuration_id'] == 'cfg-high' and record['triggers'] == [{'reason': 'schedule'}]
    assert [i['passed'] for i in record['items']] == [True, True, False, False]
    assert {i['grader'] for i in record['items']} == {'program'}
    assert all(i['variant']['source'] == 'advisor' for i in record['items'])
    assert (record['graded'], record['passed'], record['score']) == (4, 2, 0.5)
    assert record['confidence_interval'] == {'method': 'wilson', 'level': 0.95, 'low': 0.15, 'high': 0.85}
    assert record['complete_task_cost_usd'] == '0.04' and record['cost_per_task_usd'] == '0.01'
    assert record['unknown_cost_count'] == 0
    assert len(record['record_id']) == 64
    assert 'pool' not in str(sorted(record))


def test_unknown_cost_leaves_complete_cost_unknown():
    record = run(answer=target_answering({'Q001'}, costs={'Q003': None}))
    assert record['unknown_cost_count'] == 1
    assert record['complete_task_cost_usd'] is None and record['known_cost_usd'] == '0.03'


def test_perfect_small_run_interval_lower_bound():
    record = run(answer=target_answering({c.id for c in BANK}), bank=BANK[:3])
    assert record['confidence_interval']['low'] == 0.4385 and record['confidence_interval']['high'] == 1.0


OPEN = [checkup.OpenItem('X1', '解釋為何要 ORDER BY', control_answer='結果順序才確定', mutant_answer='不需要')]


def test_open_items_graded_by_examiner_only_after_passing_check():
    check = checkup.check_examiner(discerning_examiner, CALIBRATION, advisor_version='oss-1')
    record = run(open_items=OPEN, examiner_check=check,
                 answer=target_answering({'Q001', 'Q002'}, open_answers={'X1': '結果順序才確定'}))
    open_row = record['items'][-1]
    assert (open_row['kind'], open_row['grader'], open_row['passed']) == ('open', 'examiner', True)
    assert record['examiner_used'] is True and (record['graded'], record['passed']) == (5, 3)


def test_failed_examiner_check_falls_back_to_program_only_grading():
    check = checkup.check_examiner(lenient_examiner, CALIBRATION, advisor_version='oss-1')
    record = run(open_items=OPEN, examiner_check=check, advisor=advisor(examiner=lenient_examiner))
    open_row = record['items'][-1]
    assert (open_row['grader'], open_row['passed']) == (None, None)
    assert record['examiner_used'] is False and record['graded'] == 4


def test_examiner_check_for_another_advisor_version_is_not_trusted():
    check = checkup.check_examiner(discerning_examiner, CALIBRATION, advisor_version='oss-0')
    assert run(open_items=OPEN, examiner_check=check)['examiner_used'] is False


def test_same_family_advisor_neither_writes_nor_grades_the_exam():
    calls = []
    check = checkup.check_examiner(discerning_examiner, CALIBRATION, advisor_version='oss-1')
    def target_from_program_variants(task):
        if task['kind'] == 'open':
            return {'answer': '結果順序才確定', 'cost_usd': '0'}
        return {'answer': checkup.program_variant(BY_ID[task['item_id']], seed=0).reference_answer(),
                'cost_usd': '0'}
    record = run(advisor=advisor(family='vendor', generator=fixed_generator(calls)), open_items=OPEN,
                 examiner_check=check, answer=target_from_program_variants)
    assert calls == []
    assert record['same_family_avoided'] is True and record['examiner_used'] is False
    assert all(i['variant']['source'] == 'program' for i in record['items'] if i['kind'] == 'program')
    assert record['items'][-1]['passed'] is None and (record['graded'], record['passed']) == (4, 4)


def test_invalid_generator_output_falls_back_to_program_variant():
    record = run(advisor=advisor(generator=lambda base, seed: {'base_id': base.id, 'k': 0, 'data_seed': 1}),
                 answer=lambda task: {'answer': 'not sql', 'cost_usd': '0'})
    assert all(i['variant']['source'] == 'program' for i in record['items'])
    assert record['variant_fallbacks'] == ['Q001', 'Q002', 'Q003', 'Q004']


def test_target_only_sees_public_prompt():
    seen = []
    def spy(task):
        seen.append(task)
        return {'answer': '{}', 'cost_usd': '0'}
    run(answer=spy, bank=BANK[:1])
    assert set(seen[0]) == {'item_id', 'kind', 'prompt'}
    assert 'SELECT' not in seen[0]['prompt'].split('Public contract')[0]
