import json

from cloudyfan_jev.scores.metrics import normalize_benchmarks, normalize_endpoints
import pytest

NOW = '2026-10-20T00:00:00+00:00'


def test_benchmark_preserves_price_without_inventing_provider_speed_or_effort():
    raw = json.dumps({'meta': {'version': 'v1', 'source': 'artificial-analysis', 'as_of': NOW},
        'data': [{'source': 'artificial-analysis', 'model_permaslug': 'example/model',
                  'intelligence_index': 80, 'pricing': {'prompt': '0.000001', 'completion': '0.000002'}}]}).encode()
    row = normalize_benchmarks(raw, retrieved_at=NOW)[0]
    assert row['quality'] == {'intelligence_index': 80}
    assert row['pricing']['prompt'] == '0.000001' and row['price_unit'] == 'USD/token'
    assert row['speed'] is None and row['provider'] is None and row['effort'] is None


def test_design_arena_time_remains_mean_and_null_price_remains_unknown():
    raw = json.dumps({'meta': {'version': 'v1', 'source': 'design-arena', 'as_of': NOW},
        'data': [{'source': 'design-arena', 'model_permaslug': 'example/model',
                  'elo': 1200, 'win_rate': .7, 'avg_generation_time_ms': 250, 'pricing': None}]}).encode()
    row = normalize_benchmarks(raw, retrieved_at=NOW)[0]
    assert row['speed'] == {'metric': 'generation_time_ms', 'value': 250, 'statistic': 'mean'}
    assert row['pricing'] is None and row['provider'] is None


def endpoint_body():
    return {'data': {'id': 'example/model', 'endpoints': [{'tag': 'example/fp4/us',
        'name': 'Example | example/model-20261020', 'provider_name': 'Example', 'quantization': 'fp4',
        'pricing': {'prompt': '0.001', 'completion': '0.002', 'input_cache_write': '0.003',
                    'overrides': [{'min_prompt_tokens': 1000, 'prompt': '0.004'}]},
        'latency_last_30m': None, 'throughput_last_30m': 50}]}}


def test_endpoint_preserves_provider_cache_tiers_and_distinct_speed_units():
    row = normalize_endpoints(json.dumps(endpoint_body()).encode(), retrieved_at=NOW)[0]
    assert row['provider'] == 'example/fp4/us' and row['quantization'] == 'fp4'
    assert row['pricing']['input_cache_write'] == '0.003'
    assert row['pricing']['overrides'][0]['prompt'] == '0.004'
    assert row['speed']['latency_last_30m'] is None and row['speed']['statistic'] is None
    assert row['speed']['units']['throughput_last_30m'] == 'tokens/second'
    assert row['live_identity_verified'] is False


def test_conflicting_endpoint_model_identity_cannot_borrow_outer_identity():
    body = endpoint_body()
    body['data']['endpoints'][0]['model_id'] = 'example/different-model'
    with pytest.raises(ValueError, match='identity'):
        normalize_endpoints(json.dumps(body).encode(), retrieved_at=NOW)


@pytest.mark.parametrize('mutation', ['nan', 'bool', 'negative_price', 'duplicate', 'private'])
def test_unsafe_or_ambiguous_endpoint_metrics_are_rejected(mutation):
    body = endpoint_body()
    endpoint = body['data']['endpoints'][0]
    if mutation == 'nan': endpoint['throughput_last_30m'] = float('nan')
    if mutation == 'bool': endpoint['throughput_last_30m'] = True
    if mutation == 'negative_price': endpoint['pricing']['prompt'] = '-1'
    if mutation == 'duplicate': body['data']['endpoints'].append(endpoint)
    if mutation == 'private': endpoint['hidden_answers'] = ['secret']
    with pytest.raises(ValueError):
        normalize_endpoints(json.dumps(body).encode(), retrieved_at=NOW)


def test_authenticated_endpoint_percentiles_preserve_units_and_provider_scope():
    body=endpoint_body()
    body['data']['endpoints'][0].update(latency_last_30m={'p50':250,'p75':500,'p90':900,'p99':2000},
        throughput_last_30m={'p50':100,'p75':80,'p90':50,'p99':10})
    row=normalize_endpoints(json.dumps(body).encode(),retrieved_at=NOW)[0]
    assert row['speed']['latency_last_30m']['p50']==250
    assert row['speed']['units']['latency_last_30m']=='milliseconds'
    assert row['speed']['statistic']=='percentiles'
    assert row['speed']['throughput_last_30m']['p99']==10


@pytest.mark.parametrize('bad',[True,-1,float('nan'),{'p50':-1},{'p99':True},{'p50':'250'}])
def test_authenticated_bad_speed_never_becomes_valid_evidence(bad):
    body=endpoint_body();body['data']['endpoints'][0]['latency_last_30m']=bad
    with pytest.raises(ValueError):normalize_endpoints(json.dumps(body).encode(),retrieved_at=NOW)


