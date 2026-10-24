"""Original capacity/fee evidence across independent HTTP processes and restart."""
from decimal import Decimal
import json

import pytest

from capacity_resilience_process_fixture import (
    readonly_rows, resilience_process_pair as resilience_process_pair,
)
from test_local_http_integration import loopback_network as loopback_network, TASK


def known_fees(pair):
    rows = [row for ledger in pair.ledgers().values() for row in ledger['events']]
    return sum((Decimal(row['cost']) for row in rows if row['cost'] is not None), Decimal(0)), sum(
        row['cost'] is None for row in rows)


def save_long_window(pair, run='original-long-window'):
    pair.activate()
    task = {**TASK, 'run_id': run}
    accepted = pair.client(0).post('/v1/tasks', json=task)
    assert accepted.status_code == 202, accepted.text
    pair.dispatched(0, pair.dispatch(0))
    assert len(pair.calls()) == 1 and pair.calls()[0]['method'] == 'POST'
    window = pair.capacity(1)['windows']
    assert len(window['groups']) == 1 and window['groups'][0]['status'] == 'wait', window
    assert window['groups'][0]['not_before'] == pair.clock() + 3600
    assert known_fees(pair)[0] > 0 and known_fees(pair)[1] == 0
    return task, accepted.json(), window


@pytest.mark.parametrize('separate_budgets', [False, True])
def test_known_response_window_survives_two_process_reopen_after_normal_rate_horizon(resilience_process_pair, separate_budgets):
    with resilience_process_pair(retry_after_seconds=3600,
            separate_budgets=separate_budgets, disjoint=separate_budgets) as pair:
        task, accepted, original_window = save_long_window(pair)
        original_receipts, original_fees = pair.receipt_hashes(), pair.ledgers()
        original_pids = [worker.process.pid for worker in pair.workers]
        pair.clock.advance(61)
        pair.reopen()
        assert all(worker.process.pid != old for worker, old in zip(pair.workers, original_pids))
        assert pair.capacity(0)['windows'] == pair.capacity(1)['windows'] == original_window
        before = pair.protected_hashes()
        for index in (0, 1):
            client = pair.client(index)
            state = client.get('/v1/tasks/' + task['run_id'])
            assert state.status_code == 202 and state.json()['queue_state'] == 'queued', state.text
            assert state.json()['queue']['deadline'] == accepted['queue']['deadline']
            assert client.post('/v1/tasks', json=task).json()['queue']['accepted_at'] == accepted['queue']['accepted_at']
            assert client.post('/v1/tasks/' + task['run_id'] + '/resume', json={}).json()['queue_state'] == 'queued'
            pair.view('/monitor/queue', index)
            pair.capacity(index)
        assert pair.protected_hashes() == before
        assert pair.receipt_hashes() == original_receipts and pair.ledgers() == original_fees
        assert pair.dispatched(1, pair.dispatch(1)) is None
        waiting = pair.client(1).get('/v1/tasks/' + task['run_id']).json()
        assert waiting['reason'] == 'queue_waiting_window' and waiting['capacity_reason'] == 'capacity_window_wait'
        assert len(pair.calls()) == 1


