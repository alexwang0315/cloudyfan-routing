"""Private worker entrypoint. No credentials, site packages, tools or model I/O."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys


def limits(require_address_space):
    # These values are mirrored and checked by sql_process; protocol changes
    # require a new worker version and a new frozen public contract.
    for name, cap in (('RLIMIT_CPU', 2), ('RLIMIT_CORE', 0), ('RLIMIT_FSIZE', 0),
                      ('RLIMIT_NOFILE', 32)):
        kind = getattr(resource, name)
        resource.setrlimit(kind, (cap, cap))
        if resource.getrlimit(kind) != (cap, cap):
            raise RuntimeError('resource contract unavailable')
    address_space = None
    if require_address_space:
        resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))
        if resource.getrlimit(resource.RLIMIT_AS) != (268435456, 268435456):
            raise RuntimeError('address space contract unavailable')
        address_space = 268435456
    import sqlite3
    with sqlite3.connect(':memory:') as connection:
        connection.execute('PRAGMA hard_heap_limit=33554432')
        value = connection.execute('PRAGMA hard_heap_limit').fetchone()
        if value != (33554432,):
            raise RuntimeError('SQLite allocator limit unavailable')
    return {'cpu_seconds': 2, 'sqlite_heap_bytes': 33554432,
            'python_identity': {'implementation': sys.implementation.name,
                                'version': sys.version, 'cache_tag': sys.implementation.cache_tag},
            'address_space_bytes': address_space, 'file_bytes': 0, 'core_bytes': 0,
            'open_files': 32, 'pid': os.getpid(), 'inherited_environment_keys': sorted(os.environ)}


def main():
    response = {'protocol': 'sql-worker-v2', 'status': 'unavailable'}
    try:
        resources = limits(sys.argv[1:] == ['--require-address-space'])
        payload = sys.stdin.buffer.read(2097153)
        if len(payload) > 2097152:
            raise ValueError('oversized request')
        request = json.loads(payload)
        if not isinstance(request, dict) or set(request) != {'tables', 'columns', 'data', 'sql'}:
            raise ValueError('invalid protocol')
        spec = importlib.util.spec_from_file_location(
            '_sql_executor', Path(__file__).resolve().with_name('sql_executor.py'))
        executor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(executor)
        resources['sqlite_constraints'] = executor._execution_constraints()
        result = executor._execute_fixture(request['tables'], request['columns'],
                                           request['data'], request['sql'], public=True)
        response.update(status='ok', result=result, resources=resources)
        encoded = json.dumps(response, ensure_ascii=True, allow_nan=False).encode()
        if len(encoded) > 262144:
            raise ValueError('oversized output')
    except Exception:
        encoded = b'{"protocol":"sql-worker-v2","status":"unavailable"}'
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    main()
