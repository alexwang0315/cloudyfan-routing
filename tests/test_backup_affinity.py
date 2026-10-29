"""#47：快取親和性只在全部閘門之後、等價集合內作次要排序；不啟用時排序完全不變。"""
import json

import pytest

from test_backup_runtime import build, response, run


def _handler(calls):
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls))
    return handler


def _model(core, config_id):
    return next(r['entry']['profile']['model_version'] for rows in core._backup['pools'].values()
                for r in rows if r['entry']['config_id'] == config_id)


def _checkpoint(tmp_path):
    return json.loads((tmp_path / 'receipts' / 'runs' / 'backup' / 'backup-selection-0.json').read_text())


def test_without_an_affinity_port_the_original_order_and_checkpoint_are_unchanged(tmp_path):
    calls = []
    core, _, _ = build(tmp_path, _handler(calls))
    result = run(core)
    assert calls[0]['model'] == _model(core, 'open_source-0')
    assert result['attempts'][0]['config_id'] == 'open_source-0'
    assert 'affinity' not in _checkpoint(tmp_path)


def test_a_trusted_hint_breaks_the_tie_inside_the_equivalent_set_and_is_saved(tmp_path):
    calls, offers = [], []
    core, _, _ = build(tmp_path, _handler(calls))

    def affinity(request):
        offers.append(request)
        return {'policy_version': 'prefix-affinity-v1', 'preferred': 'open_source-1',
                'reason': 'identical_leading_messages_in_same_scope', 'observed_at': 1.0,
                'matched_messages': 1, 'previous_started_at': '2026-10-21T00:00:00+00:00'}

    core.affinity = affinity
    result = run(core)
    assert [row['config_id'] for row in offers[0]['candidates']] == ['open_source-0', 'open_source-1', 'open_source-2']
    # The offered payload is the exact first-turn request the real builder would send.
    assert all(row['payload']['messages'][0]['role'] == 'system' for row in offers[0]['candidates'])
    assert calls[0]['model'] == _model(core, 'open_source-1') and result['attempts'][0]['config_id'] == 'open_source-1'
    saved = _checkpoint(tmp_path)['affinity']['0:open_source']
    assert saved['adopted'] is True and saved['original_config_id'] == 'open_source-0'
    assert saved['selected_config_id'] == 'open_source-1' and saved['policy_version'] == 'prefix-affinity-v1'
    assert saved['equivalent_config_ids'] == ['open_source-0', 'open_source-1', 'open_source-2']


@pytest.mark.parametrize('answer,reason', [
    ({'preferred': 'low-0', 'reason': 'x'}, 'affinity_preference_outside_equivalent_set'),
    ({'preferred': None, 'reason': 'no_earlier_sent_request_with_identical_prefix'},
     'no_earlier_sent_request_with_identical_prefix'),
    (RuntimeError('port down'), 'affinity_port_failed'),
    ('not-a-dict', 'affinity_port_failed'),
], ids=['outside-set', 'no-hint', 'port-error', 'bad-answer'])
def test_skipped_affinity_keeps_the_original_choice_and_records_why(tmp_path, answer, reason):
    calls = []
    core, _, _ = build(tmp_path, _handler(calls))

    def affinity(request):
        if isinstance(answer, Exception):
            raise answer
        return answer

    core.affinity = affinity
    run(core)
    assert calls[0]['model'] == _model(core, 'open_source-0')
    saved = _checkpoint(tmp_path)['affinity']['0:open_source']
    assert (saved['adopted'], saved['reason'], saved['selected_config_id']) == (False, reason, 'open_source-0')


def test_capacity_gate_runs_before_affinity_so_an_excluded_candidate_is_never_offered(tmp_path):
    calls, offers = [], []
    core, _, _ = build(tmp_path, _handler(calls))
    closed = _model(core, 'open_source-1')
    core.client.candidate_window = lambda profile: ({'status': 'closed', 'reason_code': 'capacity_window_closed'}
                                                    if profile['model_version'] == closed else None)
    core.affinity = lambda request: offers.append(request) or {'preferred': 'open_source-1', 'reason': 'x'}
    run(core)
    assert 'open_source-1' not in [row['config_id'] for row in offers[0]['candidates']]
    assert calls[0]['model'] == _model(core, 'open_source-0')
    assert _checkpoint(tmp_path)['affinity']['0:open_source']['reason'] == 'affinity_preference_outside_equivalent_set'


def test_replay_rejects_a_saved_affinity_choice_outside_the_equivalent_set(tmp_path):
    calls = []
    core, _, _ = build(tmp_path, _handler(calls))
    core.affinity = lambda request: {'preferred': 'open_source-1', 'reason': 'x', 'policy_version': 'v'}
    run(core)
    run_dir = tmp_path / 'receipts' / 'runs' / 'backup'
    checkpoint = run_dir / 'backup-selection-0.json'
    saved = json.loads(checkpoint.read_text())
    saved['affinity']['0:open_source']['equivalent_config_ids'] = ['open_source-1']
    checkpoint.chmod(0o600)
    checkpoint.write_text(json.dumps(saved))
    (run_dir / 'result.json').rename(run_dir / 'result-before-replay-control.json')
    replay, _, _ = build(tmp_path, _handler(calls))
    with pytest.raises(ValueError, match='equivalent set|differs'):
        run(replay)
