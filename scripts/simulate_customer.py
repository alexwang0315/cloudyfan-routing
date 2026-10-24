#!/usr/bin/env python3
"""Ask fifty questions through the official Ollama SDK using a loopback service.

Pass only {"client_token": "..."} on stdin; credentials never appear in argv or
stdout. Same --batch-id replays the original tasks without new model dispatch.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cloudyfan_monitor.customer_simulation import local_origin, question_requests, run_customer50  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", "--host", dest="origin", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=.15,
                        help="Observation gap between questions, not model latency")
    args = parser.parse_args(argv)
    output = args.output.absolute()
    try:
        local_origin(args.origin)
        question_requests(args.batch_id)
        if not 0 <= args.delay <= 1:
            raise ValueError
        incoming = sys.stdin.readline(8193)
        credential = json.loads(incoming)
        if (len(incoming) > 8192 or not isinstance(credential, dict)
                or set(credential) != {"client_token"} or not isinstance(credential["client_token"], str)
                or len(credential["client_token"]) < 24):
            raise ValueError
        if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
            raise ValueError
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except (ValueError, OSError, TypeError):
        print(json.dumps({"status": "failed", "error": "simulation_input_or_output_invalid"}))
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            report = run_customer50(args.origin, credential["client_token"], args.batch_id, delay=args.delay)
        except Exception:
            # The server retains original task and mock receipt evidence. Keep
            # this failure too, without echoing credentials or exception text.
            report = {"schema_version": 1, "status": "failed", "batch_id": args.batch_id,
                      "error": "simulation_customer_run_failed"}
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    summary = {key: report[key] for key in ("status", "batch_id", "completed", "total", "outcomes", "error")
               if key in report}
    print(json.dumps({**summary, "output": str(output)}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
