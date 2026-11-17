"""#76：經驗卡（ADR 0036 第 10、11 點）的組裝測試；不呼叫任何模型。"""
import json


from cloudyfan_jev.experience import ExperienceStore
from cloudyfan_jev.experience_card import MAX_CARD_BYTES, advisor_view, build_card, facts_block

SCOPE = 'arith_v1'
SNAPSHOT = 'snapshot-20261117-a'
ADVISOR = {'model_version': 'oss/advisor-v1', 'effort': 'medium', 'provider_name': 'Example'}
POOL = {'low': {'model_version': 'example/low', 'effort': 'low', 'provider_name': 'Example', 'tier': 'low'},
        'mid': {'model_version': 'example/mid', 'effort': 'low', 'provider_name': 'Example', 'tier': 'mid'}}


def rewriter(fact):
    return '此類題目常見錯誤代碼 ' + fact['code'] + '，提交前請自行檢查。'


def outcome(profile, verdict, role='solver', cost='0.01', snapshot=SNAPSHOT):
    row = {'role': role, 'model_version': profile['model_version'], 'effort': profile['effort'],
           'provider': profile['provider_name'], 'verdict': verdict, 'cost_usd': cost}
    if snapshot is not None:
        row['pool_snapshot'] = snapshot
    return row


def record(store, task, outcomes=(), supports=(), client='c1', day=1):
    store.record({'task_id': task, 'client_id': client, 'scope': SCOPE,
                  'settings': store.freeze_settings(client), 'created_at': f'2026-11-{day:02d}',
                  'supports': list(supports), 'outcomes': list(outcomes)})


def card(store, client='c1', snapshot=SNAPSHOT, advisor=ADVISOR, pool=POOL):
    return build_card(store, client_id=client, scope=SCOPE, settings=store.freeze_settings(client),
                      advisor=advisor, pool=pool, pool_snapshot=snapshot)


def test_empty_store_marks_every_group_insufficient_and_has_no_facts():
    body = advisor_view(card(ExperienceStore(rewriter=rewriter)))
    assert body['advisor_direct']['sample'] == 'insufficient'
    assert body['advisor_direct']['pass_rate'] is None and body['advisor_direct']['dispatches'] == 0
    assert set(body['pool']) == {'low', 'mid'}
    assert all(entry['sample'] == 'insufficient' for entry in body['pool'].values())
    assert body['facts'] == []
    assert body['scope'] == SCOPE and body['pool_snapshot'] == SNAPSHOT


def test_advisor_pass_rate_shown_from_five_verified_direct_answers():
    store = ExperienceStore(rewriter=rewriter)
    verdicts = ['passed', 'passed', 'passed', 'failed']
    for i, verdict in enumerate(verdicts):
        record(store, f't{i}', [outcome(ADVISOR, verdict, role='advisor', cost='0')])
    assert advisor_view(card(store))['advisor_direct']['sample'] == 'insufficient'
    record(store, 't9', [outcome(ADVISOR, 'passed', role='advisor', cost='0')])
    direct = advisor_view(card(store))['advisor_direct']
    assert direct['sample'] == 'sufficient'
    assert direct['verified'] == 5 and direct['passed'] == 4 and direct['pass_rate'] == '0.8'


def test_unverifiable_tasks_are_counted_separately_and_not_as_samples():
    store = ExperienceStore(rewriter=rewriter)
    for i in range(4):
        record(store, f'p{i}', [outcome(POOL['mid'], 'passed', cost='0.02')])
    for i in range(3):
        record(store, f'u{i}', [outcome(POOL['mid'], 'unverifiable', cost='0.02')])
    mid = advisor_view(card(store))['pool']['mid']
    assert mid['dispatches'] == 7 and mid['unverifiable'] == 3 and mid['verified'] == 4
    assert mid['sample'] == 'insufficient' and mid['pass_rate'] is None
    assert mid['mean_cost_usd'] == '0.02'


