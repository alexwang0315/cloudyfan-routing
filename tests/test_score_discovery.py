"""Broad discovery is read-only and cannot manufacture runtime qualification."""
import json
from copy import deepcopy

import httpx
import pytest

from cloudyfan_jev.scores.discovery import catalog_inventory, build_proposal, refresh

NOW = '2026-10-20T12:00:00+00:00'
POLICY = {'id': 'discovery-test-v1', 'metric': 'coding_index', 'low_mid': 40,
          'mid_high': 70, 'input_tokens': 2000, 'output_tokens': 1000,
          'minimum_price_gap_fraction': '0.20', 'ttl_hours': 24}


def model(name='example/low', score=30, cost='0.000001'):
    return {'id': name, 'canonical_slug': name + '-20261020', 'context_length': 32768,
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
            'pricing': {'prompt': cost, 'completion': cost},
            'benchmarks': {'artificial_analysis': {'coding_index': score}},
            'reasoning': {'supported_efforts': ['low', 'high'], 'default_effort': 'high'},
            'hugging_face_id': None}


def raw(rows):
    return json.dumps({'data': rows}).encode()


def endpoint(name, price, latency=None, provider='p'):
    return json.dumps({'data': {'id': name, 'endpoints': [
        {'model_id': name, 'tag': provider, 'provider_name': provider, 'pricing': {
            'prompt': price, 'completion': price}, 'latency_last_30m': latency,
         'throughput_last_30m': None, 'supports_image_reference': True}]}}).encode()


def test_inventory_keeps_missing_quality_separate_from_low_and_native_boundaries():
    inv = catalog_inventory(raw([model(score=None), model('example/a', 40), model('example/b', 70)]),
                            retrieved_at=NOW)
    p = build_proposal(inv, {}, POLICY)
    assert p['unassigned'][0]['model_id'] == 'example/low'
    assert [c['model_id'] for c in p['pools']['mid']] == ['example/a', 'example/b']
    assert not p['pools']['low'] and not p['pools']['high']
    assert p['production_activated'] is False
    assert p['pools']['mid'][0]['quality']['effort'] is None


def test_pool_is_multi_candidate_and_cost_gap_can_remove_overpriced_low_without_jev():
    rows = [model('example/l1', 30), model('example/l2', 35, '0.0000005'),
            model('example/l3', 20, '0.00001'), model('example/m', 60, '0.00001')]
    p = build_proposal(catalog_inventory(raw(rows), retrieved_at=NOW), {}, POLICY)
    assert len(p['pools']['low']) == 3
    assert next(c for c in p['pools']['low'] if c['model_id']=='example/l3')['economics']['status'] == 'gap_not_demonstrated'
    assert p['pools']['low'][0]['economic_qualification'] == 'pending_full_path_and_classifier_evidence'


def test_no_model_name_heuristics_for_open_weights_and_free_price_not_total_guarantee():
    a = model('example/opensource:free', 30, '0')
    b = model('example/weights', None); b['hugging_face_id'] = 'example/weights'
    p = build_proposal(catalog_inventory(raw([a,b]), retrieved_at=NOW), {}, POLICY)
    assert [c['model_id'] for c in p['pools']['oss']] == ['example/weights']
    assert p['pools']['oss'][0]['open_weight_license_verified'] is False
    assert p['pools']['low'][0]['zero_token_price'] is True
    assert p['pools']['low'][0]['zero_total_fee_guarantee'] is False


def test_batch_expired_and_multimodal_output_stay_inventory_not_synchronous_pools():
    rows=[model('example/a:batch'),model('example/b'),model('example/c')]
    rows[1]['expiration_date']='2026-10-19T00:00:00Z'
    rows[2]['architecture']['output_modalities']=['text','image']
    p=build_proposal(catalog_inventory(raw(rows),retrieved_at=NOW),{},POLICY)
    assert len(p['excluded'])==3 and not any(p['pools'].values())


