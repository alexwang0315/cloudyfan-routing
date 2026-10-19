"""First-release inputs and admission, through public preparation/run seams."""
from copy import deepcopy
import json

import httpx
import pytest

from cloudyfan_jev.scores.demo import example_inputs
from cloudyfan_jev.scores.adapters import import_codexradar
from cloudyfan_jev.scores.catalog import digest

NOW = '2026-10-20T00:00:00+00:00'


def profile():
    imported, registry, policy, requirements, decision = example_inputs(NOW)
    for tier, entry in zip(('low', 'mid', 'high'), registry['candidates'], strict=True):
        entry['intended_tier'] = tier
    observations = import_codexradar(json.dumps(imported).encode(), retrieved_at=NOW)
    return {'schema_version': 'cloudyfan-first-pool-profile-v1', 'synthetic': False,
            'source_choice': {'reason': 'Synthetic native-scale fixture', 'archive_sha256': 'a' * 64},
            'observations': observations, 'registry': registry, 'policy': policy,
            'requirements': requirements, 'decision': decision, 'review': {},
            'configuration_evidence': {}}


def witnessed_profile():
    from cloudyfan_jev.scores.first_pool import configuration_digest
    value = profile()
    value['synthetic'] = True
    registry = value['registry']
    for entry in [registry['open_source'], *registry['candidates']]:
        value['configuration_evidence'][entry['config_id']] = {
            'configuration_sha256': configuration_digest(entry),
            'mapping': {'status': 'approved', 'evidence': ['fixture://exact-identity'],
                        **{k: entry[k] for k in ('source', 'source_version', 'source_model_id')}},
            'capabilities': {'basis': 'independent_calibration', 'operations': entry['capabilities'],
                             'context_tokens': 8192, 'evidence': ['fixture://capability']}}
    proof = {'status': 'approved', 'reviewer': 'AI-fixture-not-human',
             'reviewed_at': NOW, 'evidence': ['fixture://synthetic-review']}
    from cloudyfan_jev.scores.first_pool import prepare_pool
    input_sha256 = prepare_pool(value, now=NOW)['qualification_pack']['input_sha256']
    value['review'] = {'input_sha256': input_sha256,
                       **{k: deepcopy(proof) for k in ('domain', 'source', 'thresholds', 'coverage', 'expiry', 'access')},
                       'models': {e['config_id']: deepcopy(proof) for e in [registry['open_source'], *registry['candidates']]}}
    return value


def test_preparation_keeps_scores_but_excludes_unwitnessed_configurations():
    from cloudyfan_jev.scores.first_pool import prepare_pool
    source = profile()
    original = deepcopy(source)
    report = prepare_pool(source, now=NOW)
    assert source == original
    assert report['status'] == 'blocked'
    assert report['production_ready'] is False
    assert 'model:low:mapping_unproven' in report['blockers']
    assert 'model:low:capabilities_unproven' in report['blockers']
    assert report['qualification_pack']['inputs']['observations'][0]['score'] == 60
    assert report['qualification_pack']['inputs']['registry']['candidates'][0]['approved'] is False
    assert report['candidates'][0]['config_id'] == 'open_source'
    assert report['source_choice'] == source['source_choice']


def test_explicit_synthetic_inputs_can_form_a_test_pool_but_never_production():
    from cloudyfan_jev.scores.first_pool import prepare_pool
    source = witnessed_profile()
    report = prepare_pool(source, now=NOW)
    assert report['status'] == 'synthetic_only'
    assert report['production_ready'] is False
    assert report['blockers'] == []
    source['registry']['candidates'][0]['profile']['effort'] = 'high'
    changed = prepare_pool(source, now=NOW)
    assert changed['status'] == 'blocked'
    assert 'model:low:configuration_evidence_unbound' in changed['blockers']


def test_controlled_entry_rejects_test_qualification_before_any_request(tmp_path):
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.budget import BudgetStore
    from cloudyfan_jev.scores.first_pool import prepare_pool, qualified_pool_core
    report = prepare_pool(witnessed_profile(), now=NOW)
    requests = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r))) as http:
        with pytest.raises(ValueError, match='Synthetic'):
            qualified_pool_core(Client(http, 'mock-only', tmp_path / 'client'), report,
                                run_id='one', budget_store=BudgetStore(tmp_path / 'budget.sqlite'),
                                model_bounds={}, clock=lambda: NOW)
    assert requests == []


@pytest.mark.parametrize('field,value', [('evidence', ['']), ('evidence', 'unstructured'),
                                        ('basis', 'holdout'), ('context_tokens', True)])
def test_capability_proof_cannot_use_empty_evidence_or_holdout(field, value):
    from cloudyfan_jev.scores.first_pool import prepare_pool
    source = witnessed_profile()
    source['configuration_evidence']['low']['capabilities'][field] = value
    assert 'model:low:capabilities_unproven' in prepare_pool(source, now=NOW)['blockers']