def test_pool_stats_show_dispatches_pass_rate_and_mean_cost_per_setting():
    store = ExperienceStore(rewriter=rewriter)
    for i in range(5):
        record(store, f't{i}', [outcome(POOL['low'], 'failed' if i < 2 else 'passed', cost='0.01')])
    body = advisor_view(card(store))
    assert body['pool']['low']['pass_rate'] == '0.6' and body['pool']['low']['dispatches'] == 5
    assert body['pool']['low']['mean_cost_usd'] == '0.01'
    assert body['pool']['mid']['dispatches'] == 0


def test_stats_do_not_carry_over_after_pool_snapshot_change():
    store = ExperienceStore(rewriter=rewriter)
    for i in range(6):
        record(store, f't{i}', [outcome(POOL['low'], 'passed')])
    assert advisor_view(card(store))['pool']['low']['sample'] == 'sufficient'
    after = advisor_view(card(store, snapshot='snapshot-20261201-auto'))['pool']['low']
    assert after['dispatches'] == 0 and after['sample'] == 'insufficient'


def test_stats_do_not_carry_over_after_model_version_change():
    store = ExperienceStore(rewriter=rewriter)
    for i in range(6):
        record(store, f't{i}', [outcome(POOL['low'], 'passed')])
    upgraded = {**POOL, 'low': {**POOL['low'], 'model_version': 'example/low-2'}}
    assert advisor_view(card(store, pool=upgraded))['pool']['low']['dispatches'] == 0
    reprovided = {**POOL, 'low': {**POOL['low'], 'provider_name': 'Other Host'}}
    assert advisor_view(card(store, pool=reprovided))['pool']['low']['dispatches'] == 0


FACT = {'kind': 'check_code', 'code': 'integer_sum_contract_failed'}


def test_solving_facts_appear_on_card_and_in_a_versioned_reference_block():
    store = ExperienceStore(rewriter=rewriter)
    for i in range(3):
        record(store, f't{i}', supports=[FACT])
    made = card(store)
    assert advisor_view(made)['facts'] == [FACT]
    block = facts_block(made)
    assert block['id'] == 'experience-' + advisor_view(made)['card']
    assert block['rendered'].startswith('\n\n[參考區塊 experience-ec-')
    assert 'check_code: integer_sum_contract_failed' in block['text']
    # 作答模型的區塊只有解題事實，沒有任何成績。
    assert 'pass_rate' not in block['text'] and 'dispatches' not in block['text']
    assert facts_block(card(ExperienceStore(rewriter=rewriter))) is None


def test_shared_facts_use_the_rewritten_statement():
    store = ExperienceStore(rewriter=rewriter)
    for i, client in enumerate(('c1', 'c2', 'c3')):
        record(store, f't{i}', supports=[FACT], client=client)
    block = facts_block(card(store, client='c4'))
    assert rewriter(FACT) in block['text']


def test_card_never_exceeds_two_kilobytes_and_drops_whole_facts_from_the_end():
    store = ExperienceStore(rewriter=rewriter)
    many = [{'kind': 'error_fingerprint', 'code': f'sql.very_long_error_fingerprint_code_{n:03d}'}
            for n in range(60)]
    for i in range(3):
        record(store, f't{i}', supports=many)
    made = card(store)
    view = advisor_view(made)
    encoded = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    assert len(encoded) <= MAX_CARD_BYTES
    assert 0 < len(view['facts']) < 60 and view['facts_omitted'] == 60 - len(view['facts'])
    assert view['facts'] == sorted(many, key=lambda f: f['code'])[:len(view['facts'])]


def test_client_without_shared_use_sees_only_its_own_stats():
    store = ExperienceStore(rewriter=rewriter)
    for i in range(5):
        record(store, f'o{i}', [outcome(POOL['low'], 'passed')], client='other')
    store.set_client_settings('c1', use=False)
    assert advisor_view(card(store))['pool']['low']['dispatches'] == 0
    assert advisor_view(card(store, client='c2'))['pool']['low']['dispatches'] == 5
