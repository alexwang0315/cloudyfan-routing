import json

import pytest

from cloudyfan_jev.scores import cli
from cloudyfan_jev.scores.adapters import import_codexradar
from cloudyfan_jev.scores.demo import example_inputs
from cloudyfan_jev.scores.qualification import build_qualification_pack, qualified_snapshot

NOW = '2026-10-18T10:00:00+00:00'


def inputs():
    imported, registry, policy, _, _ = example_inputs(NOW)
    observations = import_codexradar(json.dumps(imported).encode(), retrieved_at=NOW)
    return observations, registry, policy


def test_pending_review_produces_auditable_preview_but_cannot_promote():
    pack = build_qualification_pack(*inputs(), review={}, now=NOW)
    assert pack['status'] == 'blocked'
    assert {k: len(v) for k, v in pack['preview']['pools'].items()} == {'low': 1, 'mid': 1, 'high': 1}
    assert 'domain:review_pending' in pack['blockers']
    assert 'source:review_pending' in pack['blockers']
    assert pack['inputs']['policy']['low_mid'] == 80
    with pytest.raises(ValueError, match='not qualified'):
        qualified_snapshot(pack, now=NOW)


def reviewed(observations, registry, policy):
    from cloudyfan_jev.scores.catalog import digest
    evidence = {'status': 'approved', 'reviewer': 'synthetic-reviewer',
                'reviewed_at': NOW, 'evidence': ['fixture://review-only']}
    return {'input_sha256': digest({'observations': observations, 'registry': registry, 'policy': policy}),
            **{area: dict(evidence) for area in ('domain', 'source', 'thresholds', 'coverage', 'expiry', 'access')},
            'models': {r['config_id']: dict(evidence) for r in [registry['open_source'], *registry['candidates']]}}


def test_approval_requires_bound_evidence_and_model_review():
    obs, registry, policy = inputs()
    review = reviewed(obs, registry, policy)
    review['thresholds']['evidence'] = []
    del review['models']['low']
    pack = build_qualification_pack(obs, registry, policy, review=review, now=NOW)
    assert 'thresholds:review_pending' in pack['blockers']
    assert 'model:low:review_pending' in pack['blockers']
    review = reviewed(obs, registry, policy)
    policy['low_mid'] = 79
    pack = build_qualification_pack(obs, registry, policy, review=review, now=NOW)
    assert 'review:input_binding_mismatch' in pack['blockers']


def test_synthetic_review_never_qualifies_production_and_tampering_is_rejected():
    obs, registry, policy = inputs()
    pack = build_qualification_pack(obs, registry, policy, review=reviewed(obs, registry, policy),
                                    now=NOW, synthetic=True)
    assert pack['status'] == 'synthetic_only'
    assert pack['production_ready'] is False
    with pytest.raises(ValueError, match='Synthetic'):
        qualified_snapshot(pack, now=NOW)
    assert qualified_snapshot(pack, now=NOW, allow_synthetic=True)['pools']['low']
    pack['synthetic'] = False
    with pytest.raises(ValueError, match='integrity'):
        qualified_snapshot(pack, now=NOW)


def test_cli_qualify_writes_blocking_report_without_publishing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'utc_now', lambda: NOW)
    obs, registry, policy = inputs()
    args = ['qualify', '--output', str(tmp_path / 'pack.json')]
    for name, value in [('observations', obs), ('registry', registry), ('policy', policy), ('review', {})]:
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(value))
        args += ['--' + name, str(path)]
    assert cli.main(args) == 0
    pack = json.loads((tmp_path / 'pack.json').read_text())
    assert pack['status'] == 'blocked'
    assert json.loads(capsys.readouterr().out)['production_ready'] is False
    assert not (tmp_path / 'current.json').exists()


def test_build_refuses_missing_review_before_store_write(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'utc_now', lambda: NOW)
    obs, registry, policy = inputs()
    args = ['build', '--store', str(tmp_path / 'store')]
    for name, value in [('observations', obs), ('registry', registry), ('policy', policy)]:
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(value))
        args += ['--' + name, str(path)]
    assert cli.main(args) == 1
    assert not (tmp_path / 'store').exists()


@pytest.mark.parametrize('change,reason', [
    ({'effort': None}, 'effort_unknown'),
    ({'retrieved_at': '2026-10-01T00:00:00Z'}, 'source_stale'),
    ({'source_model_id': 'unmapped'}, 'score_missing'),
])
def test_unqualified_evidence_remains_excluded_in_pack(change, reason):
    obs, registry, policy = inputs()
    obs[0].update(change)
    pack = build_qualification_pack(obs, registry, policy, review=reviewed(obs, registry, policy), now=NOW)
    assert pack['status'] == 'blocked'
    assert pack['preview']['excluded'] == [{'config_id': 'low', 'reason': reason}]
    assert not pack['preview']['pools']['low']


def test_qualification_pack_through_three_pools_and_core_run(tmp_path):
    import httpx
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.demo import ArithmeticEnvironment
    from cloudyfan_jev.scores import ScoreCore, SnapshotStore

    obs, registry, policy = inputs()
    pack = build_qualification_pack(obs, registry, policy, review=reviewed(obs, registry, policy),
                                    now=NOW, synthetic=True)
    snapshot = qualified_snapshot(pack, now=NOW, allow_synthetic=True)
    assert snapshot['qualification']['pack_id'] == pack['pack_id']
    store = SnapshotStore(tmp_path / 'snapshots')
    store.publish(snapshot, now=NOW)
    requests = []

    def api(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path.endswith('/decisions'):
            return httpx.Response(200, json={'id': f'gen-qualification-{len(requests)}',
                'model': body['model'], 'provider': 'TypeSafe', 'usage': {'cost': .0001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'low_mid': {'type': 'choice', 'choice': 'low',
                                        'probabilities': {'low': .9, 'mid': .1}}}})
        return httpx.Response(200, json={'id': f'gen-qualification-{len(requests)}',
            'model': body['model'], 'provider': 'Example', 'usage': {'cost': .001, 'prompt_tokens': 1, 'completion_tokens': 1},
            'choices': [{'message': {'role': 'assistant', 'content': '3' if len(requests) == 1 else '2'},
                         'finish_reason': 'stop'}]})

    _, _, _, requirements, decision = example_inputs(NOW)
    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        engine = ScoreCore(Client(http, 'offline-only', tmp_path / 'run'), store.load(now=NOW), requirements,
                           decision, run_id='qualified-demo', clock=lambda: NOW)
        result = engine.run('qualified-demo', {'prompt': 'Return the integer sum of 1 and 1.'},
                            ArithmeticEnvironment, contract_version='arithmetic-demo-v1')
    assert result['status'] == 'passed'
    assert result['costs']['api_usd'] == '0.0021'
    assert [request['model'] for request in requests] == ['example/open_source', 'typesafe/jev-1.13', 'example/low']
