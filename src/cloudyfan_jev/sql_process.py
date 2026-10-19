"""Bounded private pipes to a disposable stdlib-only SQL worker.

This is process/resource containment, not a general OS security sandbox. The
portable policy caps SQLite's allocator; total address-space enforcement is a
separate, fail-closed opt-in because Darwin may not implement RLIMIT_AS.
"""
import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
import platform
import selectors
import subprocess
import sys
import time

from . import sql_executor
from .sql_isolation import IsolationUnavailable, LinuxIsolation


VERSION = 'sql-worker-v2'
WALL_SECONDS = 3.0
CPU_SECONDS = 2
SQLITE_HEAP_BYTES = 32 * 1024 * 1024
ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024


class WorkerUnavailable(RuntimeError):
    def __init__(self, code='SQL_VALIDATION_WORKER'):
        self.code = code
        super().__init__('SQL worker unavailable')


def constraints(require_address_space=False):
    folder = Path(__file__).resolve().parent
    return {
        'version': VERSION, 'wall_seconds': WALL_SECONDS, 'cpu_seconds': CPU_SECONDS,
        'sqlite_heap_bytes': SQLITE_HEAP_BYTES, 'address_space_bytes': ADDRESS_SPACE_BYTES,
        'require_address_space': require_address_space,
        'max_request_bytes': MAX_REQUEST_BYTES, 'max_response_bytes': MAX_RESPONSE_BYTES,
        'python': list(sys.version_info[:3]), 'platform': sys.platform,
        'python_identity': {'implementation': sys.implementation.name,
                            'version': sys.version, 'cache_tag': sys.implementation.cache_tag},
        'kernel': platform.release(), 'machine': platform.machine(),
        'implementation_sha256': {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                  for name in ('sql.py', 'sql_process.py', 'sql_worker.py',
                                               'sql_executor.py')},
    }


def _exchange(process, payload, deadline):
    """Bound input/output and wall time, including pipe backpressure and exit."""
    output = bytearray()
    offset = 0
    with selectors.DefaultSelector() as selector:
        for pipe in (process.stdin, process.stdout):
            os.set_blocking(pipe.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerUnavailable('SQL_VALIDATION_TIMEOUT')
            for key, _ in selector.select(min(remaining, .05)):
                pipe = key.fileobj
                if pipe is process.stdin:
                    try:
                        count = os.write(pipe.fileno(), payload[offset:offset + 8192])
                    except BrokenPipeError:
                        raise WorkerUnavailable() from None
                    offset += count
                    if offset == len(payload):
                        selector.unregister(pipe)
                        pipe.close()
                else:
                    chunk = os.read(pipe.fileno(), min(8192, MAX_RESPONSE_BYTES + 1 - len(output)))
                    if not chunk:
                        selector.unregister(pipe)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE_BYTES:
                            raise WorkerUnavailable('SQL_VALIDATION_WORKER_OUTPUT')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerUnavailable('SQL_VALIDATION_TIMEOUT')
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise WorkerUnavailable('SQL_VALIDATION_TIMEOUT') from None
    if returncode:
        raise WorkerUnavailable('SQL_VALIDATION_WORKER_EXIT')
    try:
        response = json.loads(output)
    except (ValueError, UnicodeError):
        raise WorkerUnavailable('SQL_VALIDATION_WORKER_PROTOCOL') from None
    if not isinstance(response, dict) or response.get('protocol') != VERSION:
        raise WorkerUnavailable('SQL_VALIDATION_WORKER_PROTOCOL')
    if response.get('status') != 'ok':
        raise WorkerUnavailable('SQL_VALIDATION_ENVIRONMENT')
    if set(response) != {'protocol', 'status', 'result', 'resources'}:
        raise WorkerUnavailable('SQL_VALIDATION_WORKER_PROTOCOL')
    return response


def execute(tables, columns, data, sql, *, require_address_space=False, isolation=None):
    if isolation is not None and (not isinstance(isolation, LinuxIsolation) or require_address_space):
        raise WorkerUnavailable('SQL_ISOLATION_POLICY')
    try:
        with isolation.lease() if isolation is not None else nullcontext(None) as command:
            return _execute(tables, columns, data, sql, require_address_space=require_address_space,
                            command=command)
    except IsolationUnavailable as error:
        raise WorkerUnavailable(error.code) from None


def _execute(tables, columns, data, sql, *, require_address_space=False, command=None):
    payload = json.dumps({'tables': tables, 'columns': columns, 'data': data, 'sql': sql},
                         ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise WorkerUnavailable('SQL_VALIDATION_WORKER_INPUT')
    worker = Path(__file__).resolve().with_name('sql_worker.py')
    command = command or [sys.executable, '-I', '-S', '-B', str(worker)]
    if require_address_space:
        command.append('--require-address-space')
    process = None
    try:
        deadline = time.monotonic() + WALL_SECONDS
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={}, close_fds=True,
                                   cwd=worker.parent, start_new_session=True)
        response = _exchange(process, payload, deadline)
        resources = response['resources']
        if (not isinstance(resources, dict) or resources.get('sqlite_heap_bytes') != SQLITE_HEAP_BYTES
                or resources.get('python_identity') != {'implementation': sys.implementation.name,
                    'version': sys.version, 'cache_tag': sys.implementation.cache_tag}
                or resources.get('cpu_seconds') != CPU_SECONDS
                or resources.get('file_bytes') != 0 or resources.get('core_bytes') != 0
                or resources.get('open_files') != 32
                or resources.get('sqlite_constraints') != sql_executor._execution_constraints()
                or (require_address_space and resources.get('address_space_bytes') != ADDRESS_SPACE_BYTES)):
            raise WorkerUnavailable('SQL_VALIDATION_WORKER_PROTOCOL')
        result = response['result']
        if not isinstance(result, list) or len(result) != 3 or not isinstance(result[2], dict):
            raise WorkerUnavailable('SQL_VALIDATION_WORKER_PROTOCOL')
        return result, resources
    except WorkerUnavailable:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkerUnavailable() from None
    finally:
        if process is not None:
            # Reap on every path, including KeyboardInterrupt; never leave SQL running.
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe is not None and not pipe.closed:
                    pipe.close()
