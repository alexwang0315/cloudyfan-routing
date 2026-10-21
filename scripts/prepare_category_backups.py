#!/usr/bin/env python3
"""Offline 23-category preparation. All evidence remains explicit input files."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cloudyfan_jev.scores.backup_pools import backup_coverage_report  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix', type=Path, required=True, help='Existing hash-bound category matrix JSON')
    parser.add_argument('--requirements', type=Path, required=True, help='Category-keyed task requirements JSON')
    parser.add_argument('--evidence', type=Path, required=True, help='Category-keyed configuration evidence JSON')
    parser.add_argument('--qcs-policies', type=Path, required=True, help='Category-keyed QCS admission policies JSON')
    parser.add_argument('--now', required=True, help='Explicit timezone-aware verification time')
    parser.add_argument('--target-size', type=int, default=3, choices=(1, 2, 3))
    parser.add_argument('--output', type=Path, help='Write JSON here; default is stdout')
    args = parser.parse_args(argv)
    try:
        values = [json.loads(path.read_text()) for path in
                  (args.matrix, args.requirements, args.evidence, args.qcs_policies)]
        report = backup_coverage_report(*values, now=args.now, target_size=args.target_size)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
