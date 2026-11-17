"""Read ALL durable intents, including failed/incomplete workflows, without HTTP."""
from decimal import Decimal
import json

from .advisor import ROLES, role_of
from .client import not_dispatched, usage


def summarize(client, *, advisor=None):
    """Amounts with unknown evidence are never represented as a known grand total.

    Use this across the entire client root, not a sum of completed run results.
    Returned rows omit prompts, answers, credentials and full response bodies.

    提供路徑二的 ``advisor``（AdvisorConfig）時，依 call_id 約定在每列加上
    ``role``，並另列各角色金額與「明示開源角色計為零」的情境金額；
    未提供時輸出與既有版本完全相同。

    注意：``hypothetical_open_source_roles_zero_usd`` 與 ``Core._with_costs`` 的
    ``hypothetical_open_source_zero_usd`` 不是同一個指標，不可互相比較。本函式只把
    明示 ``open_source: True`` 的顧問／裁判角色計為零；池內 ``open_source`` 層級的作答
    （例如開源首答）照收據計入，因為帳本列沒有作答層級。範圍也不同：本函式涵蓋整個
    client 根目錄的全部收據，``_with_costs`` 只算單題結果引用的呼叫。
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
                local = info.get('execution_kind') == 'local'
                identity = info.get('local_call_id') if local else info['generation_id']
                if not_dispatched(record):
                    identity = 'not-dispatched:' + directory.name
                identity_key = ('local' if local else 'cloud', identity)
                if local:
                    row.update({k: info[k] for k in ('local_call_id', 'run_id', 'execution_kind',
                               'quantization', 'engine', 'execution_location', 'evidence_source',
                               'usage_unknown', 'host_usd', 'electricity_usd')})
                duplicate = identity_key in seen or record.get('duplicate_generation_id')
                row['cost_unknown'] = info['cost_unknown'] or not identity or bool(duplicate)
                row['evidence_status'] = 'duplicate_identity' if duplicate else ('unknown' if row['cost_unknown'] else 'cost_recorded')
                # A flagged later duplicate must not consume the original receipt
                # when its call ID sorts before the original call ID.
                if identity and not record.get('duplicate_generation_id'):
                    seen.add(identity_key)
                if info['cost_usd'] is not None and not duplicate:
                    known += Decimal(info['cost_usd'])
            except (OSError, ValueError, TypeError, KeyError):
                pass
            unknown += bool(row['cost_unknown'])
            rows.append(row)
    summary = {'calls': len(rows), 'known_unique_cost_usd': str(known), 'unknown_calls': unknown,
               'total_api_usd': None if unknown else str(known), 'rows': rows,
               'host_and_human_usd': None}
    if advisor is not None:
        summary.update(_roles(rows, advisor, unknown))
    return summary


def _roles(rows, advisor, unknown):
    totals = {role: {'calls': 0, 'known_usd': Decimal(0), 'unknown_calls': 0,
                     'open_source': getattr(advisor, role)['open_source']} for role in ROLES}
    scenario = Decimal(0)
    for row in rows:
        row['role'] = role = role_of(row['call_id'])
        open_source = role is not None and totals[role]['open_source'] is True
        if not row['cost_unknown'] and row['cost_usd'] is not None and not open_source:
            scenario += Decimal(row['cost_usd'])
        if role is None:
            continue
        totals[role]['calls'] += 1
        if row['cost_unknown']:
            totals[role]['unknown_calls'] += 1
        elif row['cost_usd'] is not None:
            totals[role]['known_usd'] += Decimal(row['cost_usd'])
    return {'roles': {role: {**t, 'known_usd': str(t['known_usd'])} for role, t in totals.items()},
            'hypothetical_open_source_roles_zero_usd': None if unknown else str(scenario)}