def test_endpoint_cost_and_speed_never_cross_provider_and_unknown_remains_unknown():
    inv=catalog_inventory(raw([model()]),retrieved_at=NOW)
    e=json.loads(endpoint('example/low','0.000002',2))
    e['data']['endpoints'].append({'tag':'q','pricing':{'prompt':'0.000001','completion':'0.000001'},
                                 'latency_last_30m':None,'throughput_last_30m':None})
    p=build_proposal(inv,{'example/low':{'status':200,'raw':json.dumps(e).encode()}},POLICY)
    c=p['pools']['low'][0]
    assert c['providers'][0]['scenario_usd']=='0.006000'
    assert c['providers'][1]['speed']['latency_last_30m'] is None
    assert c['providers'][0]['speed']['latency_last_30m']==2
    assert c['providers'][0]['speed']['statistic'] is None


def test_scheduled_price_preserved_and_not_ranked_as_flat_complete_cost():
    m=model();m['pricing']['overrides']=[{'utc_days':['monday'],'utc_start':0,'utc_end':100,'prompt':'0'}]
    p=build_proposal(catalog_inventory(raw([m]),retrieved_at=NOW),{},POLICY)
    c=p['pools']['low'][0]
    assert c['catalog_price']['overrides']==m['pricing']['overrides']
    assert c['catalog_scenario_usd'] is None
    assert c['economics']['status']=='unknown_price_or_next_pool'


@pytest.mark.parametrize('change',['duplicate','negative','nan','bool','private'])
def test_bad_model_rows_cannot_silently_contaminate_candidate_data(change):
    m=model();rows=[m]
    if change=='duplicate':rows.append(deepcopy(m))
    if change=='negative':m['pricing']['prompt']='-1'
    if change=='nan':m['benchmarks']['artificial_analysis']['coding_index']=float('nan')
    if change=='bool':m['benchmarks']['artificial_analysis']['coding_index']=True
    if change=='private':m['benchmarks']['artificial_analysis']['hidden_answer']='secret'
    with pytest.raises(ValueError):catalog_inventory(raw(rows),retrieved_at=NOW)


def test_refresh_get_only_no_inherited_auth_and_records_endpoint_429_without_retry(tmp_path):
    seen=[]
    def handler(req):
        seen.append(req)
        assert req.method=='GET' and 'authorization' not in req.headers and 'cookie' not in req.headers
        if req.url.path.endswith('/models'):return httpx.Response(200,content=raw([model()]))
        return httpx.Response(429,json={'error':{'message':'private upstream text'}})
    with httpx.Client(transport=httpx.MockTransport(handler),headers={'Authorization':'secret','Cookie':'secret'}) as c:
        p=refresh(c,tmp_path/'snapshot',POLICY,now=NOW)
    assert len(seen)==2
    assert p['pools']['low'][0]['endpoint_status']==429
    assert 'private upstream text' not in (tmp_path/'snapshot'/'proposal.json').read_text()
    assert p['requests']['post']==0 and p['production_activated'] is False
    with pytest.raises(FileExistsError):
        refresh(c,tmp_path/'snapshot',POLICY,now=NOW)


def test_refresh_diff_tracks_removed_models_without_mutating_previous(tmp_path):
    old=build_proposal(catalog_inventory(raw([model('example/old')]),retrieved_at=NOW),{},POLICY)
    before=deepcopy(old)
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=raw([])))) as c:
        p=refresh(c,tmp_path/'next',POLICY,now=NOW,previous=old)
    assert p['changes']['removed']==['example/old'] and old==before


def test_conflicting_endpoint_identity_not_used_and_http_redirect_not_followed(tmp_path):
    def handler(req):
        if req.url.path.endswith('/models'):return httpx.Response(200,content=raw([model()]))
        return httpx.Response(302,headers={'location':'https://evil.example/steal'})
    with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True) as c:
        p=refresh(c,tmp_path/'snap',POLICY,now=NOW)
    assert p['requests']['get']==2 and p['pools']['low'][0]['endpoint_status']==302
    inv=catalog_inventory(raw([model()]),retrieved_at=NOW)
    p=build_proposal(inv,{'example/low':{'status':200,'raw':endpoint('wrong/model','0')}},POLICY)
    assert p['pools']['low'][0]['endpoint_parse_error']=='invalid_endpoint_source'
    assert not p['pools']['low'][0]['providers']