@pytest.mark.parametrize('storage_fault', ['window_table', 'empty_database', 'missing_database'])
def test_missing_original_long_window_table_at_same_bound_path_cannot_send_again(resilience_process_pair, storage_fault):
    with resilience_process_pair(retry_after_seconds=3600) as pair:
        task, accepted, original_window = save_long_window(pair)
        original_receipts, original_fees = pair.receipt_hashes(), pair.ledgers()
        pair.clock.advance(61)
        # Establish a legitimate untouched reopen at exactly the same path and
        # identities before constructing the NEW incomplete database in place.
        pair.reopen()
        assert pair.capacity(1)['windows'] == original_window
        assert pair.dispatched(1, pair.dispatch(1)) is None and len(pair.calls()) == 1
        pair.stop_all()
        pair.fault('capacity.sqlite', omitted=('capacity_window_observations',) if storage_fault == 'window_table' else (),
            empty=storage_fault == 'empty_database', absent=storage_fault == 'missing_database')
        pair.start(0)
        pair.start(1)
        before_observation = pair.protected_hashes()
        for index in (0, 1):
            view = pair.view('/monitor/capacity', index)
            assert view['dispatch']['integrity']['status'] == 'unavailable'
            assert view['dispatch']['integrity']['automatic_repair'] is False
            retained = view['dispatch']['windows']['groups']
            assert len(retained) == len(original_window['groups'])
            for current, original in zip(retained, original_window['groups']):
                assert {key: current[key] for key in original} == original
                assert current['observation_status'] == 'unavailable'
                assert current['current_failure'] in {'capacity_store_binding_unavailable',
                    'capacity_store_integrity_unavailable', 'capacity_store_scope_unavailable'}
            observed = pair.client(index, operator=True).get('/monitor/queue')
            assert observed.status_code == 200, observed.text
        assert pair.protected_hashes() == before_observation
        pair.dispatched(1, pair.dispatch(1))
        assert len(pair.calls()) == 1, 'A missing saved long window authorized another physical POST'
        assert pair.receipt_hashes() == original_receipts and pair.ledgers() == original_fees
        state = pair.client(1).get('/v1/tasks/' + task['run_id'])
        assert state.status_code == 202, state.text
        assert state.json()['queue']['deadline'] == accepted['queue']['deadline']
        assert state.json()['costs']['known_api_usd'] == '0.0001'
        assert state.json()['costs']['unknown_events'] == 0
        # Repeated observations after a safe refusal cannot silently repair or
        # bless the damaged copy as a new free state.
        after_refusal = pair.protected_hashes()
        for index in (0, 1):
            pair.view('/monitor/capacity', index)
            observed = pair.client(index, operator=True).get('/monitor/queue')
            assert observed.status_code == 200, observed.text
        assert pair.protected_hashes() == after_refusal
        pair.originals_unchanged()


@pytest.mark.parametrize('mode', ['kill', 'uncertain'])
def test_original_sent_uncertainty_survives_dead_worker_and_readonly_reopen(resilience_process_pair, mode):
    with resilience_process_pair(first_mode=mode, separate_budgets=True, disjoint=True) as pair:
        pair.activate()
        task = {**TASK, 'run_id': 'original-uncertain-' + mode}
        accepted = pair.client(0).post('/v1/tasks', json=task)
        assert accepted.status_code == 202, accepted.text
        command = pair.dispatch(0)
        pair.entered(0)
        requested = pair.client(1).post('/v1/tasks/' + task['run_id'] + '/cancel', json={})
        assert requested.status_code == 200 and requested.json()['cancellation_requested'] is True
        assert requested.json()['provider_cancellation_confirmed'] is False
        if mode == 'kill':
            pair.finish(0, kill=True)
        else:
            pair.release(0)
            assert pair.dispatched(0, command)['queue_state'] == 'interrupted_pending'
            pair.finish(0)
        original_receipts, original_fees = pair.receipt_hashes(), pair.ledgers()
        assert sum(path.endswith('intent.json') for path in original_receipts) == 1
        assert sum(path.endswith('response.json') for path in original_receipts) == (0 if mode == 'kill' else 1)
        assert known_fees(pair) == (Decimal(0), 1)
        pair.clock.advance(600)
        pair.start(0)
        before = pair.protected_hashes()
        for index in (0, 1):
            state = pair.client(index).get('/v1/tasks/' + task['run_id'])
            assert state.status_code == 200, state.text
            assert state.json()['queue_state'] in {'running', 'interrupted_pending'}
            assert state.json()['queue']['deadline'] == accepted.json()['queue']['deadline']
            assert state.json()['costs']['unknown_events'] == 1
            assert pair.client(index).post('/v1/tasks', json=task).json()['queue_state'] in {'running', 'interrupted_pending'}
            assert pair.client(index).post('/v1/tasks/' + task['run_id'] + '/resume', json={}).json()['queue_state'] in {'running', 'interrupted_pending'}
            capacity = pair.capacity(index)
            assert capacity['allocations'][0]['state'] == ('sent' if mode == 'kill' else 'uncertain')
            group = next(row for row in capacity['groups'] if row['group'] == 'shared')
            assert group['concurrency_used'] == group['rpm_used'] == 1
            pair.view('/monitor/queue', index)
        assert pair.protected_hashes() == before
        assert pair.receipt_hashes() == original_receipts and pair.ledgers() == original_fees
        assert len(pair.calls()) == 1


