"""One offline verification entry point for local checkouts and CI."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--quick', action='store_true', help='locked sync and Ruff only (commit hook)')
    modes.add_argument('--integration', action='store_true', help='also run both mock demos and build')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    uv = shutil.which('uv')
    if uv is None:
        print('uv is required; no fallback to global pytest or pip.', file=sys.stderr)
        return 127
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'UV_NO_SYNC', 'UV_FROZEN', 'UV_NO_DEV'):
        env.pop(key, None)
    env.update(UV_PROJECT_ENVIRONMENT=str(root / '.venv'), UV_OFFLINE='1', UV_PYTHON_DOWNLOADS='never')
    python = root / '.venv/bin/python'
    commands = [
        [uv, 'sync', '--project', str(root), '--locked', '--offline', '--extra', 'dev',
         '--no-python-downloads'],
        [uv, 'pip', 'check', '--python', str(python)],
        [str(python), '-m', 'ruff', 'check', '.'],
    ]
    if not args.quick:
        commands.append([str(python), '-m', 'pytest', '-q'])
    output = root / 'runtime/checks' / uuid.uuid4().hex
    if args.integration:
        commands.extend([
            [str(root / '.venv/bin/cloudyfan-jev-demo'), '--route', 'high', '--output', str(output / 'core')],
            [str(root / '.venv/bin/cloudyfan-score-demo'), '--route', 'high', '--output', str(output / 'scores')],
            [uv, 'build', '--offline', '--no-python-downloads',
             '--python', str(python), '--out-dir', str(output / 'dist')],
        ])
    output.mkdir(parents=True)
    report = {'mode': 'integration' if args.integration else 'quick' if args.quick else 'standard',
              'started_at': datetime.now(timezone.utc).isoformat(), 'steps': [], 'passed': False}
    result = 0
    for command in commands:
        print('+ ' + ' '.join(command), flush=True)
        try:
            result = subprocess.run(command, cwd=root, env=env, check=False).returncode
        except OSError as exc:
            print(f'Cannot start verification command: {exc}', file=sys.stderr)
            result = 127
        report['steps'].append({'command': command, 'returncode': result})
        (output / 'checks.json').write_text(json.dumps(report, indent=2) + '\n')
        if result:
            break
    report['passed'] = result == 0
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    (output / 'checks.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Verification {"PASS" if result == 0 else "FAIL"}: {output / "checks.json"}')
    return result if result >= 0 else 128 - result


if __name__ == '__main__':
    sys.exit(main())