def test_invalid_policy_or_future_expiry_shape_fails_closed():
    inv=catalog_inventory(raw([model()]),retrieved_at=NOW)
    with pytest.raises(ValueError):build_proposal(inv,{},dict(POLICY,low_mid=80))
    with pytest.raises(ValueError):build_proposal(inv,{},dict(POLICY,input_tokens=-1))


def test_virtual_latest_routes_retained_but_excluded_from_fixed_endpoint_discovery():
    inv=catalog_inventory(raw([model('~openai/gpt-luna-latest')]),retrieved_at=NOW)
    p=build_proposal(inv,{},POLICY)
    assert p['excluded']==[{'model_id':'~openai/gpt-luna-latest','reason':'virtual_unpinned_route'}]


def test_real_provider_discount_and_dynamic_router_sentinel():
    body=json.loads(endpoint('example/low','0.000001'))
    body['data']['endpoints'][0]['pricing']['discount']=0
    inv=catalog_inventory(raw([model(),model('typesafe/jev-router',None,'-1')]),retrieved_at=NOW)
    p=build_proposal(inv,{'example/low':{'status':200,'raw':json.dumps(body).encode()}},POLICY)
    assert p['pools']['low'][0]['providers'][0]['pricing']['discount']==0
    assert p['excluded'][0]['reason']=='dynamic_priced_router_not_fixed_model'


def test_hosted_price_screen_cannot_remove_openweight_from_local_discovery():
    m=model();m['hugging_face_id']='example/weights'
    inv=catalog_inventory(raw([m,model('example/m',60,'0')]),retrieved_at=NOW)
    p=build_proposal(inv,{},POLICY)
    assert len(p['pools']['oss'])==1
    assert p['pools']['low'][0]['economics']['status']=='gap_not_demonstrated'


def test_empty_hf_and_date_only_expiry_do_not_abort_whole_catalog():
    a=model();a['hugging_face_id']='';a['expiration_date']='2026-10-21'
    b=model('example/old');b['expiration_date']='2026-10-20'
    p=build_proposal(catalog_inventory(raw([a,b]),retrieved_at=NOW),{},POLICY)
    assert len(p['pools']['low'])==1 and p['excluded'][0]['reason']=='expired'


def test_bad_or_duplicate_provider_does_not_destroy_other_valid_endpoints():
    body=json.loads(endpoint('example/low','0.000001'))
    bad=deepcopy(body['data']['endpoints'][0]);bad['tag']='bad';bad['pricing']['prompt']='-2'
    dup=deepcopy(body['data']['endpoints'][0]);dup['tag']='duplicate'
    body['data']['endpoints'] += [bad,dup,deepcopy(dup)]
    inv=catalog_inventory(raw([model()]),retrieved_at=NOW)
    p=build_proposal(inv,{'example/low':{'status':200,'raw':json.dumps(body).encode()}},POLICY)
    c=p['pools']['low'][0]
    assert len(c['providers'])==1 and len(c['provider_rejections'])==3