def test_old_held_allocation_outside_latest100_remains_hot_with_independent_original_fees(resilience_process_pair):
    with resilience_process_pair(first_mode='kill', separate_budgets=True, disjoint=True, rpm=500) as pair:
        pair.activate()
        pair.mode('agent', 'open_source_first')
        task = {**TASK, 'run_id': 'old-held-outside-history'}
        assert pair.client(0).post('/v1/tasks', json=task).status_code == 202
        pair.dispatch(0)
        pair.entered(0)
        old_id = pair.capacity(1)['allocations'][0]['id']
        pair.finish(0, kill=True)
        original_receipts = pair.receipt_hashes()
        original_fees = pair.ledgers()
        pair.clock.advance(61)
        pair.start(0)
        pair.mode('studio', 'fixed_high')
        second = pair.client(1, principal='agent')
        for number in range(102):
            later = {**TASK, 'run_id': 'later-known-' + str(number)}
            accepted = second.post('/v1/tasks', json=later)
            assert accepted.status_code == 202, accepted.text
            completed = pair.dispatched(1, pair.dispatch(1))
            assert completed['run_id'] == later['run_id'] and completed['status'] == 'passed', completed
        capacity = pair.capacity(1)
        assert capacity['allocation_count'] == 103 and len(capacity['allocations']) == 100
        assert capacity['allocation_history_complete'] is False
        assert all(row['id'] != old_id for row in capacity['allocations'])
        assert capacity['event_count'] > 100 and len(capacity['events']) == 100
        assert capacity['event_history_complete'] is False
        group = next(row for row in capacity['groups'] if row['group'] == 'shared')
        assert group['concurrency_used'] == group['rpm_used'] == 1
        # A THIRD original fee account shares physical A: its refusal cannot
        # be explained by A's unresolved fee or a shared Client receipt lock.
        contender = {**TASK, 'run_id': 'held-group-independent-fee-contender'}
        assert pair.client(0).post('/v1/tasks', json=contender).status_code == 202
        receipts_before, fees_before = pair.receipt_hashes(), pair.ledgers()
        assert pair.dispatched(0, pair.dispatch(0)) is None
        refused = pair.client(1).get('/v1/tasks/' + contender['run_id']).json()
        assert refused['queue_state'] == 'queued' and refused['capacity_reason'] == 'capacity_concurrency_exhausted', refused
        assert len(pair.calls()) == 103 and len(pair.calls(0)) == 1
        assert pair.receipt_hashes() == receipts_before and pair.ledgers() == fees_before
        assert all(pair.receipt_hashes()[path] == value for path, value in original_receipts.items())
        held_account = next(name for name, ledger in original_fees.items()
            if any(row['cost'] is None for row in ledger['events']))
        assert pair.ledgers()[held_account] == original_fees[held_account]
        assert known_fees(pair)[0] > 0 and known_fees(pair)[1] == 1
        before = pair.protected_hashes()
        for index in (0, 1):
            # Keep the original dashboard timeout boundary after the history;
            # moving mode setup earlier must not remove this real HTTP check.
            pair.view('/monitor/snapshot', index)
            pair.capacity(index)
            pair.view('/monitor/queue', index)
        assert pair.protected_hashes() == before
        # Hide only the OLD held row from the SQL hot query. Its valid JSON,
        # original intent, original fee uncertainty and independent evidence all
        # remain unchanged, and it is already outside the bounded history page.
        pair.stop_all()
        pair.fault('capacity.sqlite', update=('UPDATE capacity_allocations SET state=?,horizon=? WHERE id=?',
            ('complete', 0, old_id)))
        pair.start(0)
        pair.start(1)
        pair.view('/monitor/capacity', 0)
        pair.view('/monitor/queue', 1)
        before_fault_dispatch = pair.receipt_hashes(), pair.ledgers()
        pair.dispatched(0, pair.dispatch(0))
        assert len(pair.calls()) == 103, 'A hidden old held allocation authorized another physical POST'
        assert (pair.receipt_hashes(), pair.ledgers()) == before_fault_dispatch
        independently_ready = {**TASK, 'run_id': 'disjoint-after-local-index-fault'}
        allowed = pair.client(1, principal='agent').post('/v1/tasks', json=independently_ready)
        assert allowed.status_code == 202, allowed.text
        completed = pair.dispatched(1, pair.dispatch(1))
        assert completed['run_id'] == independently_ready['run_id'] and completed['status'] == 'passed', completed
        assert len(pair.calls()) == 104 and len(pair.calls(0)) == 1
        assert all(pair.receipt_hashes()[path] == value for path, value in original_receipts.items())
        assert pair.ledgers()[held_account] == original_fees[held_account]
        pair.originals_unchanged()


