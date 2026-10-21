#!/usr/bin/env python3
"""Freeze or verify a metadata-only low-pool plan; no credential or HTTP client."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cloudyfan_jev.scores.sql_low_preparation import prepare_low_trial, verify_low_trial  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--now', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--output', type=Path)
    action.add_argument('--verify', type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        inputs = json.loads(args.input.read_text())
        if args.verify:
            result = verify_low_trial(json.loads(args.verify.read_text()), inputs, root=root, now=args.now)
        else:
            plan = prepare_low_trial(inputs, now=args.now, root=root)
            output = args.output.resolve()
            if not output.is_relative_to(root / 'runtime'):
                raise ValueError('Use a new output file inside ignored runtime')
            # Complete serialization before creating a new, never-overwritten artifact.
            text = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
            with output.open('x') as stream:
                output.chmod(0o600)
                stream.write(text)
            result = {'plan_id': plan['plan_id'], 'dispatch_authorized': False,
                      'formal_qualification': 'pending', 'model_api_calls': 0}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
