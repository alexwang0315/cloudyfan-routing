#!/usr/bin/env python3
"""Real main-service host for #51 context fallback and rejection desktop QA; upstreams MockTransport only."""
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

# 一個參考區塊任務（大區塊會被整塊省略）與一個舊格式任務。
CONTEXT = {'format': 'reference-blocks-v1', 'compression': 'extractive-v1', 'blocks': [
    {'id': 'PRIVATE_QA51_BLOCK_ID', 'text': 'PRIVATE_QA51_REQUIRED 必要：答案是 2。', 'omittable': False, 'priority': 0},
    {'id': 'small', 'text': 'S' * 40, 'omittable': True, 'priority': 1}]}
TOO_BIG = {'format': 'reference-blocks-v1', 'compression': 'extractive-v1', 'blocks': [
    {'id': 'huge', 'text': 'PRIVATE_QA51_HUGE ' + 'H' * 900, 'omittable': False, 'priority': 0}]}
POLICY = {'version': 'extractive-v1', 'max_input_bytes': 8192, 'max_blocks': 8, 'max_block_bytes': 2048,
          'max_prepared_bytes': 700, 'framing_reserve_bytes': 100, 'output_reserve_bytes': 100,
          'allow_omit': True, 'token_guarantee': 'not_required', 'max_prepare_ms': 5, 'fallback': 'original_if_fits',
          'qualification': {'content_preparation_version': 'extractive-v1', 'scope': 'synthetic-wiring-only',
                            'synthetic': True, 'evidence_sha256': 'b' * 64}}
TASKS = (('studio', 'qa51-fallback', 'open_source_first', (), 'Return only 1+1. PRIVATE_QA51_CANARY'),
         ('studio', 'qa51-rejected', 'open_source_first', (), '2+2 PRIVATE_QA51_CANARY'))


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
    receipt = {'schema_version': 'qa51-context-fallback-host-v1', 'synthetic': True, 'fixture_pid': os.getpid(),
               'scope': 'Real main service/Core/BudgetedCore/Store/Identity; upstreams MockTransport only'}
    thread = None
    try:
        bundle = build_local_integration(output / 'service', origin=origin, clock=Clock(), seed=False)
        # 合成登記：示範供應商的快取支援與資料政策相容；來源 TTL 未提供。
        # 合成登記：這個契約的參考區塊準備政策（資格明確標示為合成接線）。
        from dataclasses import replace
        contracts = bundle.app.gateway.service.contracts
        contracts['demo-open_source_first'] = replace(contracts['demo-open_source_first'], context_policy=POLICY)
        server.set_app(bundle.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()

        def state():
            return {'model_calls': len(bundle.upstream.calls), 'original_files_sha256': hashlib.sha256(json.dumps({
                str(path.relative_to(bundle.root)): fingerprint(path)
                for path in sorted(bundle.root.rglob('*')) if path.is_file()
                and '.saved-comparisons' not in path.parts and path.name != '.cache-observation.key'
                and not path.name.endswith(('-wal', '-shm'))}, sort_keys=True).encode()).hexdigest()}

        with httpx.Client(base_url=origin, trust_env=False, timeout=10,
                          headers={'Authorization': 'Bearer ' + bundle.operator_token}) as operator:
            for client, run, mode, fail, prompt in TASKS:
                revision = operator.get('/monitor/snapshot').json()['control']['revision']
                check(operator.post('/monitor/mode', json={'revision': revision, 'mode': mode}).status_code == 200,
                      'qa_mode_switch_failed')
                bundle.upstream.fail_tiers = set(fail)
                with httpx.Client(base_url=origin, trust_env=False, timeout=10, headers={
                        'Authorization': 'Bearer ' + bundle.credentials[client].token}) as customer:
                    from cloudyfan_jev import context_preparation
                    clock = context_preparation._now
                    if run == 'qa51-fallback':
                        # 可控時鐘：模擬準備逾時，政策允許時付費前回退原文。
                        ticks = iter([0.0, 0.05])
                        context_preparation._now = lambda: next(ticks, 0.0)
                    try:
                        created = customer.post('/v1/tasks', json={'run_id': run, 'category': CATEGORY, 'prompt': prompt,
                            'context': CONTEXT if run == 'qa51-fallback' else TOO_BIG})
                    finally:
                        context_preparation._now = clock
                    check(created.status_code == (200 if run == 'qa51-fallback' else 413), 'qa_original_task_unexpected')
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
