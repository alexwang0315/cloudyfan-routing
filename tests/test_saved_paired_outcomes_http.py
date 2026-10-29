"""Paired saved outcomes for identical inputs across original modes, via the operator HTTP seam."""
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import json
import threading

import httpx
import pytest

from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import build_local_integration
from cloudyfan_monitor.saved_pairing import compare_pairs
from test_local_http_integration import Clock, QuietHandler, ThreadedServer
from test_local_http_integration import loopback_network as loopback_network


class Paired:
    def __init__(self, bundle, origin, stack):
        self.bundle, self.origin, self.stack, self.clients = bundle, origin, stack, {}

    def client(self, principal='operator'):
        if principal not in self.clients:
            token = self.bundle.operator_token if principal == 'operator' else self.bundle.credentials[principal].token
            self.clients[principal] = self.stack.enter_context(httpx.Client(
                base_url=self.origin, trust_env=False, timeout=10, headers={'Authorization': 'Bearer ' + token}))
        return self.clients[principal]

    def run(self, principal, run, mode, *, fail=(), prompt='1+1 PRIVATE_PAIRED_CANARY'):
        operator = self.client()
        revision = operator.get('/monitor/snapshot').json()['control']['revision']
        assert operator.post('/monitor/mode', json={'revision': revision, 'mode': mode}).status_code == 200
        self.bundle.upstream.fail_tiers = set(fail)
        created = self.client(principal).post('/v1/tasks', json={'run_id': run, 'category': CATEGORY, 'prompt': prompt})
        assert created.status_code in (200, 409), created.text
        return {'client': principal, 'run_id': run}

    def state(self):
        return {'model_calls': len(self.bundle.upstream.calls), 'files': hashlib.sha256(json.dumps({
            str(path.relative_to(self.bundle.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.bundle.root.rglob('*')) if path.is_file()
            and '.saved-comparisons' not in path.parts}, sort_keys=True).encode()).hexdigest()}

    def compare(self, tasks, **extra):
        before = self.state()
        response = self.client().post('/monitor/comparisons', json={
            'policy_version': 'original-policy-reproduction-v1', 'tasks': tasks, **extra})
        assert response.status_code == 200, response.text
        assert 'PRIVATE_PAIRED_CANARY' not in response.text and str(self.bundle.root) not in response.text
        for _ in range(2):
            assert self.client().get('/monitor/comparisons/' + response.json()['comparison_id']).content == response.content
        assert self.state() == before, 'Pairing or polling changed original state or called an upstream'
        return response.json()


@pytest.fixture
def paired_http(tmp_path, loopback_network):
    with ExitStack() as stack:
        server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f'http://127.0.0.1:{server.server_port}'
        bundle = build_local_integration(tmp_path / 'service', origin=origin, clock=Clock(), seed=False)
        server.set_app(bundle.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        yield Paired(bundle, origin, stack)
    assert not loopback_network.blocked


def test_identical_inputs_pair_across_modes_with_equal_quality_cost_difference(paired_http):
    tasks = [paired_http.run('studio', 'pair-oss', 'open_source_first'),
             paired_http.run('agent', 'pair-high', 'fixed_high')]
    report = paired_http.compare(tasks, pairing='same-input-v1')
    pairing = report['pairing']
    assert report['scope']['pairing'] == 'same-input-v1' and pairing['rule'] == 'same-input-v1'
    assert pairing['counts']['paired_groups'] == 1 and pairing['counts']['equal_quality'] == 1
    group = pairing['groups'][0]
    assert group['status'] == 'equal_quality' and {row['mode'] for row in group['members']} == {
        'open_source_first', 'fixed_high'}
    difference = pairing['differences'][0]
    assert difference['strategies'] == ['fixed_high', 'open_source_first'] and difference['equal_quality_groups'] == 1
    costs = {row['mode']: Decimal(row['complete_api_usd']) for row in group['members']}
    assert Decimal(difference['difference_usd']) == costs['open_source_first'] - costs['fixed_high']
    assert pairing['alternative_outcomes'] == 'not_observed' and pairing['adopted'] is False
    assert not any(word in json.dumps(pairing) for word in ('win_rate', 'success_rate', 'saving'))


def test_worse_quality_is_reported_without_a_cost_difference_and_rescue_needs_the_same_flow(paired_http):
    tasks = [paired_http.run('studio', 'quality-cloudyfan', 'cloudyfan', fail={'open_source'}),
             paired_http.run('agent', 'quality-oss', 'open_source_first', fail={'open_source', 'low', 'mid', 'high'})]
    pairing = paired_http.compare(tasks, pairing='same-input-v1')['pairing']
    group = pairing['groups'][0]
    assert group['status'] == 'quality_differs' and group['quality'] == 'differs'
    assert pairing['differences'][0]['difference_usd'] is None
    members = {row['mode']: row for row in group['members']}
    assert members['cloudyfan']['observed_rescue'] is True and members['cloudyfan']['first_public_status'] != 'passed'
    assert members['open_source_first']['task_status'] == 'failed' and members['open_source_first']['observed_rescue'] is False
    assert pairing['arms']['cloudyfan']['observed_rescues'] == 1
    assert pairing['counts']['failed_flows'] == 1


def test_different_inputs_and_duplicate_modes_stay_unpaired_or_not_comparable(paired_http):
    tasks = [paired_http.run('studio', 'input-a', 'cloudyfan'),
             paired_http.run('agent', 'input-b', 'fixed_high', prompt='2+2 PRIVATE_PAIRED_CANARY'),
             paired_http.run('batch', 'input-a-again', 'cloudyfan')]
    pairing = paired_http.compare(tasks, pairing='same-input-v1')['pairing']
    reasons = sorted(tuple(row['reasons']) for row in pairing['groups'])
    assert reasons == [('no_other_strategy',), ('no_other_strategy', 'duplicate_strategy_member')]
    assert pairing['counts']['paired_groups'] == 0 and pairing['counts']['requested'] == 3
    assert all(row['difference_usd'] is None for row in pairing['differences'])


def test_expired_originals_are_excluded_without_reopening_raw_input(paired_http):
    tasks = [paired_http.run('studio', 'expired-a', 'cloudyfan'), paired_http.run('agent', 'expired-b', 'fixed_high')]
    paired_http.bundle.app.store.clock.now += 31 * 86400
    pairing = paired_http.compare(tasks, pairing='same-input-v1')['pairing']
    assert pairing['counts']['excluded'] == 2 and pairing['counts']['groups'] == 0
    assert {row['reason'] for row in pairing['excluded']} == {'original_raw_access_expired'}


@pytest.mark.parametrize('rule', ['same-output-v1', '', 1, None, {'match': 'prompt'}])
def test_unregistered_pairing_rule_is_rejected_and_plain_reports_have_no_pairing(paired_http, rule):
    tasks = [paired_http.run('studio', 'rule-a', 'cloudyfan')]
    response = paired_http.client().post('/monitor/comparisons', json={
        'policy_version': 'original-policy-reproduction-v1', 'tasks': tasks, 'pairing': rule})
    assert response.status_code in (409, 422)
    plain = paired_http.compare(tasks)
    assert 'pairing' not in plain and 'pairing' not in plain['scope']
    catalog = paired_http.client().get('/monitor/comparisons').json()
    assert [row['version'] for row in catalog['pairing_rules']] == ['same-input-v1']
    assert paired_http.client('studio').get('/monitor/comparisons').status_code == 401


def _entry(run, mode, status, complete, unknown=0, first='passed'):
    return {'task': {'client': 'synthetic', 'run_id': run}, 'key': 'k', 'key_reason': None, 'original': {
        'mode': mode, 'task_status': status, 'pool_version': 'p', 'contract_version': 'c', 'routing_version': 'r',
        'path': [{'public_status': first}], 'costs': {'known_api_usd': complete, 'complete_api_usd': complete,
                                                      'unknown_events': unknown}}}


def test_unknown_fee_keeps_known_sum_and_zero_success_is_undefined_not_zero():
    result = compare_pairs([_entry('a', 'cloudyfan', 'failed', '0.01'),
                            _entry('b', 'fixed_high', 'failed', None, unknown=1)])
    group = result['groups'][0]
    assert (group['status'], group['quality'], group['reasons']) == ('no_accepted_work', 'none_accepted', ['complete_fee_unknown'])
    assert result['arms']['cloudyfan']['complete_api_usd_per_accepted'] is None
    assert result['arms']['cloudyfan']['complete_api_usd'] == '0.01'
    assert result['arms']['fixed_high']['complete_api_usd'] is None
    assert result['arms']['fixed_high']['unknown_fee_flows'] == 1
    assert result['counts']['unknown_fee_flows'] == 1
    assert all(row['difference_usd'] is None for row in result['differences'])
