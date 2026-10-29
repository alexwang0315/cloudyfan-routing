#!/usr/bin/env python3
"""Real main-service host for paired saved-outcome desktop QA; upstreams MockTransport only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from cloudyfan_monitor.demo import CATEGORY  # noqa: E402
from cloudyfan_monitor.integration import build_local_integration  # noqa: E402
from test_local_http_integration import (  # noqa: E402
    Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)

# (client, run, mode, failing tiers, prompt): one equal-quality pair, one differing-quality pair.
TASKS = (('studio', 'qa44-equal-oss', 'open_source_first', (), '1+1 PRIVATE_QA44_CANARY'),
         ('agent', 'qa44-equal-high', 'fixed_high', (), '1+1 PRIVATE_QA44_CANARY'),
         ('studio', 'qa44-rescued', 'cloudyfan', ('open_source',), '2+2 PRIVATE_QA44_CANARY'),
         ('batch', 'qa44-failed', 'open_source_first', ('open_source', 'low', 'mid', 'high'), '2+2 PRIVATE_QA44_CANARY'))


def fingerprint(path):
    """SQLite by table contents (page polling may rewrite unchanged rows), other files by bytes."""
    if path.suffix in ('.sqlite', '.sqlite3'):
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            rows = {name: sorted(map(repr, db.execute('SELECT * FROM "' + name + '"').fetchall())) for name in tables}
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    receipt = {'schema_version': 'qa44-paired-outcomes-host-v1', 'synthetic': True, 'fixture_pid': os.getpid(),
               'scope': 'Real main service/Core/BudgetedCore/Store/Identity; upstreams MockTransport only'}
    thread = None
    try:
        bundle = build_local_integration(output / 'service', origin=origin, clock=Clock(), seed=False)
        server.set_app(bundle.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()

        def state():
            return {'model_calls': len(bundle.upstream.calls), 'original_files_sha256': hashlib.sha256(json.dumps({
                str(path.relative_to(bundle.root)): fingerprint(path)
                for path in sorted(bundle.root.rglob('*')) if path.is_file()
                and '.saved-comparisons' not in path.parts and not path.name.endswith(('-wal', '-shm'))}, sort_keys=True).encode()).hexdigest()}

        with httpx.Client(base_url=origin, trust_env=False, timeout=10,
                          headers={'Authorization': 'Bearer ' + bundle.operator_token}) as operator:
            for client, run, mode, fail, prompt in TASKS:
                revision = operator.get('/monitor/snapshot').json()['control']['revision']
                check(operator.post('/monitor/mode', json={'revision': revision, 'mode': mode}).status_code == 200,
                      'qa_mode_switch_failed')
                bundle.upstream.fail_tiers = set(fail)
                with httpx.Client(base_url=origin, trust_env=False, timeout=10, headers={
                        'Authorization': 'Bearer ' + bundle.credentials[client].token}) as customer:
                    created = customer.post('/v1/tasks', json={'run_id': run, 'category': CATEGORY, 'prompt': prompt})
                    check(created.status_code in (200, 409), 'qa_original_task_not_created')
            bundle.upstream.fail_tiers = set()
            baseline = state()
            receipt['baseline'] = baseline
            print(json.dumps({'type': 'ready', 'origin': origin, 'synthetic': True, 'fixture_pid': os.getpid(),
                              'operator_token': bundle.operator_token, 'state': baseline,
                              'tasks': [[client, run] for client, run, *_ in TASKS]}), flush=True)
            with httpx.Client(base_url=origin, trust_env=False, timeout=10, headers={
                    'Authorization': 'Bearer ' + bundle.credentials['studio'].token}) as customer:
                for line in sys.stdin:
                    command = json.loads(line).get('command')
                    if command == 'stop':
                        break
                    check(command == 'state', 'qa_unknown_private_command')
                    print(json.dumps({'type': 'state', 'state': state(),
                        'customer_report_denied': customer.get('/monitor/comparisons').status_code == 401}), flush=True)
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