def test_hash_verified_replay_never_calls_network_and_rejects_corruption(tmp_path):
    from cloudyfan_jev.scores.discovery import replay_capture
    def handler(req):
        return httpx.Response(200,content=raw([model()]) if req.url.path.endswith('/models') else endpoint('example/low','0'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        original=refresh(c,tmp_path/'source',POLICY,now=NOW)
    result=replay_capture(tmp_path/'source',tmp_path/'derived',POLICY,previous=original)
    assert result['requests']['get']==0 and result['requests']['reused_capture_get']==2
    assert result['changes']['changed']==[]
    (tmp_path/'source'/'models.raw').write_bytes(b'{}')
    with pytest.raises(ValueError,match='hash mismatch'):
        replay_capture(tmp_path/'source',tmp_path/'bad',POLICY)


def test_explicit_key_goes_only_to_get_source_and_is_never_persisted(tmp_path):
    def handler(req):
        assert req.headers['Authorization']=='Bearer test-secret'
        return httpx.Response(200,content=raw([]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        refresh(c,tmp_path/'auth',POLICY,now=NOW,api_key='test-secret')
    assert all('test-secret' not in f.read_text() for f in (tmp_path/'auth').iterdir())


def test_rate_limit_stops_refresh_without_silent_retry_or_claiming_complete_coverage(tmp_path):
    seen=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(200,content=raw([model(),model('example/z')])) if len(seen)==1 else httpx.Response(429)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        p=refresh(c,tmp_path/'limited',POLICY,now=NOW)
    assert len(seen)==2
    assert sum(c['endpoint_status'] is None for c in p['candidate_models'])==1


def test_diff_context_and_coverage_changes_but_not_irrelevant_raw_hash():
    from cloudyfan_jev.scores.discovery import _changes
    inv=catalog_inventory(raw([model()]),retrieved_at=NOW)
    original=build_proposal(inv,{'example/low':{'status':200,'raw':endpoint('example/low','0')}},POLICY)
    p=deepcopy(original);p['candidate_models'][0]['providers'][0]['raw_sha256']='a'*64
    _changes(p,original);assert p['changes']['changed']==[]
    p['candidate_models'][0]['context_length']=4096
    _changes(p,original);assert p['changes']['changed']==['example/low']
    p=deepcopy(original);p['candidate_models'][0]['endpoint_status']=429
    _changes(p,original);assert p['changes']['changed']==['example/low']


def test_oversize_source_is_explicitly_truncated_and_request_bound_is_enforced(tmp_path,monkeypatch):
    import cloudyfan_jev.scores.discovery as discovery
    monkeypatch.setattr(discovery,'MAX_BYTES',4)
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'too large'))) as c:
        r=discovery._get(c,discovery.CATALOG_URL)
    assert r['error']=='source_too_large' and r['truncated'] is True and r['bytes_observed']==9
    monkeypatch.setattr(discovery,'MAX_BYTES',1024*1024)
    def handler(req):
        return httpx.Response(200,content=raw([model(),model('example/z')]) if req.url.path.endswith('/models')
                              else endpoint('example/low','0'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        p=refresh(c,tmp_path/'bounded',dict(POLICY,max_endpoint_requests=1),now=NOW)
    assert p['requests']['get']==2
    assert sum(c['endpoint_status'] is None for c in p['candidate_models'])==1


def test_authenticated_percentile_provider_is_not_quarantined_or_converted_to_seconds():
    b=json.loads(endpoint('example/low','0.000001'))
    e=b['data']['endpoints'][0]
    e['latency_last_30m']={'p50':7115.5,'p75':11830,'p90':18263.4,'p99':48201.32}
    e['throughput_last_30m']={'p50':28,'p75':21,'p90':15,'p99':7}
    inv=catalog_inventory(raw([model()]),retrieved_at=NOW)
    p=build_proposal(inv,{'example/low':{'status':200,'raw':json.dumps(b).encode()}},POLICY)
    provider=p['pools']['low'][0]['providers'][0]
    assert provider['speed']['latency_unit']=='milliseconds'
    assert provider['speed']['statistic']=='percentiles'
    assert provider['speed']['latency_last_30m']['p50']==7115.5
    assert p['summary']['provider_rows_with_latency']==1
    assert not p['pools']['low'][0]['provider_rejections']


def test_all_null_percentiles_do_not_count_as_observed_speed():
    b=json.loads(endpoint('example/low','0.000001'));e=b['data']['endpoints'][0]
    e['latency_last_30m']={'p50':None,'p75':None,'p90':None,'p99':None}
    e['throughput_last_30m']={}
    p=build_proposal(catalog_inventory(raw([model()]),retrieved_at=NOW),
                     {'example/low':{'status':200,'raw':json.dumps(b).encode()}},POLICY)
    assert p['summary']['provider_rows']==1
    assert p['summary']['provider_rows_with_latency']==0 and p['summary']['provider_rows_with_throughput']==0
