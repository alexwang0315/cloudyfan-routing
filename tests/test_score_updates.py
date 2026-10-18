from copy import deepcopy

from test_qualification import NOW, inputs, reviewed
from cloudyfan_jev.scores.qualification import build_qualification_pack, qualified_snapshot
from cloudyfan_jev.scores.updates import preview_update, ScoreUpdater
from cloudyfan_jev.scores import SnapshotStore


def pack(change=None):
    obs, registry, policy = inputs()
    if change:
        change(obs, registry, policy)
    return build_qualification_pack(obs, registry, policy, review=reviewed(obs, registry, policy),
                                    now=NOW, synthetic=True)


def test_preview_describes_group_and_qualification_changes_from_two_observations():
    before = pack()

    def change(obs, registry, policy):
        obs[0]['score'] = 90
        registry['candidates'][1]['approved'] = False
        new = deepcopy(registry['candidates'][0])
        new['config_id'] = 'new-low'
        new['source_model_id'] = 'example/new-low'
        registry['candidates'].append(new)
        obs.append({**obs[0], 'source_model_id': 'example/new-low', 'score': 60})

    after = pack(change)
    report = preview_update(before, after, now=NOW, allow_synthetic=True)
    assert report['added'] == ['new-low']
    assert report['removed'] == ['mid']
    assert report['tier_changes'] == [{'config_id': 'low', 'before': 'low', 'after': 'mid'}]
    assert report['qualification_changes'] == [{'config_id': 'mid', 'before': 'eligible',
                                               'after': 'configuration_not_approved'}]
    assert report['before']['pack_id'] == before['pack_id']
    assert report['after']['input_sha256'] == after['input_sha256']
    assert report['after']['source_version'] == 'synthetic-demo-v1'


def test_publish_records_auditable_result_and_idempotent_retry(tmp_path):
    before, after = pack(), pack(lambda obs, registry, policy: obs[0].update(score=61))
    store = SnapshotStore(tmp_path)
    old = qualified_snapshot(before, now=NOW, allow_synthetic=True)
    store.publish(old, now=NOW)
    updater = ScoreUpdater(store)
    result = updater.apply('update-1', before, after, now=NOW, allow_synthetic=True)
    assert result['status'] == 'published'
    assert result['attempts'] == 1
    assert store.load()['snapshot_id'] == result['snapshot_id']
    assert updater.inspect('update-1') == result
    assert updater.apply('update-1', before, after, now=NOW, allow_synthetic=True) == result
    assert store.load(old['snapshot_id']) == old


def test_expected_current_prevents_stale_update_overwriting_winner(tmp_path):
    import pytest
    before, a, b = pack(), pack(lambda o, r, p: o[0].update(score=61)), pack(lambda o, r, p: o[0].update(score=62))
    store = SnapshotStore(tmp_path)
    old = qualified_snapshot(before, now=NOW, allow_synthetic=True)
    first = qualified_snapshot(a, now=NOW, allow_synthetic=True)
    second = qualified_snapshot(b, now=NOW, allow_synthetic=True)
    store.publish(old, now=NOW)
    store.publish(first, now=NOW, expected_current_id=old['snapshot_id'])
    with pytest.raises(ValueError, match='Current snapshot changed'):
        store.publish(second, now=NOW, expected_current_id=old['snapshot_id'])
    assert store.load() == first


def test_failed_pointer_write_is_audited_and_retry_keeps_same_input(tmp_path, monkeypatch):
    from cloudyfan_jev.scores import snapshots
    import pytest
    before, after = pack(), pack(lambda obs, registry, policy: obs[0].update(score=61))
    store = SnapshotStore(tmp_path)
    old = qualified_snapshot(before, now=NOW, allow_synthetic=True)
    store.publish(old, now=NOW)
    replace = snapshots.os.replace

    def interrupted(source, destination):
        if str(destination).endswith('current.json'):
            raise OSError('synthetic disk failure')
        return replace(source, destination)

    monkeypatch.setattr(snapshots.os, 'replace', interrupted)
    updater = ScoreUpdater(store)
    failed = updater.apply('write-failure', before, after, now=NOW, allow_synthetic=True)
    assert failed['status'] == 'failed'
    assert failed['failure_reason'] == 'publication:OSError'
    assert updater.inspect('write-failure')['history'][0]['status'] == 'failed'
    assert store.load() == old
    monkeypatch.setattr(snapshots.os, 'replace', replace)
    retried = updater.apply('write-failure', before, after, now=NOW, allow_synthetic=True)
    assert retried['status'] == 'published'
    assert retried['attempts'] == 2
    with pytest.raises(ValueError, match='different update inputs'):
        updater.apply('write-failure', before, before, now=NOW, allow_synthetic=True)


def test_update_cli_preview_apply_and_audit_inspection(tmp_path, monkeypatch, capsys):
    import json
    from cloudyfan_jev.scores import cli
    monkeypatch.setattr(cli, 'utc_now', lambda: NOW)
    before, after = pack(), pack(lambda o, r, p: o[0].update(score=61))
    for name, value in [('before', before), ('after', after)]:
        (tmp_path / (name + '.json')).write_text(json.dumps(value))
    args = ['--previous', str(tmp_path / 'before.json'), '--proposed', str(tmp_path / 'after.json'), '--synthetic']
    assert cli.main(['update-preview', *args, '--output', str(tmp_path / 'preview.json')]) == 0
    assert json.loads((tmp_path / 'preview.json').read_text())['publishable'] is True
    store = SnapshotStore(tmp_path / 'store')
    store.publish(qualified_snapshot(before, now=NOW, allow_synthetic=True), now=NOW)
    assert cli.main(['update', *args, '--store', str(store.root), '--operation', 'cli-update']) == 0
    assert cli.main(['update-inspect', '--store', str(store.root), '--operation', 'cli-update']) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['status'] == 'published'


