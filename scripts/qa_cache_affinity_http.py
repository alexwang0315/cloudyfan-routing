#!/usr/bin/env python3
"""Real main-service host for #47 cache-affinity desktop QA; Backup Core, upstreams MockTransport only."""
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
from history_fixture import assemble_backup_host  # noqa: E402
from test_local_http_integration import (  # noqa: E402
    LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)

PROMPT = 'Return only 1+1. PRIVATE_QA47_CANARY'


def fingerprint(path):
    """SQLite 依資料表內容（頁面輪詢可能重寫未變的列），其他檔案依位元組。"""
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
    receipt = {'schema_version': 'qa47-cache-affinity-host-v1', 'synthetic': True, 'fixture_pid': os.getpid(),
               'scope': 'Real main service/BackupCategoryCore/BudgetedCore/Store/Identity; upstreams MockTransport only'}
    thread = None
    try:
        host = assemble_backup_host(output / 'service', origin)
        plan = host.values['plan']
        # 合成登記：示範供應商的快取支援與資料政策相容；來源 TTL 未提供。
        host.app.cache_support = {row['entry']['profile']['provider']: {'source_ttl_seconds': None,
                                                                       'data_policy': 'compatible'}
                                  for rows in plan['pools'].values() for row in rows}
        server.set_app(host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        models = {row['entry']['config_id']: row['entry']['profile']['model_version']
                  for rows in plan['pools'].values() for row in rows}

        def state():
            return {'model_calls': len(host.calls), 'original_files_sha256': hashlib.sha256(json.dumps({
                str(path.relative_to(host.root)): fingerprint(path) for path in sorted(host.root.rglob('*'))
                if path.is_file() and '.saved-comparisons' not in path.parts and path.name != '.cache-observation.key'
                and not path.name.endswith(('-wal', '-shm'))}, sort_keys=True).encode()).hexdigest()}

        def create(run, unhealthy=None):
            target = next((row['configuration_sha256'] for rows in plan['pools'].values() for row in rows
                           if row['entry']['config_id'] == unhealthy), None)
            original = host.health.assess

            def assess(configuration, provider):
                result = original(configuration, provider)
                return ({**result, 'reasons': [*result['reasons'], 'synthetic_unhealthy_for_qa']}
                        if configuration == target else result)

            host.health.assess = assess
            try:
                before = len(host.calls)
                with httpx.Client(base_url=origin, trust_env=False, timeout=10, headers={
                        'Authorization': 'Bearer ' + host.credentials['studio'].token}) as customer:
                    response = customer.post('/v1/tasks', json={'run_id': run, 'category': plan['category_id'],
                                                                'prompt': PROMPT})
                check(response.status_code == 200, 'qa_original_task_not_created')
                return host.calls[before]['model']
            finally:
                del host.health.assess

        print(json.dumps({'type': 'ready', 'origin': origin, 'synthetic': True, 'fixture_pid': os.getpid(),
                          'operator_token': host.operator_token, 'models': models}), flush=True)
        baseline = None
        for line in sys.stdin:
            message = json.loads(line)
            command = message.get('command')
            if command == 'stop':
                break
            if command == 'create-pair':
                first = create('qa47-first', unhealthy='open_source-0')
                second = create('qa47-second')
                baseline = state()
                receipt['baseline'] = baseline
                print(json.dumps({'type': command, 'first_model': first, 'second_model': second, 'state': baseline}),
                      flush=True)
                continue
            check(command == 'state', 'qa_unknown_private_command')
            print(json.dumps({'type': 'state', 'state': state()}), flush=True)
        receipt['final'] = state()
        receipt['readonly_verified'] = baseline is not None and receipt['final'] == baseline
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