def test_cli_materializes_blocked_first_pool_without_publishing(tmp_path, monkeypatch, capsys):
    from cloudyfan_jev.scores import cli
    monkeypatch.setattr(cli, 'utc_now', lambda: NOW)
    src = tmp_path / 'input.json'
    src.write_text(json.dumps(profile()))
    output = tmp_path / 'prepared'
    assert cli.main(['prepare-first-pool', '--profile', str(src), '--output-dir', str(output)]) == 1
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 'blocked'
    assert json.loads((output / 'qualification-pack.json').read_text()) == report['qualification_pack']
    assert json.loads((output / 'fee-gaps.json').read_text())['workflow_upper_bound_usd'] is None
    assert not (output / 'current.json').exists()
    assert cli.main(['prepare-first-pool', '--profile', str(src), '--output-dir', str(output)]) == 1
    assert 'no generation' in capsys.readouterr().err


@pytest.mark.parametrize('mutation', ['report', 'profile'])
def test_controlled_entry_rejects_tampering_even_if_outer_hash_is_recomputed(tmp_path, mutation):
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.budget import BudgetStore
    from cloudyfan_jev.scores.first_pool import prepare_pool, qualified_pool_core
    report = prepare_pool(witnessed_profile(), now=NOW)
    if mutation == 'report':
        report['qualification_pack']['inputs']['registry']['candidates'][0]['approved'] = False
    else:
        report['profile']['registry']['candidates'][0]['profile']['effort'] = 'high'
    report['report_id'] = digest({k: v for k, v in report.items() if k != 'report_id'})
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('No POST allowed'))) as http:
        with pytest.raises(ValueError, match='integrity'):
            qualified_pool_core(Client(http, 'mock-only', tmp_path / 'client'), report,
                                run_id='one', budget_store=BudgetStore(tmp_path / 'budget.sqlite'),
                                model_bounds={}, allow_synthetic=True, clock=lambda: NOW)


def test_archived_profile_keeps_missing_identity_and_sql_evidence_pending():
    from pathlib import Path
    from cloudyfan_jev.scores.first_pool import prepare_pool
    archived = json.loads((Path(__file__).parents[1] / 'examples/first-pool-profile.json').read_text())
    report = prepare_pool(archived, now=NOW)
    assert report['status'] == 'blocked'
    assert report['production_ready'] is False
    assert len(archived['observations']) == 14
    assert next(o['score'] for o in archived['observations']
                if o['source_model_id'] == 'openai/gpt-6-sol' and o['effort'] == 'low') == 35.4
    assert all(e['approved'] is False for e in report['qualification_pack']['inputs']['registry']['candidates'])
    assert archived['registry']['open_source']['catalog_checked_at'] == '2026-10-17T17:18:14.033119+00:00'
    assert 'model:first-open_source:mapping_unproven' in report['blockers']
    low = next(e for e in report['candidates'] if e['config_id'] == 'first-low')
    assert [o['score'] for o in low['score_evidence']] == [35.4]


def test_unknown_existing_charge_blocks_the_qualified_entry(tmp_path):
    from datetime import datetime
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore, EnforcedBound
    from cloudyfan_jev.demo import ArithmeticEnvironment
    from cloudyfan_jev.scores.first_pool import prepare_pool, qualified_pool_core
    source = witnessed_profile()
    report = prepare_pool(source, now=NOW)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime.fromisoformat(NOW))
    store.authorize(Authorization('fixture-only', '1', '2099-01-01T00:00:00+00:00'))
    store.import_history('old-unknown', cost_usd=None)
    bounds = {e['profile']['model_version']: EnforcedBound('.01', 'fixture://cap') for e in
              [source['registry']['open_source'], *source['registry']['candidates']]}
    bounds[source['decision']['version']] = EnforcedBound('.001', 'fixture://cap')
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('Unknown charge must stop POST'))) as http:
        app = qualified_pool_core(Client(http, 'mock-only', tmp_path / 'client'), report,
                                  run_id='one', budget_store=store, model_bounds=bounds,
                                  verify_bound=lambda *args: True, allow_synthetic=True, clock=lambda: NOW)
        with pytest.raises(BudgetError, match='unknown'):
            app.run('one', {'prompt': 'Return 2'}, ArithmeticEnvironment, contract_version='fixture-v1')


@pytest.mark.parametrize('change', ['capability', 'mapping', 'source_choice', 'requirements', 'decision'])
def test_any_admission_evidence_change_requires_a_new_review_binding(change):
    from cloudyfan_jev.scores.first_pool import prepare_pool
    source = witnessed_profile()
    before = prepare_pool(source, now=NOW)
    if change == 'capability':
        source['configuration_evidence']['low']['capabilities']['evidence'] = ['fixture://changed-capability']
    elif change == 'mapping':
        source['configuration_evidence']['low']['mapping']['evidence'] = ['fixture://changed-mapping']
    elif change == 'source_choice':
        source['source_choice']['archive_sha256'] = 'b' * 64
    elif change == 'requirements':
        source['requirements']['context_tokens'] = 1100
    else:
        source['decision']['version'] = 'typesafe/changed-version'
    after = prepare_pool(source, now=NOW)
    assert after['status'] == 'blocked'
    assert 'review:input_binding_mismatch' in after['blockers']
    assert after['qualification_pack']['pack_id'] != before['qualification_pack']['pack_id']
    source['review']['input_sha256'] = after['qualification_pack']['input_sha256']
    assert prepare_pool(source, now=NOW)['status'] == 'synthetic_only'