def test_disjoint_physical_capacity_does_not_bypass_shared_original_unknown_fee_stop(resilience_process_pair):
    with resilience_process_pair(first_mode='kill', shared_budget=True, disjoint=True, rpm=500) as pair:
        pair.activate()
        pair.mode('agent', 'open_source_first')
        held = {**TASK, 'run_id': 'shared-budget-unknown-original'}
        assert pair.client(0).post('/v1/tasks', json=held).status_code == 202
        pair.dispatch(0)
        pair.entered(0)
        pair.finish(0, kill=True)
        original_fees, original_receipts = pair.ledgers(), pair.receipt_hashes()
        pair.clock.advance(61)
        pair.start(0)
        contender = {**TASK, 'run_id': 'shared-budget-disjoint-contender'}
        response = pair.client(1, principal='agent').post('/v1/tasks', json=contender)
        assert response.status_code == 202, response.text
        assert pair.dispatched(1, pair.dispatch(1)) is None
        state = pair.client(1, principal='agent').get('/v1/tasks/' + contender['run_id']).json()
        assert state['queue_state'] == 'queued' and state['reason'] == 'queue_waiting_budget'
        assert state['capacity_reason'] == 'capacity_original_budget_denied'
        assert known_fees(pair) == (Decimal(0), 1)
        assert len(pair.calls()) == 1 and pair.calls(1) == []
        assert pair.ledgers() == original_fees and pair.receipt_hashes() == original_receipts


