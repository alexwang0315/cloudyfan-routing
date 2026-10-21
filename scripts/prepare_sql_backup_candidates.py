#!/usr/bin/env python3
"""Prepare an offline SQL backup candidate/evidence review; never dispatch models."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cloudyfan_jev.scores.sql_backup_candidates import prepare_sql_backup_candidates  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', type=Path, required=True)
    parser.add_argument('--sql-low-proposal', type=Path, required=True)
    parser.add_argument('--evidence-index', type=Path)
    parser.add_argument('--archived-pack', type=Path)
    parser.add_argument('--now', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        coverage = json.loads(args.coverage.read_text())
        low = json.loads(args.sql_low_proposal.read_text())
        evidence = json.loads(args.evidence_index.read_text()) if args.evidence_index else None
        archive = None
        if args.archived_pack:
            raw = args.archived_pack.read_bytes()
            expected = [s['sha256'] for s in coverage.get('sources', []) if s.get('name') == 'prepared_research_v8']
            if len(expected) != 1 or hashlib.sha256(raw).hexdigest() != expected[0]:
                raise ValueError('Archived pack raw hash does not match inventory source')
            archive = json.loads(raw)
        report = prepare_sql_backup_candidates(coverage, low, now=args.now, evidence_index=evidence,
                                               archived_pack=archive)
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output:
            with args.output.open('x') as stream:
                stream.write(text)
        else:
            print(text, end='')
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
