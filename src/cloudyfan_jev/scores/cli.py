"""Explicit read-only ingestion and local snapshot publication; no model calls."""
import argparse
import json
import os
from pathlib import Path
import sys

import httpx

from . import adapters
from .catalog import canonical, compile_snapshot, select_profiles
from .integration import utc_now
from .snapshots import SnapshotStore


def _read(path):
    return json.loads(Path(path).read_text())


def _output(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not silently replace an observation archive or a user's profiles.
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(canonical(value) + b'\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Benchmark data only; never invokes a generation or Jev API')
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch', help='Explicit GET requests to official benchmark sources')
    fetch.add_argument('source', choices=['livebench', 'artificial-analysis', 'openrouter'])
    fetch.add_argument('--output', required=True)
    fetch.add_argument('--release')
    fetch.add_argument('--revision')
    fetch.add_argument('--metadata', help='Reviewed LiveBench JSON effort metadata; omitted means unknown effort')
    imported = commands.add_parser('import-codexradar', help='Import reviewed model benchmark JSON, not account points')
    imported.add_argument('--input', required=True)
    imported.add_argument('--output', required=True)
    build = commands.add_parser('build', help='Compile and atomically publish all three source-native pools')
    for name in ('observations', 'registry', 'policy', 'store'):
        build.add_argument('--' + name, required=True)
    select = commands.add_parser('select', help='Prepare per-task profiles without paid classification')
    for name in ('store', 'requirements', 'output'):
        select.add_argument('--' + name, required=True)
    select.add_argument('--snapshot', help='Explicit snapshot digest; default is current')
    inspect = commands.add_parser('inspect', help='Inspect integrity, expiry, pool sizes and exclusions')
    inspect.add_argument('--store', required=True)
    inspect.add_argument('--snapshot')
    args = parser.parse_args(argv)
    now = utc_now()
    try:
        if args.command == 'fetch':
            # Reject an existing archive before performing any network request.
            if Path(args.output).exists():
                raise FileExistsError('Observation archive exists')
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                if args.source == 'livebench':
                    if bool(args.release) != bool(args.revision):
                        raise ValueError('Supply both release and revision, or neither for discovery')
                    version = ({'release': args.release, 'revision': args.revision} if args.release else
                               adapters.discover_livebench(client))
                    metadata = (Path(args.metadata).read_bytes() if args.metadata else
                                canonical({**version, 'models': {}}))
                    rows = adapters.fetch_livebench(client, **version, metadata_json=metadata, retrieved_at=now)
                elif args.source == 'artificial-analysis':
                    rows = adapters.fetch_artificial_analysis(client,
                        api_key=os.environ['ARTIFICIAL_ANALYSIS_API_KEY'], retrieved_at=now)
                else:
                    rows = adapters.fetch_openrouter_benchmarks(client,
                        api_key=os.environ['OPENROUTER_API_KEY'], retrieved_at=now)
            _output(args.output, rows)
            print(json.dumps({'observations': len(rows), 'output': args.output, 'generation_calls': 0}))
        elif args.command == 'import-codexradar':
            rows = adapters.import_codexradar(Path(args.input).read_bytes(), retrieved_at=now)
            _output(args.output, rows)
            print(json.dumps({'observations': len(rows), 'channel': 'reviewed_import', 'generation_calls': 0}))
        elif args.command == 'build':
            snapshot = compile_snapshot(_read(args.observations), _read(args.registry), _read(args.policy), now=now)
            SnapshotStore(args.store).publish(snapshot, now=now)
            print(json.dumps({'snapshot_id': snapshot['snapshot_id'],
                              'pools': {k: len(v) for k, v in snapshot['pools'].items()},
                              'excluded': snapshot['excluded']}))
        else:
            snapshot = SnapshotStore(args.store).load(args.snapshot, now=now)
            if args.command == 'select':
                profiles = select_profiles(snapshot, _read(args.requirements), now=now)
                _output(args.output, {'snapshot_id': snapshot['snapshot_id'], 'profiles': profiles})
                print(json.dumps({'snapshot_id': snapshot['snapshot_id'], 'output': args.output}))
            else:
                print(json.dumps({'snapshot_id': snapshot['snapshot_id'], 'valid_until': snapshot['valid_until'],
                                  'policy': snapshot['policy'], 'pools': {k: len(v) for k, v in snapshot['pools'].items()},
                                  'excluded': snapshot['excluded']}))
    except (ValueError, TypeError, KeyError, OSError) as error:
        # No response bodies, environment contents or HTTP headers in diagnostics.
        print(f'Score operation failed ({type(error).__name__}); no generation request was made.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