def test_mapped_physical_corruption_blocks_original_scope_without_charging_eligible_other_scope(resilience_process_pair):
    with resilience_process_pair(first_mode='kill', separate_budgets=True, disjoint=True) as pair:
        pair.activate()
        pair.mode('agent', 'open_source_first')
        first = {**TASK, 'run_id': 'mapped-original-held-a'}
        assert pair.client(0).post('/v1/tasks', json=first).status_code == 202
        pair.dispatch(0)
        pair.entered(0)
        original_id = pair.capacity(1)['allocations'][0]['id']
        pair.finish(0, kill=True)
        pair.start(0)
        other = pair.client(1, principal='agent')
        control = {**TASK, 'run_id': 'mapped-eligible-b-control'}
        assert other.post('/v1/tasks', json=control).status_code == 202
        assert pair.dispatched(1, pair.dispatch(1))['status'] == 'passed'
        assert len(pair.calls()) == 2
        capacity = pair.capacity()
        physical_b = next(group['physical'] for row in capacity['allocations'] for group in row['groups']
            if group['group'] == 'disjoint')
        pair.mode('studio', 'fixed_high')
        task_a = {**TASK, 'run_id': 'mapped-affected-independent-fee-a'}
        task_b = {**TASK, 'run_id': 'mapped-eligible-independent-fee-b'}
        assert pair.client(0).post('/v1/tasks', json=task_a).status_code == 202
        assert other.post('/v1/tasks', json=task_b).status_code == 202
        original_receipts, original_fees = pair.receipt_hashes(), pair.ledgers()
        row = next(row for row in readonly_rows(pair.root / 'capacity.sqlite', 'capacity_allocations')
            if row['id'] == original_id)
        changed = json.loads(row['payload'])
        assert len(changed['groups']) == 1 and changed['groups'][0]['physical'] != physical_b
        changed['groups'][0]['physical'] = physical_b
        pair.stop_all()
        pair.fault('capacity.sqlite', update=('UPDATE capacity_allocations SET payload=? WHERE id=?',
            (json.dumps(changed, sort_keys=True, separators=(',', ':')), original_id)))
        pair.start(0)
        pair.start(1)
        result = pair.dispatched(1, pair.dispatch(1))
        # There is no required private scan order: the worker may skip A and
        # complete B in the same pass. It must never send the high-model A call.
        assert all(json.loads(call['content']).get('model') != 'example/high' for call in pair.calls()[2:])
        if result is None:
            cancelled = pair.client(0).post('/v1/tasks/' + task_a['run_id'] + '/cancel', json={})
            assert cancelled.status_code == 200, cancelled.text
            result = pair.dispatched(1, pair.dispatch(1))
        assert result is not None and result['run_id'] == task_b['run_id'] and result['status'] == 'passed', result
        assert len(pair.calls()) == 3
        assert all(pair.receipt_hashes()[path] == value for path, value in original_receipts.items())
        held_account = next(name for name, ledger in original_fees.items()
            if any(event['cost'] is None for event in ledger['events']))
        assert pair.ledgers()[held_account] == original_fees[held_account]
        assert known_fees(pair)[1] == 1
        pair.originals_unchanged()


@pytest.mark.parametrize('queue_fault', ['jobs_table', 'empty_database', 'missing_database', 'running_index'])
def test_lost_queue_jobs_cannot_reset_global_execution_slots_for_disjoint_original_account(resilience_process_pair, queue_fault):
    limits = {'capacity': 8, 'concurrency': 1, 'per_principal': 4, 'max_wait_seconds': 1800}
    with resilience_process_pair(first_mode='kill', separate_budgets=True, disjoint=True, limits=limits) as pair:
        pair.activate()
        pair.mode('agent', 'open_source_first')
        held = {**TASK, 'run_id': 'queue-preserved-original-held'}
        first = pair.client(0).post('/v1/tasks', json=held)
        assert first.status_code == 202, first.text
        pair.dispatch(0)
        pair.entered(0)
        pair.finish(0, kill=True)
        pair.start(0)
        pending = {**TASK, 'run_id': 'queue-preserved-original-waiter'}
        assert pair.client(1, principal='agent').post('/v1/tasks', json=pending).status_code == 202
        assert pair.dispatched(1, pair.dispatch(1)) is None
        before = pair.view('/monitor/queue')
        assert before['job_count'] == 2 and before['active_count'] == 2
        assert len(pair.calls()) == 1
        original_receipts, original_fees = pair.receipt_hashes(), pair.ledgers()
        held_identity = next(row['id'] for row in readonly_rows(pair.root / 'queue.sqlite', 'jobs')
            if row['run_id'] == held['run_id'])
        pair.clock.advance(61)
        pair.reopen()
        assert pair.dispatched(1, pair.dispatch(1)) is None and len(pair.calls()) == 1
        pair.stop_all()
        pair.fault('queue.sqlite', omitted=('jobs',) if queue_fault == 'jobs_table' else (),
            empty=queue_fault == 'empty_database', absent=queue_fault == 'missing_database',
            update=('UPDATE jobs SET state=? WHERE id=?', ('completed', held_identity))
                if queue_fault == 'running_index' else None)
        pair.start(0)
        pair.start(1)
        for index in (0, 1):
            response = pair.client(index, operator=True).get('/monitor/queue')
            assert response.status_code in (200, 503), response.text
        new_task = {**TASK, 'run_id': 'queue-lost-state-disjoint-new-task'}
        response = pair.client(1, principal='agent').post('/v1/tasks', json=new_task)
        assert response.status_code in (202, 429, 503), response.text
        if response.status_code == 202:
            pair.dispatched(1, pair.dispatch(1))
        assert len(pair.calls()) == 1, 'Lost queue jobs reset global capacity and authorized another physical POST'
        assert pair.receipt_hashes() == original_receipts and pair.ledgers() == original_fees
        for index in (0, 1):
            saved = pair.client(index).get('/v1/tasks/' + held['run_id'])
            assert saved.status_code in (200, 503), saved.text
            if saved.status_code == 200:
                assert saved.json()['queue_state'] in {'running', 'interrupted_pending'}
                assert saved.json()['queue']['deadline'] == first.json()['queue']['deadline']
                assert saved.json()['costs']['unknown_events'] == 1
        assert known_fees(pair) == (Decimal(0), 1)
        pair.originals_unchanged()


