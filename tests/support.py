import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from cloudyfami_portal.http_link import PortalClient, PortalUnreachable

SRC = Path(__file__).resolve().parents[1] / 'src'


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def clean_env(**extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith('CLOUDYFAMI_')}
    return {**env, 'PYTHONPATH': str(SRC), 'HOME': extra.pop('HOME', env.get('HOME', '')), **extra}


def start_server(db, port, *args):
    process = subprocess.Popen(
        [sys.executable, '-B', '-m', 'cloudyfami_portal', 'serve', '--db', str(db), '--port', str(port), *args],
        env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe = PortalClient(f'http://127.0.0.1:{port}', 'probe', timeout=1, retry_delays=())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            probe._call('GET', '/health')
            return process
        except PortalUnreachable:
            time.sleep(0.1)
    process.kill()
    raise AssertionError('relay did not start')
