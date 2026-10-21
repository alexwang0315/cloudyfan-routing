"""Preparation is offline and cannot grant exact model or workflow qualification."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.sql_backup_candidates import (
    CATEGORY, HISTORICAL_TRANSPORT_BLOCKER, POSITIONS, TIERS, prepare_sql_backup_candidates,
)

NOW = '2026-10-21T10:00:00Z'


def inputs():
    pools = []
    for tier in TIERS:
        positions = []
        for index, position in enumerate(POSITIONS):
            model = f'example/{tier}-{index}'
            profile = {'model': model, 'model_version': model, 'accepted_models': [model],
                       'provider': f'provider-{index}', 'provider_name': f'Provider {index}',
                       'effort': 'low', 'quantization': 'fp8', 'endpoint': 'https://example.org/completions'}
            positions.append({'position': position, 'profile': profile})
        pools.append({'category_id': CATEGORY, 'tier': tier, 'positions': positions})
    coverage = {'named_pool_positions': {'pools': pools},
                'source_capture_times': {'catalog': NOW, 'existing_research_valid_until': '2026-10-22T10:00:00Z'},
                'existing_research_admission': {'verification_time': NOW}}
    low = {'category_id': CATEGORY, 'target_pool': 'low', 'created_at': NOW,
           'review_before': '2026-10-22T10:00:00Z', 'candidates': []}
    for row in pools[1]['positions']:
        p = deepcopy(row['profile']); p['effort'] = 'none'
        low['candidates'].append({'position': row['position'], 'entry': {'profile': p},
                                 'request_contract': {'reasoning_field_must_be_absent': True},
                                 'source': {'retrieved_at': NOW},
                                 'qualification': {'blockers': [HISTORICAL_TRANSPORT_BLOCKER, 'real_sql_missing']}})
    return coverage, low


def prepare(values=None, **kwargs):
    return prepare_sql_backup_candidates(*(values or inputs()), now=kwargs.pop('now', NOW), **kwargs)


def at(report, tier, position='primary'):
    slot = next(x for x in report['slots'] if x['tier'] == tier and x['position'] == position)
    return next(c for c in report['configurations'] if c['config_id'] == slot['config_id'])


def proof(config):
    return {'profile': deepcopy(config['profile']), 'configuration_sha256': config['configuration_sha256'],
            'report_id': 'independent-sql-example', 'artifact_sha256': 'a'*64,
            'suite_sha256': 'b'*64, 'grader_sha256': 'c'*64, 'public_contract_sha256': 'd'*64,
            'domain': 'bounded-sqlite-readonly', 'valid_until': '2026-10-22T00:00:00Z',
            'qualification_status': 'passed'}


def test_pure_review_twelve_positions_no_dispatch_and_no_source_mutation():
    values = inputs(); before = deepcopy(values); result = prepare(values)
    assert result['counts'] == {'positions': 12, 'unique_preparation_records': 12,
                                'complete_exact_configurations': 12, 'historical_research_references': 0,
                                'exact_response_contract_compatible': 12,
                                'runtime_dispatch_enabled': 0, 'production_qualified': 0}
    assert not result['runtime_dispatch_enabled'] and result['formal_qualification'] == 'pending'
    assert not result['answers_read'] and not result['model_api_calls']
    assert values == before
    assert result['review_id'] == digest({k:v for k,v in result.items() if k != 'review_id'})


def test_model_label_aliases_cannot_fill_multiple_backup_positions():
    coverage, low = inputs()
    positions = coverage['named_pool_positions']['pools'][0]['positions']
    positions[1]['profile']['model_version'] = positions[0]['profile']['model_version']
    positions[1]['profile']['accepted_models'] = positions[0]['profile']['accepted_models'][:]
    with pytest.raises(ValueError, match='different model routes'):
        prepare((coverage, low))


def test_sql_specific_low_priority_keeps_both_original_orders_and_explains_choice():
    coverage, low = inputs(); low['candidates'][0]['entry']['profile']['model'] = 'example/sql-specific'
    result = prepare((coverage, low)); compare = result['low_proposal_comparison']
    assert at(result, 'low')['profile']['model'] == 'example/sql-specific'
    assert compare['generic_original_order'][0]['model'] == 'example/low-0'
    assert compare['sql_specific_original_order'][0]['model'] == 'example/sql-specific'
    assert 'not measured answer performance' in compare['selection_basis']


def test_none_omit_is_currently_implemented_without_rewriting_historical_blockers():
    config = at(prepare(), 'low')
    assert config['profile']['reasoning_parameter'] == 'omit'
    assert HISTORICAL_TRANSPORT_BLOCKER in config['historical_blockers']
    assert HISTORICAL_TRANSPORT_BLOCKER not in config['current_blockers']
    assert config['current_blockers'] == ['real_sql_missing']
    assert config['transport_implementation']['none_omit_support_implemented']


def test_provider_default_is_not_rewritten_to_none_or_executable_identity():
    values = inputs(); values[1]['candidates'][2]['entry']['profile']['effort'] = 'provider_default'
    config = at(prepare(values), 'low', 'backup_2')
    assert config['profile']['effort'] == 'provider_default'
    assert config['configuration_sha256'] is None
    assert set(config['identity_gaps']) == {'unverified_effort', 'omit_requires_explicit_none_effort'}
    assert config['sql_evidence_comparison']['status'] == 'incomplete_identity_no_reuse'


def test_exact_setting_shared_across_pools_is_tested_once_in_preparation():
    coverage, low = inputs()
    coverage['named_pool_positions']['pools'][0]['positions'][0]['profile'] = {
        **deepcopy(low['candidates'][0]['entry']['profile']), 'reasoning_parameter': 'omit'}
    result = prepare((coverage, low))
    assert result['counts']['unique_preparation_records'] == 11
    assert at(result, 'open_source')['config_id'] == at(result, 'low')['config_id']
    assert len(at(result, 'low')['slots']) == 2


@pytest.mark.parametrize('field,value', [('provider','different-provider'), ('effort','high'),
                                        ('quantization','fp4'), ('model_version','example/date-version'),
                                        ('reasoning_parameter','effort')])
def test_provider_strength_version_quantization_or_wire_change_not_shared(field, value):
    coverage, low = inputs()
    clone = {**deepcopy(low['candidates'][0]['entry']['profile']), 'reasoning_parameter': 'omit'}
    clone[field] = value
    coverage['named_pool_positions']['pools'][0]['positions'][0]['profile'] = clone
    result = prepare((coverage, low))
    assert at(result,'open_source')['config_id'] != at(result,'low')['config_id']


def test_identical_names_with_unknown_effort_are_distinct_unverified_proposals():
    coverage, low = inputs()
    for pool in [coverage['named_pool_positions']['pools'][0], coverage['named_pool_positions']['pools'][2]]:
        pool['positions'][0]['profile'] = {'model':'example/unknown', 'effort':None}
    result = prepare((coverage,low))
    assert at(result,'open_source')['config_id'] != at(result,'mid')['config_id']
    assert at(result,'mid')['configuration_sha256'] is None


def test_matching_exact_metadata_still_needs_contract_and_artifact_review():
    config = at(prepare(),'low'); key=config['configuration_sha256']
    result = prepare(evidence_index={key:proof(config)})
    compare = at(result,'low')['sql_evidence_comparison']
    assert compare['status']=='matching_metadata_requires_contract_review'
    assert compare['automatic_reuse'] is False
    assert result['production_qualified'] is False


def test_same_name_other_provider_evidence_is_not_reused():
    config=at(prepare(),'low'); key=config['configuration_sha256']; p=proof(config)
    p['profile']['provider']='wrong-provider'
    assert at(prepare(evidence_index={key:p}),'low')['sql_evidence_comparison']['status']=='exact_identity_mismatch'


def test_evidence_missing_contract_binding_and_expiration_stay_explicit():
    config=at(prepare(),'low');key=config['configuration_sha256'];p=proof(config)
    p.pop('suite_sha256')
    assert at(prepare(evidence_index={key:p}),'low')['sql_evidence_comparison']['status']=='missing_contract_or_artifact_binding'
    p=proof(config);p['valid_until']=NOW
    assert at(prepare(evidence_index={key:p}),'low')['sql_evidence_comparison']['status']=='expired'


def test_singleton_research_reference_does_not_supply_new_backup_flow_or_classifier():
    coverage, low=inputs();row=coverage['named_pool_positions']['pools'][0]['positions'][0]
    row.update(status='research_admitted_only',config_id='old-example',scope='frozen-only',configuration_sha256='f'*64)
    config=at(prepare((coverage,low)),'open_source')
    assert config['historical_sql_evidence']['status']=='historical_research_reference_only'
    assert config['historical_sql_evidence']['freshness']['status']=='fresh'
    assert config['new_backup_evidence']['old_singleton_calibration_reusable'] is False
    assert config['new_backup_evidence']['whole_flow_cost_speed']=='missing'


def test_stale_source_and_proposal_are_not_refreshed_by_review_time():
    result=prepare(now='2026-10-24T10:00:00Z')
    assert at(result,'mid')['source_freshness']['status']=='expired'
    assert result['low_proposal_comparison']['proposal_freshness']['status']=='expired'
    assert at(result,'mid')['source_freshness']['observed_at']==NOW


def test_free_route_is_retained_and_paid_canonical_is_an_identity_gap():
    coverage,low=inputs(); p=low['candidates'][0]['entry']['profile']
    p.update(model='example/free:free',model_version='example/free:free')
    config=at(prepare((coverage,low)),'low')
    assert config['free_route_must_be_preserved'] and config['profile']['model_version'].endswith(':free')
    p['model_version']='example/free-paid'
    assert 'free_request_route_not_preserved' in at(prepare((coverage,low)),'low')['identity_gaps']


def test_private_fields_are_not_projected_or_used_to_choose_proposals():
    values=inputs(); original=prepare(values)
    values[0]['hidden_answers']='secret-final-answer'
    values[0]['named_pool_positions']['pools'][1]['positions'][0]['profile']['hidden_answers']='secret-final-answer'
    values[1]['candidates'][0]['entry']['profile']['hidden_answers']='secret-final-answer'
    result=prepare(values)
    assert result['slots']==original['slots']
    assert 'secret-final-answer' not in json.dumps(result)
    config=at(result,'low');p=proof(config);p['answers']='secret-final-answer'
    with pytest.raises(ValueError,match='metadata only'):
        prepare(values,evidence_index={config['configuration_sha256']:p})


@pytest.mark.parametrize('change', ['category','positions','same_model'])
def test_invalid_category_duplicate_position_or_same_pool_model_rejected(change):
    values=inputs()
    if change=='category':values[1]['category_id']='other'
    elif change=='positions':values[1]['candidates'][0]['position']='backup_1'
    else:values[1]['candidates'][1]['entry']['profile']['model']=values[1]['candidates'][0]['entry']['profile']['model']
    with pytest.raises(ValueError):prepare(values)


def test_cli_is_offline_and_refuses_to_overwrite_previous_output(tmp_path):
    coverage,low=inputs();a=tmp_path/'coverage.json';b=tmp_path/'low.json';out=tmp_path/'review.json'
    a.write_text(json.dumps(coverage));b.write_text(json.dumps(low))
    cmd=[sys.executable,str(Path(__file__).parents[1]/'scripts/prepare_sql_backup_candidates.py'),
         '--coverage',str(a),'--sql-low-proposal',str(b),'--now',NOW,'--output',str(out)]
    first=subprocess.run(cmd,capture_output=True,text=True)
    assert first.returncode==0,first.stderr
    assert json.loads(out.read_text())['counts']['positions']==12
    before=out.read_bytes();second=subprocess.run(cmd,capture_output=True,text=True)
    assert second.returncode==2 and out.read_bytes()==before


def archive_inputs():
    coverage, low = inputs()
    row = coverage['named_pool_positions']['pools'][0]['positions'][0]
    cid = 'archived-research'
    profile = deepcopy(row['profile'])
    from cloudyfan_jev.scores.configuration import configuration_digest
    chash = configuration_digest({'profile': profile})
    row.update(status='research_admitted_only',config_id=cid,configuration_sha256=chash)
    row['profile'].pop('endpoint');row['profile'].pop('accepted_models')
    coverage['existing_research_admission']['matrix_id'] = '3'*64
    qual = {'configuration_sha256':chash,'parent_measured_report_id':'old-report',
            'report_id':'admission','valid_until':'2026-10-22T10:00:00Z'}
    admission = {'configuration_sha256':chash,'parent_report_id':'old-report','suite_id':'s'*64,
                 'identity_contract_id':'i'*64}
    archive = {'matrix':{'matrix_id':'3'*64,'categories':{CATEGORY:{'pools':{'open_source':[
                {'config_id':cid,'profile':profile,'sql_qualification':qual}]}}},
                'research_measured_admissions':{cid:admission}},
               'qualification_report_ids':{'open_source':'old-report'},'retrieved_at':NOW}
    archive['pack_id']=digest(archive)
    return coverage,low,archive


def test_archived_exact_profile_fills_summary_omissions_after_hash_and_identity_check():
    coverage,low,archive=archive_inputs();result=prepare((coverage,low),archived_pack=archive)
    config=at(result,'open_source')
    assert config['exact_configuration_complete']
    assert config['profile']['endpoint']=='https://example.org/completions'
    assert config['archived_profile_evidence']['parent_report_id']=='old-report'
    assert config['archived_profile_evidence']['suite_id']=='s'*64
    assert config['archived_profile_evidence']['grader_sha256'] is None
    assert not config['archived_profile_evidence']['automatic_reuse']
    assert config['new_backup_evidence']['old_singleton_calibration_reusable'] is False


@pytest.mark.parametrize('bad', ['pack_hash','matrix_id','summary_provider','config_hash','report_binding'])
def test_archived_pack_never_fills_a_conflicting_summary_or_evidence(bad):
    coverage,low,archive=archive_inputs()
    entry=archive['matrix']['categories'][CATEGORY]['pools']['open_source'][0]
    if bad=='pack_hash':archive['retrieved_at']='2026-10-20T10:00:00Z'
    elif bad=='matrix_id':coverage['existing_research_admission']['matrix_id']='4'*64
    elif bad=='summary_provider':coverage['named_pool_positions']['pools'][0]['positions'][0]['profile']['provider']='other'
    elif bad=='config_hash':
        entry['profile']['endpoint']='https://other.example/completions'
        archive['pack_id']=digest({k:v for k,v in archive.items() if k!='pack_id'})
    else:
        entry['sql_qualification']['parent_measured_report_id']='other-report'
        archive['pack_id']=digest({k:v for k,v in archive.items() if k!='pack_id'})
    with pytest.raises(ValueError):prepare((coverage,low),archived_pack=archive)


def test_archived_multiple_response_aliases_are_preserved_but_not_exam_compatible():
    coverage,low,archive=archive_inputs()
    entry=archive['matrix']['categories'][CATEGORY]['pools']['open_source'][0]
    entry['profile']['accepted_models'].append('example/provider-alias')
    from cloudyfan_jev.scores.configuration import configuration_digest
    h=configuration_digest(entry)
    coverage['named_pool_positions']['pools'][0]['positions'][0]['configuration_sha256']=h
    entry['sql_qualification']['configuration_sha256']=h
    archive['matrix']['research_measured_admissions']['archived-research']['configuration_sha256']=h
    archive['pack_id']=digest({k:v for k,v in archive.items() if k!='pack_id'})
    config=at(prepare((coverage,low),archived_pack=archive),'open_source')
    assert config['exact_configuration_complete']
    assert len(config['profile']['accepted_models'])==2
    assert config['configuration_sha256']==h
    assert config['exam_compatibility']['status']=='requires_new_explicit_exact_response_contract'
    assert config['exam_compatibility']['is_exam_dispatch_authority'] is False


def test_cli_archived_pack_must_match_inventory_raw_hash(tmp_path):
    import hashlib
    coverage,low,archive=archive_inputs()
    raw=json.dumps(archive).encode()
    coverage['sources']=[{'name':'prepared_research_v8','sha256':hashlib.sha256(raw).hexdigest()}]
    paths=[tmp_path/name for name in ['coverage.json','low.json','archive.json']]
    paths[0].write_text(json.dumps(coverage));paths[1].write_text(json.dumps(low));paths[2].write_bytes(raw)
    cmd=[sys.executable,str(Path(__file__).parents[1]/'scripts/prepare_sql_backup_candidates.py'),
         '--coverage',str(paths[0]),'--sql-low-proposal',str(paths[1]),'--archived-pack',str(paths[2]),'--now',NOW]
    result=subprocess.run(cmd,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['counts']['complete_exact_configurations']==12
    paths[2].write_bytes(raw+b' ')
    result=subprocess.run(cmd,capture_output=True,text=True)
    assert result.returncode==2 and 'raw hash does not match' in result.stderr
