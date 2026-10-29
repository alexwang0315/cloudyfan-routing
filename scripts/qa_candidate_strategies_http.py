#!/usr/bin/env python3
"""Real main-service host for candidate-strategy desktop QA; upstreams MockTransport only."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from candidate_observation_fixture import assemble_candidate_host  # noqa: E402
from test_local_http_integration import (  # noqa: E402
    Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)


def check(value, code):
    if not value:
        raise AssertionError(code)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    boundary = LoopbackEvidence()
    configure_loopback_network(boundary, setattr)
    server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
    boundary.ports.add(server.server_port)
    origin = f'http://127.0.0.1:{server.server_port}'
    receipt = {'schema_version': 'qa43-candidate-strategies-host-v1', 'synthetic': True, 'fixture_pid': os.getpid(),
               'scope': 'Real main service/CategoryCore/BudgetedCore/Store/Identity; upstreams MockTransport only'}
    host = thread = None
    try:
        clock = Clock(datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp())
        host = assemble_candidate_host(output / 'service', origin, clock=clock, fail_first=True, classify=True)
        server.set_app(host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()

        def state():
            return {'model_calls': len(host.calls), 'original_files_sha256': hashlib.sha256(json.dumps({
                str(path.relative_to(host.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(host.root.rglob('*')) if path.is_file()
                and not str(path.relative_to(host.root)).startswith('.saved-comparisons/')},
                sort_keys=True).encode()).hexdigest(), 'budget': {key: host.budget.report().get(key)
                for key in ('known_usd', 'reserved_usd', 'unknown_events')}}

        with httpx.Client(base_url=origin, trust_env=False, timeout=10, headers={
                'Authorization': 'Bearer ' + host.credentials['studio'].token}) as customer:
            created = customer.post('/v1/tasks', json={'run_id': 'qa43-classified', 'category': host.category,
                                                        'prompt': '1+1 PRIVATE_QA43_CANARY'})
            check(created.status_code == 200 and created.json()['status'] == 'passed', 'qa_original_task_not_passed')
            check(len(host.calls) == 3, 'qa_original_classification_path_missing')
            baseline = state()
            receipt['baseline'] = baseline
            print(json.dumps({'type': 'ready', 'origin': origin, 'synthetic': True, 'fixture_pid': os.getpid(),
                              'operator_token': host.operator_token, 'state': baseline}), flush=True)
            for line in sys.stdin:
                command = json.loads(line).get('command')
                if command == 'stop':
                    break
                check(command == 'state', 'qa_unknown_private_command')
                result = {'type': 'state', 'state': state()}
                result['customer_report_denied'] = customer.get('/monitor/comparisons').status_code == 401
                print(json.dumps(result), flush=True)
            receipt['final'] = state()
            receipt['readonly_verified'] = receipt['final'] == baseline
    except BaseException as error:
        receipt['failure_code'] = str(error) if isinstance(error, AssertionError) else 'qa_host_failed'
        raise
    finally:
        if thread is not None and thread.is_alive():
            server.shutdown()
            thread.join(2)
        server.server_close()
        receipt['blocked_socket_attempt_count'] = len(boundary.blocked)
        receipt['socket_attempts'] = [{'host': address[0], 'port': address[1]} for address in boundary.connected]
        with (output / 'fixture-receipt.json').open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    check(receipt.get('readonly_verified') and not boundary.blocked, 'qa_readonly_or_network_guard_failed')


if __name__ == '__main__':
    main()
