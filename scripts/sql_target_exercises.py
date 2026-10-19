"""Opt-in, serial synthetic Linux probes; never imported by the runtime package.

OS probes replace only the fixed sandbox Python program with trusted literals.
They are envelope evidence, NOT execution of the SQL interpreter. The separate
interruption check uses the unchanged real SQL worker and Core.run/MockTransport.
No cgroup provisioning, host policy changes, external traffic or paid calls.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from cloudyfan_jev import sql_process


MEMORY_PROBE = '''import os,time
r,w=os.pipe()
pid=os.fork()
if pid==0:
 os.close(r)
 allocation=bytearray(160*1024*1024)
 os.write(w,b'1')
 time.sleep(30)
else:
 os.close(w)
 assert os.read(r,1)==b'1'
 print('DESCENDANT_ALLOCATED',flush=True)
 allocation=bytearray(160*1024*1024)
 time.sleep(30)
'''
CPU_PROBE = '''import os
os.fork()
print('CPU_READY',flush=True)
while True: pass
'''
WAIT_PROBE = '''import os,time
os.fork()
print('WAIT_READY',flush=True)
time.sleep(30)
'''


def counters(path):
    return {k: int(v) for k, v in (line.split() for line in path.read_text().splitlines())}


def empty_leaf(policy):
    leaf = Path(policy.cgroup)
    return (not (leaf/'cgroup.procs').read_text().strip()
            and 'populated 0' in (leaf/'cgroup.events').read_text().splitlines())


def probe(policy, program, *, timeout=7, cancel=False):
    """Same verified lease/flags; only a trusted qualification program differs."""
    process = None
    started = None
    requested_cancellation = False
    record = {'subject': 'trusted probe in runtime sandbox envelope',
              'program_sha256': hashlib.sha256(program.encode()).hexdigest(),
              'timed_out': False, 'cancelled': False}
    try:
        with policy.lease() as command:
            args = json.loads(command[-1])
            assert args == policy.command() and args[-1] == '/app/sql_worker.py'
            args[-1:] = ['-c', program]
            command[-1] = json.dumps(args, separators=(',', ':'))
            record['sandbox_command'] = args
            started = time.monotonic()
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env={}, close_fds=True,
                                       start_new_session=True)
            try:
                out, err = process.communicate(timeout=.5 if cancel else timeout)
            except subprocess.TimeoutExpired as error:
                record['stdout'] = (error.output or b'').decode(errors='replace')
                if cancel:
                    if 'WAIT_READY' not in record['stdout']:
                        raise RuntimeError('Cancellation probe never reached its ready marker') from None
                    requested_cancellation = True
                    raise KeyboardInterrupt
                record['timed_out'] = True
            except BaseException:
                raise
            else:
                record['stdout'] = out.decode(errors='replace')
                record['stderr'] = err.decode(errors='replace')
    except KeyboardInterrupt:
        if not requested_cancellation:
            raise
        record['cancelled'] = True
    finally:
        # The lease kills the entire leaf first, including descendants. Reap the
        # direct host child too, even when communicate or a probe assertion fails.
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            for stream in (process.stdout, process.stderr):
                stream.close()
            record['returncode'] = process.returncode
        record['seconds'] = None if started is None else time.monotonic()-started
        record['leaf_empty_after'] = empty_leaf(policy)
    return record


def memory_evidence(policy, output):
    path = Path(policy.cgroup)/'memory.events'
    before = counters(path)
    row = probe(policy, MEMORY_PROBE)
    after = counters(path)
    row['memory_events_delta'] = {k: after[k]-v for k, v in before.items()}
    row['passed'] = (not row['timed_out'] and row['returncode'] != 0
                     and 'DESCENDANT_ALLOCATED' in row.get('stdout', '')
                     and row['memory_events_delta'].get('oom_kill', 0) > 0
                     and row['memory_events_delta'].get('oom_group_kill', 0) > 0
                     and row['leaf_empty_after'])
    return row


def cpu_evidence(policy, output):
    path = Path(policy.cgroup)/'cpu.stat'
    before = counters(path)
    row = probe(policy, CPU_PROBE)
    after = counters(path)
    row['cpu_stat_delta'] = {k: after[k]-v for k, v in before.items()}
    row['passed'] = (not row['timed_out'] and row['returncode'] != 0
                     and 'CPU_READY' in row.get('stdout', '')
                     and row['cpu_stat_delta']['usage_usec'] >= 1500000
                     and row['cpu_stat_delta']['nr_throttled'] > 0 and row['leaf_empty_after'])
    return row


def deadline_evidence(policy, output):
    row = probe(policy, WAIT_PROBE, timeout=sql_process.WALL_SECONDS)
    row['passed'] = (row['timed_out'] and 'WAIT_READY' in row.get('stdout', '')
                     and row['seconds'] < sql_process.WALL_SECONDS+2 and row['leaf_empty_after'])
    return row


def cancellation_evidence(policy, output):
    row = probe(policy, WAIT_PROBE, cancel=True)
    row['passed'] = row['cancelled'] and row['leaf_empty_after'] and row['returncode'] != 0
    return row


def filesystem_evidence(policy, output):
    sentinel = output/'host-synthetic-sentinel'
    with sentinel.open('x') as handle:
        handle.write('RG01 public synthetic sentinel; no private content\n')
    original = sentinel.read_bytes()
    program = '''import errno,json,os
p=PATH
results={}
for name,mode in [('host_read','rb'),('host_write','wb')]:
 try:
  with open(p,mode): pass
  results[name]=None
 except OSError as e: results[name]=e.errno
try:
 with open('/app/rg01-write-denial','wb') as f:f.write(b'x')
 results['root_write']=None
except OSError as e:results['root_write']=e.errno
results['cgroup_control_visible']=os.path.exists('/sys/fs/cgroup/cgroup.procs')
print(json.dumps(results))
'''.replace('PATH', repr(str(sentinel)))
    row = probe(policy, program)
    observed = json.loads(row.get('stdout', '{}'))
    import errno
    row['observed'] = observed
    row['host_sentinel_unchanged'] = sentinel.read_bytes() == original
    row['passed'] = (row['returncode'] == 0 and row['leaf_empty_after']
        and observed == {'host_read': errno.ENOENT, 'host_write': errno.ENOENT,
                         'root_write': errno.EROFS, 'cgroup_control_visible': False}
        and row['host_sentinel_unchanged'])
    return row


def network_evidence(policy, output):
    namespaces = ('user', 'ipc', 'pid', 'net', 'uts', 'cgroup')
    parent = {n: os.readlink('/proc/self/ns/'+n) for n in namespaces}
    # A positive control proves a synthetic host listener existed. Only our
    # ephemeral localhost port is tested; no DNS, Internet or service endpoint.
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(2)
        address = server.getsockname()
        with socket.create_connection(address, timeout=1):
            peer, _ = server.accept()
            peer.close()
        program = '''import json,os,socket
result={'namespaces':{n:os.readlink('/proc/self/ns/'+n) for n in NAMES}}
try:
 with socket.create_connection(ADDRESS,timeout=.5):pass
 result['host_connect']=True
except OSError:result['host_connect']=False
with open('/proc/self/status') as f:
 result['cap_eff']=next(line.split()[1] for line in f if line.startswith('CapEff:'))
print(json.dumps(result))
'''.replace('NAMES', repr(namespaces)).replace('ADDRESS', repr(address))
        row = probe(policy, program)
        observed = json.loads(row.get('stdout', '{}'))
        server.settimeout(.1)
        try:
            unexpected, _ = server.accept()
        except TimeoutError:
            reached_host = False
        else:
            unexpected.close()
            reached_host = True
    row['observed'] = observed
    row['host_namespaces'] = parent
    row['host_positive_control'] = True
    row['passed'] = (row['returncode'] == 0 and row['leaf_empty_after'] and not reached_host
                     and observed.get('host_connect') is False
                     and int(observed.get('cap_eff', '1'), 16) == 0
                     and all(observed.get('namespaces', {}).get(n) != parent[n] for n in namespaces))
    return row


def interrupted_answer_resume(policy, output):
    """SIGSTOP an actual SQL worker after mock answer persistence, then resume."""
    import httpx
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.demo import example_config
    from cloudyfan_jev.engine import Core
    from cloudyfan_jev.sql import SQL_DIAGNOSTICS, SqlValidationUnavailable
    from cloudyfan_jev.sql_catalog import contract
    calls = []
    injected = []
    stop = threading.Event()

    def pause_real_worker():
        while not stop.wait(.001):
            for text in (Path(policy.cgroup)/'cgroup.procs').read_text().split():
                try:
                    pid = int(text)
                    args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                    if args[:1] == [b'/runtime/bin/python3'] and b'/app/sql_worker.py' in args:
                        os.kill(pid, signal.SIGSTOP)
                        injected.append({'signal': 'SIGSTOP', 'subject': 'actual immutable SQL worker'})
                        return
                except (FileNotFoundError, ProcessLookupError):
                    continue

    def respond(request):
        if request.method != 'POST' or not request.url.path.endswith('/chat/completions'):
            raise RuntimeError('Only mock generation is permitted')
        payload = json.loads(request.content)
        calls.append({'method': request.method, 'model': payload['model']})
        return httpx.Response(200, json={'id': 'gen-synthetic-interruption', 'model': payload['model'],
            'provider': 'Example', 'usage': {'cost': 0.01}, 'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps({'sql': 'SELECT SUM(amount) AS total FROM orders'})}}]})

    task = contract('S01', isolation=policy)
    should_interrupt = True

    class InterruptibleEnvironment:
        def __init__(self):
            self.environment = task.environment()

        def tools_schema(self):
            return self.environment.tools_schema()

        def call(self, name, arguments):
            return self.environment.call(name, arguments)

        def validate(self, text):
            if not should_interrupt:
                return self.environment.validate(text)
            watcher = threading.Thread(target=pause_real_worker, daemon=True)
            watcher.start()
            try:
                return self.environment.validate(text)
            finally:
                stop.set()
                watcher.join(timeout=1)
                if watcher.is_alive():
                    raise RuntimeError('Interrupt watcher did not stop')

    profiles, decision = example_config()
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = Client(http, 'mock-only-target-evidence', output/'client')
        engine = Core(client, profiles, decision, diagnostic_contract=SQL_DIAGNOSTICS)
        store = BudgetStore(output/'budget.sqlite', clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))
        store.authorize(Authorization('synthetic-only', '1', '2026-10-19T00:00:00+00:00'))
        bounds = {p['model_version']: EnforcedBound('.03', 'synthetic') for p in profiles.values()}
        bounds[decision['version']] = EnforcedBound('.001', 'synthetic')
        controlled = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={},
                                  verify_bound=lambda *args: True)
        interrupted_code = None
        try:
            controlled.run('interrupted', task.task(), InterruptibleEnvironment,
                           contract_version=task.contract_version)
        except SqlValidationUnavailable as error:
            interrupted_code = error.code
        held = store.report()
        saved = {str(p.relative_to(client.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (client.root/'calls').glob('*/response.json')}
        if interrupted_code != 'SQL_VALIDATION_TIMEOUT' or not injected:
            return {'passed': False, 'interrupted_code': interrupted_code, 'fault_injected': injected,
                    'mock_posts': len(calls), 'leaf_empty_after': empty_leaf(policy)}
        should_interrupt = False
        result = controlled.run('interrupted', task.task(), InterruptibleEnvironment,
                                contract_version=task.contract_version)
        final = store.report()
        unchanged = all(hashlib.sha256((client.root/name).read_bytes()).hexdigest() == digest
                        for name, digest in saved.items())
        return {'subject': 'actual SQL worker and Core.run; model API mocked',
                'passed': (len(calls) == 1 and len(saved) == 1 and unchanged
                           and held['known_usd'] == '0.01' and held['reserved_usd'] != '0'
                           and result['status'] == 'passed' and result['resumed']
                           and final['reserved_usd'] == '0' and empty_leaf(policy)),
                'interrupted_code': interrupted_code, 'fault_injected': injected,
                'saved_responses_sha256': saved, 'saved_responses_unchanged': unchanged,
                'budget_after_interruption': held, 'budget_after_resume': final,
                'mock_posts': len(calls), 'resumed': result['resumed'],
                'status_after_resume': result['status'], 'leaf_empty_after': empty_leaf(policy)}


EXERCISES = (('total_memory_and_descendants', memory_evidence), ('cpu_limit', cpu_evidence),
             ('external_deadline', deadline_evidence), ('host_file_read_write_denial', filesystem_evidence),
             ('network_namespace_denial', network_evidence),
             ('cancellation_and_reaping', cancellation_evidence),
             ('interrupted_answer_and_budget_resume', interrupted_answer_resume))


def run_target_exercises(policy, output):
    """Explicit caller admission first; stop subsequent probes on any failure."""
    rows = []
    stopped = False
    for name, exercise in EXERCISES:
        if stopped:
            rows.append({'id': name, 'result': 'not_run'})
            continue
        folder = output/name
        folder.mkdir(exist_ok=False)
        try:
            detail = exercise(policy, folder)
            row = {'id': name, 'result': 'passed' if detail['passed'] else 'failed', 'detail': detail}
        except Exception as error:
            row = {'id': name, 'result': 'failed', 'error_type': type(error).__name__,
                   'error_code': getattr(error, 'code', None), 'error': str(error)}
        rows.append(row)
        (folder/'result.json').write_text(json.dumps(row, indent=2)+'\n')
        print(name+': '+row['result'], file=sys.stderr, flush=True)
        stopped = row['result'] != 'passed'
    return rows
