import json

import httpx

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.local import LocalBackend


class TextContract:
    def tools_schema(self): return []
    def validate(self, text): return {'passed': text == '2'}


def local_engine(tmp_path, local_handler, cloud_handler=None, **config_changes):
    config = dict(model_version='fixture-model-v1', quantization='Q4_K_M', engine='fixture-engine-v1',
                  execution_location='home:fixture', endpoint='http://127.0.0.1:8080/v1/chat/completions',
                  no_model_fee_evidence='fixture://operator-owned-engine-no-fee-v1')
    config.update(config_changes)
    local = LocalBackend(httpx.Client(transport=httpx.MockTransport(local_handler)), tmp_path / 'local', **config)
    profiles, decision = example_config()
    profiles['open_source'].update(backend='local', effort='none', model=config['model_version'], model_version=config['model_version'],
        accepted_models=[config['model_version']], quantization=config['quantization'], engine=config['engine'],
        execution_location=config['execution_location'], external_score=None)
    profiles['open_source'].pop('provider')
    profiles['open_source'].pop('provider_name')
    def no_cloud(req): raise AssertionError('Unexpected cloud request')
    cloud = Client(httpx.Client(transport=httpx.MockTransport(cloud_handler or no_cloud)), 'fixture-key', tmp_path / 'cloud')
    return Core(cloud, profiles, decision, local_backend=local)


def response(text='2', **changes):
    data = {'model': 'fixture-model-v1', 'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 1}}
    data.update(changes)
    return httpx.Response(200, json=data)


def test_local_pass_has_zero_classifier_and_real_local_provenance(tmp_path):
    requests = []
    def local(req):
        requests.append(req)
        assert 'authorization' not in req.headers
        return response()
    engine = local_engine(tmp_path, local)
    result = engine.run('local-pass', {'prompt': 'Return 1+1'}, TextContract, contract_version='public-v1')
    assert result['status'] == 'passed'
    assert result['selection'] is None
    assert result['costs']['api_usd'] == '0'
    assert result['costs']['host_and_human_usd'] is None
    call = result['attempts'][0]['calls'][0]
    assert call['generation_id'] is None and call['provider'] is None
    assert call['local_call_id'] == 'run-local-pass-a0-t0'
    assert call['quantization'] == 'Q4_K_M'
    assert call['engine'] == 'fixture-engine-v1'
    assert call['execution_location'] == 'home:fixture'
    assert call['cost_source'] == 'local_no_model_fee_contract'
    assert len(requests) == 1
    assert json.loads(requests[0].content)['stream'] is False
    assert engine.run('local-pass', {'prompt': 'Return 1+1'}, TextContract, contract_version='public-v1') == result
    assert len(requests) == 1


def test_missing_local_usage_is_unknown_resource_not_unknown_model_fee(tmp_path):
    from cloudyfan_jev.ledger import summarize
    engine = local_engine(tmp_path, lambda req: response(usage=None))
    result = engine.run('missing-usage', {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')
    call = result['attempts'][0]['calls'][0]
    assert call['usage_unknown'] is True and call['usage'] == {}
    assert call['host_usd'] is None and call['electricity_usd'] is None
    ledger = summarize(engine.local_backend)
    assert ledger['total_api_usd'] == '0'
    assert ledger['rows'][0]['local_call_id'] == 'run-missing-usage-a0-t0'
    assert ledger['rows'][0]['generation_id'] is None


def test_local_failure_uses_binary_cloud_repair_without_cloud_reconciliation(tmp_path):
    seen = []
    def cloud(req):
        seen.append(req)
        assert req.method == 'POST'
        payload = json.loads(req.content)
        if req.url.path.endswith('/decisions'):
            return httpx.Response(200, json={'id': 'gen-local-decision', 'model': payload['model'], 'provider': 'TypeSafe',
                'usage': {'cost': 0.001, 'input_tokens': 10, 'output_tokens': 1}, 'answers': {'low_mid': {'type': 'choice', 'choice': 'low',
                'probabilities': {'low': .9, 'mid': .1}}}})
        return httpx.Response(200, json={'id': 'gen-local-repair', 'model': payload['model'], 'provider': 'Example',
            'usage': {'cost': .01}, 'choices': [{'message': {'role': 'assistant', 'content': '2'}, 'finish_reason': 'stop'}]})
    engine = local_engine(tmp_path, lambda req: response('3'), cloud)
    result = engine.run('repair', {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'low']
    assert result['status'] == 'passed' and result['costs']['api_usd'] == '0.011'
    assert result['costs']['classifier_usd'] == '0.001'
    assert len(seen) == 2


def test_cloud_unknown_charge_remains_a_stop_after_local_failure(tmp_path):
    import pytest
    requests = []
    def cloud(req):
        requests.append(req)
        return httpx.Response(200, json={'model': 'typesafe/jev-1.13', 'usage': {}})
    engine = local_engine(tmp_path, lambda req: response('3'), cloud)
    with pytest.raises(RuntimeError, match='incomplete billing'):
        engine.run('unknown', {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')
    assert len(requests) == 1


def test_bad_local_identity_and_timeout_are_saved_never_resent(tmp_path):
    import pytest
    for name in ('wrong-model', 'timeout', 'malformed'):
        seen = []
        def bad(req):
            seen.append(req)
            if name == 'timeout': raise httpx.ReadTimeout('fixture interruption')
            if name == 'malformed': return httpx.Response(200, text='not-json')
            return response(model='unexpected-version')
        engine = local_engine(tmp_path / name, bad)
        for _ in range(2):
            with pytest.raises(RuntimeError, match='Local execution stopped'):
                engine.run(name, {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')
        assert len(seen) == 1
        saved = engine.local_backend.root / 'calls' / f'run-{name}-a0-t0' / 'response.json'
        assert saved.exists()


def test_resuming_changed_local_quantization_is_rejected(tmp_path):
    import pytest
    engine = local_engine(tmp_path, lambda req: response())
    engine.run('frozen', {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')
    changed = local_engine(tmp_path, lambda req: pytest.fail('Should not execute'), quantization='Q8_0')
    with pytest.raises(ValueError, match='Resume evidence changed'):
        changed.run('frozen', {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')


def test_local_profile_cannot_inherit_unqualified_base_model_score(tmp_path):
    import pytest
    engine = local_engine(tmp_path, lambda req: response())
    profiles = engine.profiles
    profiles['open_source'].update(external_score=99, quantization_qualification={'approved': True, 'model_version': 'other'})
    with pytest.raises(ValueError, match='qualification'):
        Core(engine.client, profiles, engine.decision, local_backend=engine.local_backend)


def test_local_subset_rejects_unsupported_reasoning_before_request(tmp_path):
    import pytest
    engine = local_engine(tmp_path, lambda req: response())
    profiles = engine.profiles
    profiles['open_source']['effort'] = 'high'
    with pytest.raises(ValueError, match='reasoning'):
        Core(engine.client, profiles, engine.decision, local_backend=engine.local_backend)