@pytest.mark.parametrize('role,missing_database', [
    ('capacity', False), ('capacity', True), ('queue', False), ('queue', True)])
def test_lost_companion_cannot_create_new_trusted_incarnation_or_authorize_paid_work(resilience_process_pair, role, missing_database):
    limits = {'capacity': 8, 'concurrency': 2 if role == 'capacity' else 1,
        'per_principal': 4, 'max_wait_seconds': 1800}
    with resilience_process_pair(first_mode='kill', separate_budgets=True, disjoint=True, limits=limits) as pair:
        pair.activate()
        pair.mode('agent', 'open_source_first')
        task = {**TASK, 'run_id': 'original-before-companion-loss'}
        accepted = pair.client(0).post('/v1/tasks', json=task)
        assert accepted.status_code == 202, accepted.text
        pair.dispatch(0)
        pair.entered(0)
        pair.finish(0, kill=True)
        pair.start(0)
        pair.mode('studio', 'fixed_high')
        original_receipts, original_fees = pair.receipt_hashes(), pair.ledgers()
        pair.clock.advance(61)
        pair.reopen()
        state = pair.client(1).get('/v1/tasks/' + task['run_id'])
        assert state.status_code == 200 and state.json()['queue']['deadline'] == accepted.json()['queue']['deadline']
        pair.stop_all()
        pair.retain_missing_directory(role + '.sqlite.preservation')
        if missing_database:
            pair.fault(role + '.sqlite', absent=True)
        # Startup refusal is retained as a distinct incomplete observation case;
        # it must never be silently counted as full HTTP resilience success.
        pair.start(0)
        pair.start(1)
        before = pair.protected_hashes()
        for index in (0, 1):
            for path in ('/monitor/capacity', '/monitor/queue'):
                response = pair.client(index, operator=True).get(path)
                assert response.status_code in (200, 503), response.text
                assert str(pair.root) not in response.text and '.preservation' not in response.text
        assert pair.protected_hashes() == before
        principal = 'studio' if role == 'capacity' else 'agent'
        newcomer = {**TASK, 'run_id': 'new-after-companion-loss'}
        response = pair.client(1, principal=principal).post('/v1/tasks', json=newcomer)
        assert response.status_code in (202, 429, 503), response.text
        if response.status_code == 202:
            pair.dispatched(1, pair.dispatch(1))
        assert len(pair.calls()) == 1, 'A missing companion was silently replaced by a fresh paid-work authority'
        assert pair.receipt_hashes() == original_receipts and pair.ledgers() == original_fees
        assert known_fees(pair) == (Decimal(0), 1)
        pair.originals_unchanged()


