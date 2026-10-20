"""Grade saved, normalized model records offline; never send requests or approve a pool."""
import argparse
import json
from pathlib import Path

from cloudyfan_jev.scores.sql_capability import grade_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--role', choices=['open_source', 'low', 'mid', 'high'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--now', required=True)
    parser.add_argument('--synthetic', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.output.resolve().is_relative_to(root / 'runtime'):
        raise ValueError('Keep assessment results in ignored runtime')
    table = json.loads((root / 'examples/sql-model-versions-v1.json').read_text())
    entry = next(c['entry'] for c in table['configurations'] if c['role'] == args.role)
    report = grade_model(entry, json.loads(args.records.read_text()), now=args.now, synthetic=args.synthetic)
    with args.output.open('x') as f:
        args.output.chmod(0o600)
        json.dump(report, f, indent=2)
        f.write('\n')
    print(json.dumps({'qualified': report['qualified'], 'synthetic': report['synthetic'],
                      'known_api_usd': report['known_api_usd'], 'unknown_cost_count': report['unknown_cost_count']}))
    return 0 if report['qualified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
