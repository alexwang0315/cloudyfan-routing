"""Explicit read-only ingestion and local snapshot publication; no model calls."""
import argparse
import json
import os
from pathlib import Path
import sys

import httpx

from . import adapters
from .catalog import canonical, select_profiles
from .integration import utc_now
from .snapshots import SnapshotStore
from .qualification import build_qualification_pack, qualified_snapshot
from .updates import ScoreUpdater, preview_update


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
    build.add_argument('--review', help='Reviewed input-bound qualification attestations; required for publication')
    build.add_argument('--synthetic', action='store_true', help='Explicit offline fixture store, never production')
    qualify = commands.add_parser('qualify', help='Offline qualification review; never publishes preview')
    for name in ('observations', 'registry', 'policy', 'review', 'output'):
        qualify.add_argument('--' + name, required=True)
    qualify.add_argument('--synthetic', action='store_true')
    select = commands.add_parser('select', help='Prepare per-task profiles without paid classification')
    for name in ('store', 'requirements', 'output'):
        select.add_argument('--' + name, required=True)
    select.add_argument('--snapshot', help='Explicit snapshot digest; default is current')
    inspect = commands.add_parser('inspect', help='Inspect integrity, expiry, pool sizes and exclusions')
    inspect.add_argument('--store', required=True)
    inspect.add_argument('--snapshot')
    for name in ('update-preview', 'update'):
        command = commands.add_parser(name, help='Review and apply an input-bound offline update')
        command.add_argument('--previous', help='Original qualification pack; omit only for initial publication')
        command.add_argument('--proposed', required=True)
        command.add_argument('--synthetic', action='store_true')
        if name == 'update-preview':
            command.add_argument('--output', required=True)
        else:
            command.add_argument('--store', required=True)
            command.add_argument('--operation', required=True)
    audit = commands.add_parser('update-inspect', help='Read the durable update operation result')
    audit.add_argument('--store', required=True)
    audit.add_argument('--operation', required=True)
    args = parser.parse_args(argv)
    now = utc_now()
    try:
        if args.command in ('update-preview', 'update'):
            previous = _read(args.previous) if args.previous else None
            proposed = _read(args.proposed)
            if args.command == 'update-preview':
                report = preview_update(previous, proposed, now=now, allow_synthetic=args.synthetic)
                _output(args.output, report)
            else:
                report = ScoreUpdater(SnapshotStore(args.store)).apply(
                    args.operation, previous, proposed, now=now, allow_synthetic=args.synthetic)
            print(json.dumps(report))
            if args.command == 'update' and report['status'] != 'published':
                return 1
        elif args.command == 'update-inspect':
            print(json.dumps(ScoreUpdater(SnapshotStore(args.store)).inspect(args.operation)))
        elif args.command == 'fetch':
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
        elif args.command == 'qualify':
            pack = build_qualification_pack(_read(args.observations), _read(args.registry), _read(args.policy),
                                             review=_read(args.review), now=now, synthetic=args.synthetic)
            _output(args.output, pack)
            print(json.dumps({k: pack[k] for k in ('pack_id', 'status', 'production_ready', 'blockers')}))
        elif args.command == 'build':
            if not args.review:
                raise ValueError('Qualification review required before publication')
            pack = build_qualification_pack(_read(args.observations), _read(args.registry), _read(args.policy),
                                             review=_read(args.review), now=now, synthetic=args.synthetic)
            snapshot = qualified_snapshot(pack, now=now, allow_synthetic=args.synthetic)
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