def test_expired_identifiable_queued_fault_does_not_block_other_authorized_principal(resilience_process_pair):
    limits = {'capacity': 8, 'concurrency': 1, 'per_principal': 4, 'max_wait_seconds': 10}
    with resilience_process_pair(separate_budgets=True, disjoint=True, limits=limits) as pair:
        pair.activate()
        pair.mode('agent', 'open_source_first')
        second = pair.client(1, principal='agent')
        control = {**TASK, 'run_id': 'expired-local-fault-b-control'}
        assert second.post('/v1/tasks', json=control).status_code == 202
        assert pair.dispatched(1, pair.dispatch(1))['status'] == 'passed'
        task_a = {**TASK, 'run_id': 'expired-identifiable-queued-a'}
        accepted = pair.client(0).post('/v1/tasks', json=task_a)
        assert accepted.status_code == 202 and len(pair.calls()) == 1, accepted.text
        original_fees, original_receipts = pair.ledgers(), pair.receipt_hashes()
        pair.reopen()
        observed = pair.client(0).get('/v1/tasks/' + task_a['run_id'])
        assert observed.status_code == 202 and observed.json()['queue']['deadline'] == accepted.json()['queue']['deadline']
        row = next(row for row in readonly_rows(pair.root / 'queue.sqlite', 'jobs') if row['run_id'] == task_a['run_id'])
        pair.stop_all()
        pair.fault('queue.sqlite', update=('UPDATE jobs SET reason=? WHERE id=?',
            ('queue_waiting_capacity', row['id'])))
        pair.clock.advance(11)
        pair.start(0)
        pair.start(1)
        task_b = {**TASK, 'run_id': 'expired-local-fault-eligible-b'}
        response = pair.client(1, principal='agent').post('/v1/tasks', json=task_b)
        assert response.status_code == 202, response.text
        completed = pair.dispatched(1, pair.dispatch(1))
        assert completed is not None and completed['run_id'] == task_b['run_id'] and completed['status'] == 'passed', completed
        assert len(pair.calls()) == 2 and all('example/open_source' in call['content'] for call in pair.calls())
        assert all(pair.receipt_hashes()[path] == value for path, value in original_receipts.items())
        untouched = next(name for name, ledger in original_fees.items() if name.endswith('demo-cloudyfan.sqlite'))
        assert pair.ledgers()[untouched] == original_fees[untouched]
        assert known_fees(pair)[1] == 0
        current = next(item for item in readonly_rows(pair.root / 'queue.sqlite', 'jobs') if item['id'] == row['id'])
        assert current == {**row, 'reason': 'queue_waiting_capacity'}
        pair.originals_unchanged()


