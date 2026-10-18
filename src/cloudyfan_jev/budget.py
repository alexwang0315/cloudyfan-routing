"""Durable admission and exact accounting for a controlled application entry point.

This is not a provider spending-cap implementation. Real applications must supply
and verify enforceable provider/tool bounds; unknown bounds fail closed.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import os
import sqlite3


class BudgetError(RuntimeError):
    control_plane_error = True


def money(value):
    if not isinstance(value, (str, Decimal)):
        raise ValueError('Money must be a decimal string or Decimal, never a float')
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError('Money must be finite and nonnegative')
    return result


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return result


@dataclass(frozen=True)
class Authorization:
    reference: str
    total_usd: Decimal
    expires_at: str


class BudgetStore:
    """One private SQLite file per shared spending authorization/account.

    A reservation includes consumed calls until the workflow completes. Events
    remain historical after release, so a new reservation cannot reuse spent USD.
    """
    def __init__(self, path, *, clock=lambda: datetime.now(timezone.utc)):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS authorization (singleton INTEGER PRIMARY KEY, reference TEXT, total TEXT, expires TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS reservations (run TEXT PRIMARY KEY, fingerprint TEXT, amount TEXT, status TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, run TEXT, kind TEXT, bound TEXT, fingerprint TEXT, cost TEXT, verified INTEGER, receipt TEXT UNIQUE)')
            db.execute('CREATE TABLE IF NOT EXISTS violations (event_id TEXT PRIMARY KEY, reason TEXT, observed_cost TEXT, receipt TEXT, verified INTEGER, prior_cost TEXT)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def authorize(self, authorization):
        total = money(authorization.total_usd)
        if not authorization.reference or timestamp(authorization.expires_at) <= self.clock():
            raise BudgetError('Valid unexpired authorization required')
        with self._transaction() as db:
            old = db.execute('SELECT * FROM authorization').fetchone()
            record = (authorization.reference, str(total), authorization.expires_at)
            if old and tuple(old)[1:] != record:
                raise BudgetError('Existing authorization is immutable; do not reset cumulative spending')
            db.execute('INSERT OR IGNORE INTO authorization VALUES (1,?,?,?)', record)

    def _authorization(self, db):
        auth = db.execute('SELECT * FROM authorization').fetchone()
        if auth is None or timestamp(auth['expires']) <= self.clock():
            raise BudgetError('Missing or expired authorization')
        return auth

    def reserve(self, run_id, fingerprint, amount):
        amount = money(amount)
        with self._transaction() as db:
            auth = self._authorization(db)
            self._dispatchable(db, auth)
            prior = db.execute('SELECT * FROM reservations WHERE run=?', (run_id,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint or money(prior['amount']) != amount:
                    raise BudgetError('Frozen reservation changed')
                return
            reserved = sum((money(r[0]) for r in db.execute('SELECT amount FROM reservations WHERE status="active"')), Decimal(0))
            spent = self._settled_cost(db) + self._provisional_hold(db)
            if reserved + spent + amount > money(auth['total']):
                raise BudgetError('Insufficient shared balance for complete workflow')
            db.execute('INSERT INTO reservations VALUES (?,?,?,"active")', (run_id, fingerprint, str(amount)))

    def _no_unknown(self, db):
        if db.execute('SELECT 1 FROM violations LIMIT 1').fetchone():
            raise BudgetError('Unresolved cap violation blocks shared dispatch')
        if db.execute('SELECT 1 FROM events WHERE cost IS NULL LIMIT 1').fetchone():
            raise BudgetError('Historical or pending unknown cost blocks new dispatch')
        if self._unbounded_history(db):
            raise BudgetError('Unverified historical cost has no bounded exposure; balance unavailable')

    def _unbounded_history(self, db):
        return db.execute('SELECT COUNT(*) FROM events WHERE run="__history__" AND verified=0').fetchone()[0]

    def _provisional_hold(self, db):
        # Active workflow reservations already cover their call bounds. Completed
        # calls retain only the difference between the bound and provisional fee.
        return sum((max(Decimal(0), money(r['bound']) - money(r['cost'])) for r in db.execute(
            'SELECT e.bound,e.cost FROM events e LEFT JOIN reservations r ON e.run=r.run '
            'WHERE e.cost IS NOT NULL AND e.verified=0 AND e.run!="__history__" '
            'AND (r.status="complete" OR r.run IS NULL)')), Decimal(0))

    def _dispatchable(self, db, auth):
        self._no_unknown(db)
        reserved = sum((money(r[0]) for r in db.execute(
            'SELECT amount FROM reservations WHERE status="active"')), Decimal(0))
        if reserved + self._settled_cost(db) + self._provisional_hold(db) > money(auth['total']):
            raise BudgetError('Shared exposure exceeds authorized balance; dispatch stopped')

    def _settled_cost(self, db):
        return sum((money(r[0]) for r in db.execute(
            'SELECT e.cost FROM events e LEFT JOIN reservations r ON e.run=r.run '
            'WHERE e.cost IS NOT NULL AND (r.status="complete" OR r.run IS NULL)')), Decimal(0))

    def begin_event(self, run_id, event_id, kind, bound, fingerprint):
        bound = money(bound)
        if kind not in {'model', 'tool'} or not event_id or not fingerprint:
            raise ValueError('Explicit model/tool event identity required')
        with self._transaction() as db:
            auth = self._authorization(db)
            prior = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if prior:
                if (prior['run'], prior['kind'], money(prior['bound']), prior['fingerprint']) != (run_id, kind, bound, fingerprint):
                    raise BudgetError('Event identity changed')
                if prior['cost'] is None:
                    raise BudgetError('Existing unknown event cannot be dispatched again')
                return False
            self._dispatchable(db, auth)
            reservation = db.execute('SELECT * FROM reservations WHERE run=?', (run_id,)).fetchone()
            if not reservation or reservation['status'] != 'active':
                raise BudgetError('Active reservation required before dispatch')
            committed = sum((money(r[0]) for r in db.execute('SELECT bound FROM events WHERE run=?', (run_id,))), Decimal(0))
            if committed + bound > money(reservation['amount']):
                raise BudgetError('Dispatch exceeds complete workflow reservation')
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,NULL,0,NULL)',
                       (event_id, run_id, kind, str(bound), fingerprint))
            return True

    def record_event(self, event_id, *, cost_usd, verified=False, receipt_id=None):
        cost = None if cost_usd is None else str(money(cost_usd))
        breached = False
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if prior is None:
                raise BudgetError('Receipt requires a durable prior event')
            if db.execute('SELECT 1 FROM violations WHERE event_id=?', (event_id,)).fetchone():
                raise BudgetError('Unresolved cap violation requires explicit account review')
            if prior['verified'] and (cost != prior['cost'] or receipt_id != prior['receipt']):
                raise BudgetError('Verified receipt is immutable')
            if prior['receipt'] and receipt_id != prior['receipt']:
                raise BudgetError('Reconciliation identity changed')
            if cost is not None and not receipt_id:
                raise BudgetError('Known cost requires unique receipt identity')
            if cost is not None and prior['run'] != '__history__' and money(cost) > money(prior['bound']):
                # Commit the account-wide stop BEFORE raising. Preserve both the
                # observed charge and previous provisional fee as audit evidence.
                db.execute('INSERT INTO violations VALUES (?,?,?,?,?,?)',
                           (event_id, 'enforced_cap_breached', cost, receipt_id, int(verified), prior['cost']))
                db.execute('UPDATE events SET cost=NULL,verified=0 WHERE id=?', (event_id,))
                breached = True
            else:
                try:
                    db.execute('UPDATE events SET cost=?, verified=?, receipt=? WHERE id=?',
                               (cost, int(verified or bool(prior['verified'])), receipt_id, event_id))
                except sqlite3.IntegrityError:
                    raise BudgetError('Duplicate receipt identity; event remains unknown') from None
        if breached:
            raise BudgetError('Enforced cap violated; durable shared stop retained')

    def finish(self, run_id):
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM events WHERE run=? AND cost IS NULL', (run_id,)).fetchone():
                raise BudgetError('Cannot release unknown cost reservation')
            db.execute('UPDATE reservations SET status="complete" WHERE run=?', (run_id,))

    def import_history(self, event_id, *, cost_usd, receipt_id=None, verified=False):
        """Import previous spending/unknowns before admitting new runs, idempotently."""
        cost = None if cost_usd is None else str(money(cost_usd))
        if cost is not None and not receipt_id:
            raise BudgetError('Historical known cost needs receipt identity')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if row is None:
                db.execute('INSERT INTO events VALUES (?,"__history__","model",?,"history",NULL,0,NULL)',
                           (event_id, cost or '0'))
        # Existing managed events are refreshed as well, never counted as history twice.
        self.record_event(event_id, cost_usd=cost, receipt_id=receipt_id, verified=verified)

    def report(self):
        with self._transaction() as db:
            auth = db.execute('SELECT * FROM authorization').fetchone()
            reservations = [dict(r) for r in db.execute('SELECT * FROM reservations ORDER BY run')]
            events = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
            violations = [dict(r) for r in db.execute('SELECT * FROM violations ORDER BY event_id')]
            reserved = sum((money(r['amount']) for r in reservations if r['status'] == 'active'), Decimal(0))
            known = sum((money(r['cost']) for r in events if r['cost'] is not None), Decimal(0))
            verified = sum((money(r['cost']) for r in events if r['cost'] is not None and r['verified']), Decimal(0))
            unknown = sum(r['cost'] is None for r in events)
            provisional = self._provisional_hold(db)
            unbounded = self._unbounded_history(db)
            available = (money(auth['total']) - reserved - self._settled_cost(db) - provisional
                         if auth and not unknown and not violations and not unbounded else None)
            return {'authorization': dict(auth) if auth else None,
                    'authorized_usd': auth['total'] if auth else None,
                    'reserved_usd': str(reserved), 'known_usd': str(known), 'verified_usd': str(verified),
                    'unknown_events': unknown, 'total_usd': None if unknown else str(known),
                    'available_usd': str(available) if available is not None else None,
                    'provisional_hold_usd': str(provisional), 'unbounded_history_events': unbounded,
                    'dispatch_blocked': bool(violations or unknown or unbounded or not auth
                                             or (available is not None and available < 0)),
                    'violations': violations,
                    'events': events, 'reservations': reservations}


@dataclass(frozen=True)
class EnforcedBound:
    """Upper bound backed by an independently verified enforcement mechanism.

    Evidence is an audit reference, NOT proof by itself. verify_bound must inspect
    effective enforcement for the exact provider/model or tool at admission and
    each dispatch. There is deliberately no built-in 'OpenRouter token cap'.
    """
    usd: str
    evidence: str

    def __post_init__(self):
        money(self.usd)
        if not self.evidence.strip():
            raise ValueError('Enforcement evidence reference required')


@dataclass(frozen=True)
class ToolBound:
    bound: EnforcedBound
    external: bool = False


@dataclass(frozen=True)
class ToolReceipt:
    result: dict
    cost_usd: str | None
    receipt_id: str | None
    verified: bool = False


def _fingerprint(value):
    import hashlib
    from .client import _encoded
    return hashlib.sha256(_encoded(value)).hexdigest()


class _BudgetClient:
    def __init__(self, wrapped, owner, run_id):
        self.wrapped, self.owner, self.run_id = wrapped, owner, run_id

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def _event_id(self, cid):
        return 'model:' + _fingerprint(str(self.wrapped.root.resolve())) + ':' + cid

    def _record(self, cid, record):
        from .client import usage
        info = usage(record)
        receipt = info.get('local_call_id') or info.get('generation_id')
        if info.get('local_call_id'):
            receipt = 'local:' + _fingerprint(str(self.wrapped.root.resolve())) + ':' + receipt
        if record.get('duplicate_generation_id'):
            receipt = None
        self.owner.store.record_event(self._event_id(cid),
            cost_usd=info['cost_usd'] if receipt else None,
            verified=info['cost_source'] == 'generation_total_cost', receipt_id=receipt)
        return record

    def call(self, cid, *args):
        payload = args[-1]
        model = payload.get('model')
        bound = self.owner.model_bounds.get(model)
        if bound is None:
            raise BudgetError('No confirmed model upper bound')
        self.owner._verify('model', model, payload, bound)
        self.owner.store.begin_event(self.run_id, self._event_id(cid), 'model', money(bound.usd),
                                     _fingerprint(args))
        return self._record(cid, self.wrapped.call(cid, *args))

    def reconcile(self, cid):
        return self._record(cid, self.wrapped.reconcile(cid))


class _BudgetEnvironment:
    def __init__(self, environment, owner, run_id):
        self.environment, self.owner, self.run_id = environment, owner, run_id
        self.index = 0
        self.replayable_tools = frozenset(name for name in getattr(environment, 'replayable_tools', ())
                                         if name in owner.tool_bounds and not owner.tool_bounds[name].external)

    def __getattr__(self, name):
        return getattr(self.environment, name)

    def tools_schema(self):
        tools = self.environment.tools_schema()
        for tool in tools:
            name = tool.get('function', {}).get('name')
            if name not in self.owner.tool_bounds:
                raise BudgetError('Tool lacks explicit cost and execution policy')
        return tools

    def call(self, name, arguments):
        from .client import _encoded
        policy = self.owner.tool_bounds.get(name)
        if policy is None:
            raise BudgetError('Tool lacks confirmed upper bound')
        self.owner._verify('tool', name, arguments, policy.bound)
        event_id = f'tool:{self.run_id}:{self.index}'
        self.index += 1
        fresh = self.owner.store.begin_event(self.run_id, event_id, 'tool', money(policy.bound.usd),
                                             _fingerprint({'name': name, 'arguments': arguments}))
        if not fresh and policy.external:
            raise BudgetError('External tool replay is forbidden')
        result = self.environment.call(name, arguments)
        if policy.external:
            if not isinstance(result, ToolReceipt):
                raise BudgetError('External tool omitted billing receipt; cost remains unknown')
            _encoded(result.result)  # reject unsafe/nonserializable results before release
            self.owner.store.record_event(event_id, cost_usd=result.cost_usd,
                verified=result.verified, receipt_id=result.receipt_id)
            if result.cost_usd is None:
                raise BudgetError('External tool cost unknown; execution stopped')
            return result.result
        if money(policy.bound.usd) != 0:
            raise BudgetError('Local replayable tools require confirmed zero API fee')
        self.owner.store.record_event(event_id, cost_usd='0', verified=False, receipt_id=event_id)
        return result


class BudgetedCore:
    """Controlled application seam composing Core.run and shared durable budget.

    verify_bound(kind, identity, payload, EnforcedBound) returns exactly True only
    when a hard bound is effective. At preflight payload is a frozen profile (or
    tool policy); at dispatch it is the actual request. Returning estimates or
    truthy objects fails closed. No production provider verifier is included.
    """
    def __init__(self, core, store, *, model_bounds, tool_bounds, verify_bound=None):
        self.core, self.store = core, store
        self.model_bounds, self.tool_bounds = dict(model_bounds), dict(tool_bounds)
        self.verify_bound = verify_bound

    def __getattr__(self, name):
        return getattr(self.core, name)

    def _verify(self, kind, identity, payload, bound):
        if self.verify_bound is None or self.verify_bound(kind, identity, payload, bound) is not True:
            raise BudgetError('Enforceable upper bound unconfirmed; estimates are not caps')

    def _admit(self, mode):
        from .engine import INITIAL
        if mode not in INITIAL:
            raise ValueError('Unknown workflow mode')
        first = self.core.profiles[INITIAL[mode]]
        remaining = [self.core.profiles[t] for t in ('low', 'mid', 'high')] if mode == 'cloudyfan' else [self.core.profiles['high']]
        def model_cap(profile, key):
            model = profile[key]
            bound = self.model_bounds.get(model)
            if bound is None:
                raise BudgetError('No confirmed model upper bound')
            self._verify('model', model, profile, bound)
            return money(bound.usd)
        amount = self.core.limits.max_turns * (model_cap(first, 'model_version') + max(model_cap(p, 'model_version') for p in remaining))
        if mode == 'cloudyfan':
            amount += 2 * model_cap(self.core.decision, 'version')
        tool_max = Decimal(0)
        for name, policy in self.tool_bounds.items():
            self._verify('tool', name, {'external': policy.external}, policy.bound)
            if not policy.external and money(policy.bound.usd) != 0:
                raise BudgetError('Local deterministic tools need confirmed zero API fee')
            tool_max = max(tool_max, money(policy.bound.usd))
        # max_tool_calls is a per-turn bound, not a per-workflow bound.
        amount += 2 * self.core.limits.max_turns * self.core.limits.max_tool_calls * tool_max
        return amount

    def sync_client(self, client):
        """Read existing client receipts, including incomplete intents, without HTTP."""
        from .client import usage
        with client._lock():
            for directory in sorted((client.root / 'calls').iterdir()):
                if not directory.is_dir():
                    continue
                cost, receipt, verified = None, None, False
                try:
                    record = client._load(directory)
                    info = usage(record)
                    receipt = info.get('local_call_id') or info.get('generation_id')
                    if info.get('local_call_id'):
                        receipt = 'local:' + _fingerprint(str(client.root.resolve())) + ':' + receipt
                    if not record.get('duplicate_generation_id') and (directory / 'intent.json').exists():
                        cost = info['cost_usd'] if receipt else None
                        verified = info['cost_source'] == 'generation_total_cost'
                    else:
                        receipt = None
                except (OSError, ValueError, TypeError):
                    pass
                event_id = 'model:' + _fingerprint(str(client.root.resolve())) + ':' + directory.name
                self.store.import_history(event_id, cost_usd=cost, receipt_id=receipt, verified=verified)

    def run(self, run_id, task, environment_factory, *, contract_version, mode='cloudyfan', **kwargs):
        from copy import copy
        from dataclasses import asdict
        frozen = {**self.core.configuration_descriptor(),
                  'root': str(self.core.client.root.resolve()), 'task': task, 'contract': contract_version,
                  'mode': mode, 'options': kwargs,
                  'model_bounds': {k: asdict(v) for k, v in self.model_bounds.items()},
                  'tool_bounds': {k: asdict(v) for k, v in self.tool_bounds.items()}}
        self.sync_client(self.core.client)
        if getattr(self.core, 'local_backend', None) is not None:
            self.sync_client(self.core.local_backend)
        amount = self._admit(mode)
        self.store.reserve(run_id, _fingerprint(frozen), amount)
        engine = copy(self.core)
        engine.client = _BudgetClient(self.core.client, self, run_id)
        if getattr(self.core, 'local_backend', None) is not None:
            engine.local_backend = _BudgetClient(self.core.local_backend, self, run_id)
        result = engine.run(run_id, task,
            lambda: _BudgetEnvironment(environment_factory(), self, run_id),
            contract_version=contract_version, mode=mode, **kwargs)
        self.store.finish(run_id)
        report = self.store.report()
        events = [r for r in report['events'] if r['run'] == run_id]
        known = sum((money(r['cost']) for r in events if r['cost'] is not None), Decimal(0))
        unknown = sum(r['cost'] is None for r in events)
        return {**result, 'budget': {'run_known_usd': str(known), 'run_unknown_events': unknown,
                                   'run_total_usd': None if unknown else str(known),
                                   'shared': report}}