def test_concurrent_update_keeps_one_winner_and_records_conflict(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    before = pack()
    options = [pack(lambda o, r, p: o[0].update(score=61)), pack(lambda o, r, p: o[0].update(score=62))]
    store = SnapshotStore(tmp_path)
    store.publish(qualified_snapshot(before, now=NOW, allow_synthetic=True), now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: ScoreUpdater(store).apply(
            'concurrent-' + str(i), before, options[i], now=NOW, allow_synthetic=True), range(2)))
    assert sorted(row['status'] for row in results) == ['failed', 'published']
    winner = next(row for row in results if row['status'] == 'published')
    assert store.load()['snapshot_id'] == winner['snapshot_id']
    loser = next(row for row in results if row['status'] == 'failed')
    assert loser['failure_reason'] == 'current_comparison:ValueError'


def test_unqualified_and_changed_source_formats_do_not_replace_current(tmp_path):
    before = pack()
    store = SnapshotStore(tmp_path)
    old = qualified_snapshot(before, now=NOW, allow_synthetic=True)
    store.publish(old, now=NOW)
    updater = ScoreUpdater(store)
    empty_low = pack(lambda o, r, p: o[0].update(effort=None))
    report = updater.apply('no-low', before, empty_low, now=NOW, allow_synthetic=True)
    assert report['status'] == 'blocked'
    assert 'low:no_qualified_candidates' in report['preview']['after']['blockers']
    invalid = deepcopy(before)
    invalid['inputs']['observations'][0]['score'] = 'changed-source-schema'
    failed = updater.apply('bad-source', before, invalid, now=NOW, allow_synthetic=True)
    assert failed['status'] == 'failed'
    assert failed['failure_reason'] == 'preview:ValueError'
    assert store.load() == old


def test_published_pointer_with_interrupted_final_audit_recovers_without_republish(tmp_path, monkeypatch):
    import pytest
    from cloudyfan_jev.scores import snapshots
    before, after = pack(), pack(lambda o, r, p: o[0].update(score=61))
    store = SnapshotStore(tmp_path)
    store.publish(qualified_snapshot(before, now=NOW, allow_synthetic=True), now=NOW)
    original = snapshots.os.replace
    audit_writes = []
    pointer_writes = []

    def interrupted(source, destination):
        if str(destination).endswith('current.json'):
            pointer_writes.append(str(destination))
        if str(destination).endswith('audit-interrupted.json'):
            audit_writes.append(str(destination))
            if len(audit_writes) == 3:
                raise OSError('synthetic final audit interruption')
        return original(source, destination)

    monkeypatch.setattr(snapshots.os, 'replace', interrupted)
    updater = ScoreUpdater(store)
    with pytest.raises(OSError):
        updater.apply('audit-interrupted', before, after, now=NOW, allow_synthetic=True)
    assert updater.inspect('audit-interrupted')['status'] == 'pending'
    report = updater.apply('audit-interrupted', before, after, now='2026-10-20T10:00:00Z', allow_synthetic=True)
    assert report['status'] == 'published'
    assert report['attempts'] == 2
    assert len(pointer_writes) == 1


def test_new_run_uses_update_and_original_run_resumes_frozen_snapshot(tmp_path):
    import json
    import httpx
    import pytest
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.demo import ArithmeticEnvironment
    from cloudyfan_jev.scores import ScoreCore
    from cloudyfan_jev.scores.demo import example_inputs

    before, after = pack(), pack(lambda o, r, p: o[0].update(score=61))
    store = SnapshotStore(tmp_path / 'scores')
    old = qualified_snapshot(before, now=NOW, allow_synthetic=True)
    store.publish(old, now=NOW)
    requests, seen_scores = [], []

    def api(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path.endswith('/decisions'):
            seen_scores.append(body['state']['candidates']['low']['capability_card']['benchmark_evidence']['score'])
            return httpx.Response(200, json={'id': f'gen-update-{len(requests)}',
                'model': body['model'], 'provider': 'TypeSafe',
                'usage': {'cost': .0001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'low_mid': {'type': 'choice', 'choice': 'low',
                                        'probabilities': {'low': .9, 'mid': .1}}}})
        return httpx.Response(200, json={'id': f'gen-update-{len(requests)}',
            'model': body['model'], 'provider': 'Example',
            'usage': {'cost': .001, 'prompt_tokens': 1, 'completion_tokens': 1},
            'choices': [{'message': {'role': 'assistant', 'content': '3' if body['model'] == 'example/open_source' else '2'},
                         'finish_reason': 'stop'}]})

    _, _, _, requirements, decision = example_inputs(NOW)
    task = {'prompt': 'Return the integer sum of 1 and 1.'}
    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        client = Client(http, 'offline-only', tmp_path / 'runs')
        for run_id in ('old', 'new'):
            if run_id == 'new':
                assert ScoreUpdater(store).apply('update', before, after, now=NOW, allow_synthetic=True)['status'] == 'published'
            core = ScoreCore(client, store.load(now=NOW), requirements, decision, run_id=run_id, clock=lambda: NOW)
            assert core.run(run_id, task, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
        assert seen_scores == [60, 61]
        expired = '2026-10-20T10:00:00Z'
        resumed = ScoreCore(client, store.load(old['snapshot_id']), requirements, decision,
                            run_id='old', clock=lambda: expired)
        assert resumed.run('old', task, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
        assert len(requests) == 6
        with pytest.raises(ValueError, match='stale'):
            ScoreCore(client, store.load(), requirements, decision, run_id='too-late', clock=lambda: expired)
