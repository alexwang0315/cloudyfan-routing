"""Read ALL durable intents, including failed/incomplete workflows, without HTTP."""
from decimal import Decimal
import json

from .client import usage


def summarize(client):
    """Amounts with unknown evidence are never represented as a known grand total.

    Use this across the entire client root, not a sum of completed run results.
    Returned rows omit prompts, answers, credentials and full response bodies.
    """
    rows, seen = [], set()
    known, unknown = Decimal(0), 0
    with client._lock():
        for directory in sorted((client.root / 'calls').iterdir()):
            if not directory.is_dir():
                continue
            row = {'call_id': directory.name, 'cost_usd': None, 'generation_id': None,
                   'cost_unknown': True, 'evidence_status': 'incomplete'}
            try:
                intent = json.loads((directory / 'intent.json').read_text())
                record = client._load(directory)
                info = usage(record)
                row.update({k: info[k] for k in ('generation_id', 'model', 'provider', 'cost_usd', 'cost_source')})
                row['endpoint'] = intent['url']
                duplicate = info['generation_id'] in seen or record.get('duplicate_generation_id')
                row['cost_unknown'] = info['cost_unknown'] or not info['generation_id'] or bool(duplicate)
                row['evidence_status'] = 'duplicate_identity' if duplicate else ('unknown' if row['cost_unknown'] else 'cost_recorded')
                # A flagged later duplicate must not consume the original receipt
                # when its call ID sorts before the original call ID.
                if info['generation_id'] and not record.get('duplicate_generation_id'):
                    seen.add(info['generation_id'])
                if info['cost_usd'] is not None and not duplicate:
                    known += Decimal(info['cost_usd'])
            except (OSError, ValueError, TypeError, KeyError):
                pass
            unknown += bool(row['cost_unknown'])
            rows.append(row)
    return {'calls': len(rows), 'known_unique_cost_usd': str(known), 'unknown_calls': unknown,
            'total_api_usd': None if unknown else str(known), 'rows': rows,
            'host_and_human_usd': None}
