"""One offline verification entry point for local checkouts and CI."""

import argparse
import codecs
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid


def _stop_process_group(process, *, interrupted):
    # Popen creates this group; never signal the caller's group or scan PIDs.
    signals = (signal.SIGINT, signal.SIGTERM) if interrupted else (signal.SIGTERM,)
    for signum in signals:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            process.wait()
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process.poll()  # Reap the leader even if descendants are still alive.
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                process.wait()
                return
            except PermissionError:
                # A denied existence query does not establish an empty group.
                # Keep bounded escalation; actual signal errors still propagate.
                pass
            time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_logged(command, *, cwd, env, log_path) -> int:
    """Stream a child to a new private byte log and safely decoded console output.

    Interrupts stop only this child's new process group and reap the child, then
    propagate so the caller can record interruption. Launch errors are OSError.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as log:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while chunk := os.read(process.stdout.fileno(), 65536):
                log.write(chunk)
                log.flush()
                sys.stdout.write(decoder.decode(chunk))
                sys.stdout.flush()
            sys.stdout.write(decoder.decode(b'', final=True))
            sys.stdout.flush()
            return process.wait()
        except BaseException as exc:
            _stop_process_group(process, interrupted=isinstance(exc, KeyboardInterrupt))
            raise
        finally:
            process.stdout.close()


def write_report(path, report):
    """Replace the receipt atomically so an interrupted write stays readable."""
    temporary = path.with_suffix('.json.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def run_step(report, *, command, cwd, env, log_path, receipt_path,
             log_reference=None, step_fields=None, exit_key='returncode') -> int:
    """Persist one step before launch and close it after logging or interruption.

    Report-wide completion belongs to the entry point. Lifecycle fields are
    authoritative; optional metadata cannot replace them or add stale results.
    """
    if exit_key not in {'returncode', 'exit_code'}:
        raise ValueError('exit_key must be returncode or exit_code')
    log_path, receipt_path = Path(log_path), Path(receipt_path)
    if log_reference is None:
        log_reference = Path(os.path.relpath(log_path, receipt_path.parent)).as_posix()
    reserved = {'command', 'status', 'returncode', 'exit_code', 'started_at',
                'finished_at', 'seconds', 'log', 'error'}
    step = {key: value for key, value in (step_fields or {}).items() if key not in reserved}
    started = time.monotonic()
    step.update(command=command, status='running', started_at=datetime.now(timezone.utc).isoformat(),
                log=log_reference, **{exit_key: None})
    report['steps'].append(step)
    write_report(receipt_path, report)
    result, status = 127, 'failed'
    try:
        result = run_logged(command, cwd=cwd, env=env, log_path=log_path)
        status = 'passed' if result == 0 else 'failed'
    except KeyboardInterrupt:
        result, status = 130, 'interrupted'
    except OSError as exc:
        step['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        print('Cannot start verification command; details are in the private receipt.', file=sys.stderr)
    finally:
        step.update(status=status, finished_at=datetime.now(timezone.utc).isoformat(),
                    seconds=time.monotonic() - started, **{exit_key: result})
        write_report(receipt_path, report)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--quick', action='store_true', help='locked sync, dependency/record checks and Ruff (commit hook)')
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
        [str(python), str(root / 'scripts/project_state.py'), 'check'],
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
    output.mkdir(parents=True, mode=0o700)
    report = {'mode': 'integration' if args.integration else 'quick' if args.quick else 'standard',
              'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'steps': [], 'returncode': None, 'passed': False}
    receipt_path = output / 'checks.json'
    write_report(receipt_path, report)
    result = 0
    for index, command in enumerate(commands, start=1):
        print('+ ' + ' '.join(command), flush=True)
        result = run_step(report, command=command, cwd=root, env=env,
                          log_path=output / f'{index:02d}.log', receipt_path=receipt_path)
        if result:
            break
    report['passed'] = result == 0
    report['returncode'] = result
    report['status'] = 'passed' if result == 0 else report['steps'][-1]['status']
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    write_report(receipt_path, report)
    print(f'Verification {"PASS" if result == 0 else "FAIL"}: {receipt_path}')
    return result if result >= 0 else 128 - result


if __name__ == '__main__':
    sys.exit(main())