def test_native_openrouter_benchmarks_remain_separate_by_type_and_search_configuration():
    rows=[{'source':'openrouter','model_permaslug':'example/model','benchmark_type':'gpqa_diamond',
           'accuracy':.8,'accuracy_stddev':.03,'avg_cost_per_task':.02,'total_tasks':198,'last_run_timestamp':NOW},
          {'source':'openrouter','model_permaslug':'example/model','benchmark_type':'search_widesearch',
           'primary_metric':'f1_by_item','primary_score':.6,'avg_cost_per_task':.5,
           'avg_latency_per_task_ms':2500,'total_tasks':100,'last_run_timestamp':NOW,
           'search_engine':'engine','search_surface':'server-tool',
           'run_config':{'max_agent_turns':25,'reasoning_effort':'high','temperature':None}}]
    data=normalize_benchmarks(json.dumps({'meta':{'version':'v1','as_of':NOW},'data':rows}).encode(),retrieved_at=NOW)
    assert len(data)==2 and data[0]['quality']=={'accuracy':.8}
    assert data[0]['benchmark_cost']['unit']=='USD/task'
    assert data[0]['benchmark_cost']['value']=='0.02' and data[0]['pricing'] is None
    assert data[1]['quality']=={'f1_by_item':.6}
    assert data[1]['effort']=='high' and data[1]['search_engine']=='engine'
    assert data[1]['speed']=={'metric':'task_latency_ms','value':2500,'statistic':'mean'}
    assert data[1]['run_config']['max_agent_turns']==25


def test_real_endpoint_public_capability_flags_and_scheduled_pricing_are_accepted():
    body=endpoint_body();e=body['data']['endpoints'][0]
    e.update(supports_image_reference=True,supports_multiple_audio_references=False)
    e['pricing']['discount']=-.2
    e['pricing']['overrides']=[{'utc_days':['monday'],'utc_start':100,'utc_end':400,'prompt':'0.004'}]
    row=normalize_endpoints(json.dumps(body).encode(),retrieved_at=NOW)[0]
    assert row['pricing']['discount']==-.2 and row['pricing']['overrides'][0]['utc_start']==100


def test_mixed_scalar_and_percentile_speed_has_per_field_statistics():
    from cloudyfan_jev.scores.metrics import normalize_provider_speed
    s=normalize_provider_speed({'latency_last_30m':25,'throughput_last_30m':{'p50':20}})
    assert s['latency_unit'] is None and s['statistic']=='mixed'
    assert s['statistics']=={'latency_last_30m':None,'throughput_last_30m':'percentiles'}


def test_search_configurations_are_not_collapsed_into_one_model_result():
    rows=[{'source':'openrouter','model_permaslug':'example/model','benchmark_type':'search_hle',
           'primary_metric':'accuracy','primary_score':.5,
           'run_config':{'max_agent_turns':turns,'reasoning_effort':'high'}} for turns in (10,25)]
    data=normalize_benchmarks(json.dumps({'meta':{'version':'v1'},'data':rows}).encode(),retrieved_at=NOW)
    assert len(data)==2 and data[0]['run_config']['max_agent_turns']==10


def test_source_variant_labels_preserved_and_conflicting_identical_labels_quarantined_explicitly():
    rows=[{'source':'design-arena','model_permaslug':'example/model','display_name':label,
           'arena':'models','category':'website','elo':score} for label,score in [('Model Max',1500),('Model High',1400),('Model High',1300)]]
    raw=json.dumps({'meta':{'version':'v1'},'data':rows}).encode()
    with pytest.raises(ValueError,match='duplicate'):normalize_benchmarks(raw,retrieved_at=NOW)
    data=normalize_benchmarks(raw,retrieved_at=NOW,quarantine_ambiguous=True)
    assert len(data)==3 and data[0]['source_variant_label']=='Model Max'
    assert data[0]['effort'] is None and data[0]['binding_status']=='unreviewed_source_variant'
    assert [r['binding_status'] for r in data[1:]]==['ambiguous_source_identity']*2


@pytest.mark.parametrize('config',[{'max_agent_turns':True},{'max_agent_turns':0},{'reasoning_effort':''},{'temperature':float('nan')}])
def test_bad_search_configuration_cannot_establish_model_setting(config):
    row={'source':'openrouter','model_permaslug':'example/model','benchmark_type':'search_hle',
         'primary_metric':'accuracy','primary_score':.5,'run_config':config}
    with pytest.raises(ValueError):normalize_benchmarks(json.dumps({'meta':{'version':'v1'},'data':[row]}).encode(),retrieved_at=NOW)


def test_explicitly_unpublished_search_run_config_stays_unknown():
    row={'source':'openrouter','model_permaslug':'example/model','benchmark_type':'search_hle',
         'primary_metric':'accuracy','primary_score':.5,
         'run_config':{'max_agent_turns':None,'reasoning_effort':None,'temperature':None}}
    data=normalize_benchmarks(json.dumps({'meta':{'version':'v1'},'data':[row]}).encode(),retrieved_at=NOW)
    assert data[0]['run_config']['max_agent_turns'] is None and data[0]['effort'] is None
