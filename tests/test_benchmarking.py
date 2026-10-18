import json

import httpx

from cloudyfan_jev.benchmarking import BenchmarkHarness, ModelSetup
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.local import LocalBackend


class Clock:
    value = 0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Contract:
    def tools_schema(self): return []
    def validate(self, text): return {'passed': text == '2'}


def setup():
    return ModelSetup(model_version='fixture-v1', quantization='Q4', engine='fixture-engine',
                      hardware={'device': 'synthetic-device', 'memory_bytes': 1024},
                      resources={'parallel_slots': 1, 'context_tokens': 128})


def make_core(tmp_path, reply):
    profiles, decision = example_config()
    profiles['open_source'].update(model='fixture-v1', model_version='fixture-v1', accepted_models=['fixture-v1'],
        effort='none', backend='local', quantization='Q4', engine='fixture-engine', execution_location='home:fixture', external_score=None)
    local = LocalBackend(httpx.Client(transport=httpx.MockTransport(reply)), tmp_path / 'local',
        endpoint='http://127.0.0.1:8080/v1/chat/completions', model_version='fixture-v1', quantization='Q4',
        engine='fixture-engine', execution_location='home:fixture', no_model_fee_evidence='fixture://no-fee')
    cloud = Client(httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError('cloud')))),
                   'fixture-key', tmp_path / 'cloud')
    return Core(cloud, profiles, decision, local_backend=local)


def test_synthetic_load_and_generation_report_preserves_unknown_costs(tmp_path):
    clock = Clock()
    observer = {}
    def prepare(emit):
        observer['emit'] = emit
        emit('load_start'); clock.advance(2); emit('load_end')
    def reply(req):
        emit = observer['emit']
        emit('queue_start'); clock.advance(1); emit('queue_end')
        emit('prompt_start'); clock.advance(3); emit('prompt_end')
        emit('generation_start'); clock.advance(.5); emit('first_token'); clock.advance(1.5); emit('generation_end')
        return httpx.Response(200, json={'model': 'fixture-v1', 'choices': [{'message': {'role': 'assistant', 'content': '2'}}]})
    core = make_core(tmp_path, reply)
    report = BenchmarkHarness(core, tmp_path / 'reports', clock=clock).run('case1', {'prompt': 'Return 1+1'}, Contract,
        contract_version='sum-public-v1', configuration=setup(), prepare=prepare, evidence_kind='synthetic')
    assert report['status'] == 'passed'
    assert report['evidence_kind'] == 'synthetic'
    assert report['metrics']['load_seconds'] == 2
    assert report['metrics']['queue_seconds'] == 1
    assert report['metrics']['prompt_seconds'] == 3
    assert report['metrics']['generation_seconds'] == 2
    assert report['metrics']['ttft_seconds'] == 4.5
    assert report['costs']['api_usd'] == '0'
    assert report['costs']['complete_usd'] is None
    assert report['measurements']['peak_memory_bytes'] is None
    assert report['hardware_status'] == 'not_validated'


def test_oom_and_interruption_retain_distinct_failure_evidence(tmp_path):
    clock = Clock()
    def prepare(emit):
        emit('load_start'); clock.advance(2); emit('oom')
        raise MemoryError('synthetic allocation failure')
    core = make_core(tmp_path / 'oom', lambda req: (_ for _ in ()).throw(AssertionError('must not call')))
    row = BenchmarkHarness(core, tmp_path / 'reports', clock=clock).run('oom', {'prompt': 'Return 1+1'}, Contract,
        contract_version='sum-public-v1', configuration=setup(), prepare=prepare)
    assert row['status'] == 'engineering_interruption'
    assert row['failure'] == 'out_of_memory'
    assert row['public_passed'] is None
    assert row['costs']['api_usd'] == '0'
    assert row['metrics']['load_seconds'] is None
    observer = {}
    def bind(emit): observer['emit'] = emit
    def interrupted(req):
        observer['emit']('interrupted')
        raise httpx.ReadTimeout('synthetic')
    core = make_core(tmp_path / 'interrupted', interrupted)
    row = BenchmarkHarness(core, tmp_path / 'reports', clock=clock).run('interrupted', {'prompt': 'Return 1+1'}, Contract,
        contract_version='sum-public-v1', configuration=setup(), prepare=bind)
    assert row['status'] == 'engineering_interruption'
    assert row['failure'] == 'engine_interrupted'
    assert row['core_result'] is None
    assert row['ledger']['calls'] == 1


def test_switch_and_complete_resource_cost_are_separate_from_zero_model_fee(tmp_path):
    clock = Clock()
    def prepare(emit):
        emit('switch_start'); clock.advance(3600); emit('switch_end')
    core = make_core(tmp_path, lambda req: httpx.Response(200, json={'model': 'fixture-v1',
        'choices': [{'message': {'role': 'assistant', 'content': '2'}}]}))
    row = BenchmarkHarness(core, tmp_path / 'reports', clock=clock).run('switch', {'prompt': 'Return 1+1'}, Contract,
        contract_version='sum-public-v1', configuration=setup(), prepare=prepare,
        measurements={'peak_memory_bytes': 512, 'energy_kwh': '0.2', 'human_seconds': 60},
        rates={'electricity_usd_per_kwh': '0.1', 'host_usd_per_hour': '0.4', 'human_usd_per_hour': '12'})
    assert row['metrics']['switch_seconds'] == 3600
    assert row['metrics']['load_seconds'] is None
    assert row['costs']['api_usd'] == '0'
    assert row['costs']['electricity_usd'] == '0.02'
    assert row['costs']['host_usd'] == '0.4'
    assert row['costs']['human_usd'] == '0.2'
    assert row['costs']['complete_usd'] == '0.62'
    assert row['concurrency_validated'] is False