def test_actual_backup_health_stores_survive_killed_process_without_retry_or_observation_mutation(resilience_process_pair):
    from candidate_health_fixture import health_values
    with resilience_process_pair(health_core=True, first_mode='kill') as pair:
        pair.activate()
        task = {**TASK, 'category': health_values()['plan']['category_id'], 'run_id': 'actual-backup-health-process'}
        accepted = pair.client(0).post('/v1/tasks', json=task)
        assert accepted.status_code == 202, accepted.text
        pair.dispatch(0)
        pair.entered(0)
        capacity = pair.capacity(1)
        assert len(capacity['allocations']) == 1 and capacity['allocations'][0]['state'] == 'sent'
        assert len(pair.calls()) == 1 and pair.calls()[0]['method'] == 'POST'
        health_before = {str(path.relative_to(pair.root)): path.read_bytes()
            for path in (pair.root / 'health').rglob('*') if path.is_file()}
        assert health_before
        pair.finish(0, kill=True)
        original_receipts, original_fees = pair.receipt_hashes(), pair.ledgers()
        assert sum(path.endswith('intent.json') for path in original_receipts) == 1
        assert not any(path.endswith('response.json') for path in original_receipts)
        assert known_fees(pair) == (Decimal(0), 1)
        pair.clock.advance(61)
        pair.start(0)
        before = pair.protected_hashes()
        for index in (0, 1):
            client = pair.client(index)
            own = client.get('/v1/tasks/' + task['run_id'])
            assert own.status_code == 200 and own.json()['queue_state'] == 'running', own.text
            assert own.json()['queue']['deadline'] == accepted.json()['queue']['deadline']
            assert own.json()['costs']['unknown_events'] == 1
            assert client.post('/v1/tasks', json=task).json()['queue_state'] == 'running'
            assert client.post('/v1/tasks/' + task['run_id'] + '/resume', json={}).json()['queue_state'] == 'running'
            pair.capacity(index)
            pair.view('/monitor/queue', index)
        assert pair.protected_hashes() == before
        assert {str(path.relative_to(pair.root)): path.read_bytes()
            for path in (pair.root / 'health').rglob('*') if path.is_file()} == health_before
        assert pair.receipt_hashes() == original_receipts and pair.ledgers() == original_fees
        assert len(pair.calls()) == 1
        pair.stop_all()
        audits = list(pair.root.glob('health-process-side-requests-*.json'))
        assert len(audits) == 2  # One surviving original host and one reopened host; the killed child has no exit audit.
        for path in audits:
            audit = json.loads(path.read_text())
            assert audit['side_requests'] == {'source': [], 'accounting': []}
            assert all(request['method'] == 'POST' for request in audit['requests'])
        boundary_requests = [json.loads(path.read_text()) for path in pair.root.glob('private-request-*.json')]
        assert len(boundary_requests) == 1 and boundary_requests[0]['method'] == 'POST'
        assert not list(pair.root.glob('private-source-request-*.json'))
        assert not list(pair.root.glob('private-accounting-request-*.json'))


def test_cancelled_accepting_identity_without_original_job_is_readable_and_never_recreated(resilience_process_pair):
    # A real OS storage interruption preserves the accepted journal before the
    # original TaskService job exists, without replacing product collaborators.
    with resilience_process_pair() as pair:
        pair.activate()
        task = {**TASK, 'run_id': 'accepting-before-original-job'}
        first, second = pair.client(0), pair.client(1)
        with pair.original_task_lock('studio', task['run_id']) as directory:
            accepted = first.post('/v1/tasks', json=task)
            assert accepted.status_code == 202 and accepted.json()['queue_state'] == 'accepting', accepted.text
            assert not (directory / 'job.json').exists()
            assert len(pair.calls()) == 0 and pair.receipt_hashes() == {}
            state = second.get('/v1/tasks/' + task['run_id'])
            assert state.status_code == 202 and state.json()['queue_state'] == 'accepting', state.text
            cancelled = second.post('/v1/tasks/' + task['run_id'] + '/cancel', json={})
            assert cancelled.status_code == 200 and cancelled.json()['queue_state'] == 'cancelled', cancelled.text
            assert not (directory / 'job.json').exists()
        pair.clock.advance(61)
        pair.reopen()
        original = pair.protected_hashes()
        for index in (0, 1):
            client = pair.client(index)
            for response in (client.get('/v1/tasks/' + task['run_id']),
                    client.post('/v1/tasks', json=task), client.post('/v1/tasks/' + task['run_id'] + '/resume', json={})):
                assert response.status_code == 200 and response.json()['queue_state'] == 'cancelled', response.text
                assert response.json()['queue']['accepted_at'] == accepted.json()['queue']['accepted_at']
                assert response.json()['queue']['deadline'] == accepted.json()['queue']['deadline']
            pair.view('/monitor/queue', index)
        assert pair.protected_hashes() == original
        assert pair.dispatched(0, pair.dispatch(0)) is None
        assert not (directory / 'job.json').exists()
        assert pair.calls() == [] and pair.receipt_hashes() == {}
        assert all(not ledger['events'] and not ledger['reservations'] for ledger in pair.ledgers().values())