def test_existing_report_reuses_paid_answer_but_rejects_changed_routing_config(tmp_path):
    import pytest
    seen = []
    def reply(req):
        seen.append(req)
        return httpx.Response(200, json={'model': 'fixture-v1', 'choices': [{'message': {'role': 'assistant', 'content': '2'}}]})
    core = make_core(tmp_path, reply)
    harness = BenchmarkHarness(core, tmp_path / 'reports', clock=Clock())
    kwargs = dict(contract_version='sum-public-v1', configuration=setup())
    first = harness.run('cached', {'prompt': 'Return 1+1'}, Contract, **kwargs)
    assert harness.run('cached', {'prompt': 'Return 1+1'}, Contract, **kwargs) == first
    assert len(seen) == 1
    core.profiles['high']['effort'] = 'high'
    with pytest.raises(ValueError, match='Resume evidence changed'):
        harness.run('cached', {'prompt': 'Return 1+1'}, Contract, **kwargs)


def test_cloud_and_local_use_identical_public_contract_without_hardware_claim(tmp_path):
    def local_reply(req):
        return httpx.Response(200, json={'model': 'fixture-v1', 'choices': [{'message': {'role': 'assistant', 'content': '2'}}]})
    local = make_core(tmp_path / 'local-case', local_reply)
    profiles, decision = example_config()
    def cloud_reply(req):
        p = json.loads(req.content)
        return httpx.Response(200, json={'id': 'gen-benchmark', 'model': p['model'], 'provider': 'Example',
            'usage': {'cost': '.03'}, 'choices': [{'message': {'role': 'assistant', 'content': '2'}}]})
    cloud = Core(Client(httpx.Client(transport=httpx.MockTransport(cloud_reply)), 'fixture-key', tmp_path / 'cloud-case'), profiles, decision)
    cloud_setup = ModelSetup('example/high', 'provider-unspecified', 'provider-managed',
                            {'device': 'provider-managed'}, {'context_tokens': 128})
    rows = [BenchmarkHarness(core, tmp_path / 'reports', clock=Clock()).run(case, {'prompt': 'Return 1+1'}, Contract,
        contract_version='sum-public-v1', configuration=config, mode=mode)
        for core, case, config, mode in [(local, 'local', setup(), 'cloudyfan'), (cloud, 'cloud', cloud_setup, 'fixed_high')]]
    assert [row['public_passed'] for row in rows] == [True, True]
    assert {row['contract_version'] for row in rows} == {'sum-public-v1'}
    assert [row['costs']['api_usd'] for row in rows] == ['0', '0.03']
    assert all(row['hardware_status'] == 'not_validated' for row in rows)


def test_incomplete_or_repeated_phase_timings_remain_unknown(tmp_path):
    clock = Clock()
    def prepare(emit):
        emit('load_start'); clock.advance(1); emit('load_end')
        emit('load_start'); clock.advance(2); emit('load_end')
        emit('generation_start')
    core = make_core(tmp_path, lambda req: httpx.Response(200, json={'model': 'fixture-v1',
        'choices': [{'message': {'role': 'assistant', 'content': '2'}}]}))
    row = BenchmarkHarness(core, tmp_path / 'reports', clock=clock).run('repeated', {'prompt': 'Return 1+1'}, Contract,
        contract_version='sum-public-v1', configuration=setup(), prepare=prepare)
    assert row['metrics']['load_seconds'] is None
    assert row['metrics']['generation_seconds'] is None
    assert len(row['events']) == 5


def test_invalid_resource_data_rejected_before_model_execution(tmp_path):
    import pytest
    core = make_core(tmp_path, lambda req: pytest.fail('No model call allowed'))
    for bad in (True, -1, 'NaN', 'Infinity'):
        with pytest.raises(ValueError):
            BenchmarkHarness(core, tmp_path / 'reports').run('bad', {'prompt': 'Return 1+1'}, Contract,
                contract_version='sum-public-v1', configuration=setup(), measurements={'energy_kwh': bad})


def test_similar_case_ids_do_not_mix_billed_calls(tmp_path):
    profiles, decision = example_config()
    seen = []
    def reply(req):
        p = json.loads(req.content); seen.append(req)
        return httpx.Response(200, json={'id': f'gen-benchmark-{len(seen)}', 'model': p['model'], 'provider': 'Example',
            'usage': {'cost': '.03'}, 'choices': [{'message': {'role': 'assistant', 'content': '2'}}]})
    cloud = Core(Client(httpx.Client(transport=httpx.MockTransport(reply)), 'fixture-key', tmp_path / 'cloud'), profiles, decision)
    harness = BenchmarkHarness(cloud, tmp_path / 'reports', clock=Clock())
    config = ModelSetup('example/high', 'provider-unspecified', 'provider-managed', {'device': 'provider'}, {'context_tokens': 128})
    for case in ('cloud-other', 'cloud'):
        row = harness.run(case, {'prompt': 'Return 1+1'}, Contract, contract_version='sum-public-v1', configuration=config, mode='fixed_high')
        assert row['ledger']['calls'] == 1
        assert row['costs']['api_usd'] == '0.03'
