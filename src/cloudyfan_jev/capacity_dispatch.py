"""Explicit host-only immediate admission. Capacity is never a fee estimate.

The transport calls prepare only after checking immutable replay receipts. A sent
marker precedes the same prepared HTTP request; completion follows its durable
response. Uncertain slots are never reclaimed by time, PID or a policy toggle.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import re

from .capacity import CapacityStore, _identity, _time, _live_capacity

_INGRESS = ContextVar('capacity_authenticated_ingress', default=None)
SERIALIZER = 'httpx-prepared-json-v1'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class CapacityDenied(RuntimeError):
    control_plane_error = True

    def __init__(self, code, status=429):
        if not re.fullmatch(r'capacity_[a-z_]{1,64}', code):
            raise ValueError('Safe capacity reason required')
        self.code, self.status = code, status
        super().__init__(code)


def prepare_request(http, endpoint, payload, *, headers=None):
    """Supported one-send httpx protocol. Custom hooks/auth/retries fail closed."""
    import httpx
    if (not isinstance(http, httpx.Client) or http.auth is not None
            or any(http.event_hooks.values()) or http.follow_redirects
            or any(transport is not None for transport in http._mounts.values())):
        raise CapacityDenied('capacity_transport_protocol_unsupported')
    transport = http._transport
    if type(transport) is not httpx.MockTransport and not (
            type(transport) is httpx.HTTPTransport and getattr(transport._pool, '_retries', None) == 0):
        raise CapacityDenied('capacity_transport_protocol_unsupported')
    request = http.build_request('POST', endpoint, json=payload, headers=headers, timeout=180)
    if request.method != 'POST' or str(request.url) != endpoint:
        raise CapacityDenied('capacity_transport_protocol_unsupported')
    return request


@dataclass(frozen=True)
class TokenUpperBound:
    """Trusted adapter result for these exact bytes and frozen route, not an estimate."""
    tokens: int
    wire_sha256: str
    scope_sha256: str
    version: str
    evidence_ref: str
    valid_until: float


@dataclass(frozen=True)
class CapacityRoute:
    transport: object = field(repr=False, compare=False)
    endpoint: str
    model: str
    binding: str
    token_adapter: object = field(default=None, repr=False, compare=False)
    require_supplier: tuple = ()

    def descriptor(self):
        # Hashes bind controlled runtime identity without persisting secret values.
        credential = getattr(self.transport, 'key', None)
        config = getattr(self.transport, 'configuration', None)
        return {'transport_sha256': _hash({'root': str(self.transport.root.absolute()),
                    'credential': credential, 'configuration': config}),
                'endpoint': self.endpoint, 'model': self.model, 'binding': _identity(self.binding),
                'token_adapter_version': getattr(self.token_adapter, 'version', None),
                'require_supplier': list(self.require_supplier)}


class CapacityController:
    """A registered host authority. Neither request JSON nor browser registers policy."""
    def __init__(self, store, *, credential_active):
        if type(store) is not CapacityStore or not callable(credential_active):
            raise ValueError('Controlled capacity store and credential verifier required')
        self.store, self.credential_active = store, credential_active
        self.routes = {}
        with store.connection(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS capacity_policies(version TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_dispatch_control(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,version TEXT);
                INSERT OR IGNORE INTO capacity_dispatch_control VALUES(1,0,NULL);
                CREATE TABLE IF NOT EXISTS capacity_adoptions(run TEXT PRIMARY KEY,payload TEXT NOT NULL,terminal INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_allocations(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_dispatch_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_dispatch_metadata(id INTEGER PRIMARY KEY CHECK(id=1),clock_floor REAL NOT NULL);
            ''')

    def _now(self, db, *, write=False):
        now = _time(self.store.clock())
        floor = db.execute('SELECT clock_floor FROM capacity_dispatch_metadata WHERE id=1').fetchone()
        if floor and now < floor[0]:
            raise CapacityDenied('capacity_clock_backwards', 409)
        if write:
            db.execute('INSERT OR REPLACE INTO capacity_dispatch_metadata VALUES(1,?)', (now,))
        return now

    @contextmanager
    def request(self):
        token = _INGRESS.set(None)
        try:
            yield
        finally:
            _INGRESS.reset(token)

    def authenticated(self, principal, token, scope):
        match = re.fullmatch(r'cf1_([0-9a-f]{32})\.[A-Za-z0-9_-]+', token)
        if match is None:
            raise CapacityDenied('capacity_credential_unavailable', 403)
        value = {'principal': principal, 'credential': match[1], 'scope': scope}
        now = _time(self.store.clock())
        if self.credential_active(principal, match[1], scope, now) is not True:
            raise CapacityDenied('capacity_credential_inactive', 403)
        _INGRESS.set((self, value))

    def register(self, version, *, configuration_version, routes, max_seconds):
        _identity(version)
        _identity(configuration_version)
        if type(max_seconds) is not int or not 1 <= max_seconds <= 86400 or not routes or len(routes) > 512:
            raise ValueError('Bounded registered dispatch policy required')
        config = self.store.snapshot(configuration_version)
        if not config['configured'] or config['configuration_version'] != configuration_version:
            raise ValueError('Registered capacity configuration required')
        bindings = {binding['id'] for binding in config['bindings']}
        descriptors = []
        for route in routes:
            if type(route) is not CapacityRoute or route.binding not in bindings:
                raise ValueError('Exact controlled dispatch route required')
            descriptor = route.descriptor()
            if (not isinstance(route.model, str) or not route.model
                    or any(d not in {'rpm', 'tpm', 'concurrency'} for d in route.require_supplier)
                    or route.token_adapter is not None and not callable(route.token_adapter)):
                raise ValueError('Invalid registered dispatch route')
            if descriptor in descriptors:
                raise ValueError('Duplicate capacity route')
            if route.token_adapter is not None:
                _identity(descriptor['token_adapter_version'])
            descriptors.append(descriptor)
        value = {'version': version, 'configuration_version': configuration_version,
                 'configuration_sha256': config['configuration_sha256'], 'max_seconds': max_seconds,
                 'protocol': SERIALIZER, 'routes': descriptors}
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT payload FROM capacity_policies WHERE version=?', (version,)).fetchone()
            if prior and prior[0] != _json(value):
                raise ValueError('An immutable capacity policy cannot change')
            db.execute('INSERT OR IGNORE INTO capacity_policies VALUES(?,?)', (version, _json(value)))
        self.routes[version] = tuple(routes)

    def activate(self, revision, version):
        if type(revision) is not int or revision < 0:
            raise CapacityDenied('capacity_revision_conflict', 409)
        if version is not None:
            _identity(version)
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._now(db, write=True)
            current = db.execute('SELECT revision,version FROM capacity_dispatch_control WHERE id=1').fetchone()
            if current[0] != revision:
                raise CapacityDenied('capacity_revision_conflict', 409)
            if version is not None and version not in self.routes:
                raise CapacityDenied('capacity_policy_not_registered', 409)
            if current[1] != version and _live_capacity(db, now):
                raise CapacityDenied('capacity_live_state_blocks_change', 409)
            db.execute('UPDATE capacity_dispatch_control SET revision=?,version=? WHERE id=1', (revision + 1, version))
            self._event(db, {'kind': 'activation', 'version': version, 'at': now})
        return self.snapshot()

    @staticmethod
    def _event(db, event):
        db.execute('INSERT INTO capacity_dispatch_events(payload) VALUES(?)', (_json(event),))

    def adopt(self, run, principal, descriptor):
        ingress = _INGRESS.get()
        if ingress is None or ingress[0] is not self or ingress[1]['principal'] != principal:
            raise CapacityDenied('capacity_authenticated_ingress_required', 403)
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._now(db, write=True)
            prior = db.execute('SELECT payload FROM capacity_adoptions WHERE run=?', (run,)).fetchone()
            if prior:
                value = json.loads(prior[0])
                if value['principal'] != principal or value['descriptor_sha256'] != _hash(descriptor):
                    raise CapacityDenied('capacity_frozen_adoption_changed', 409)
                return value
            version = db.execute('SELECT version FROM capacity_dispatch_control WHERE id=1').fetchone()[0]
            policy = json.loads(db.execute('SELECT payload FROM capacity_policies WHERE version=?', (version,)).fetchone()[0]) if version else None
            value = {**ingress[1], 'run': run, 'policy_version': version,
                     'configuration_version': policy['configuration_version'] if policy else None,
                     'protocol': SERIALIZER, 'descriptor_sha256': _hash(descriptor), 'accepted_at': now,
                     'deadline': now + policy['max_seconds'] if policy else None}
            db.execute('INSERT INTO capacity_adoptions VALUES(?,?,0)', (run, _json(value)))
            return value

    def context(self, run, principal, descriptor):
        with self.store.connection() as db:
            row = db.execute('SELECT payload FROM capacity_adoptions WHERE run=?', (run,)).fetchone()
        if row is None:
            raise CapacityDenied('capacity_adoption_missing', 403)
        value = json.loads(row[0])
        if value['principal'] != principal or value['descriptor_sha256'] != _hash(descriptor):
            raise CapacityDenied('capacity_frozen_adoption_changed', 409)
        return value

    def verify(self, adoption):
        with self.store.connection() as db:
            row = db.execute('SELECT payload,terminal FROM capacity_adoptions WHERE run=?', (adoption['run'],)).fetchone()
            now = self._now(db)
        if row is None or json.loads(row[0]) != adoption or row[1]:
            raise CapacityDenied('capacity_adoption_unavailable', 403)
        if not self.credential_active(adoption['principal'], adoption['credential'], adoption['scope'], now):
            raise CapacityDenied('capacity_credential_inactive', 403)
        if adoption['deadline'] is not None and now >= adoption['deadline']:
            raise CapacityDenied('capacity_deadline_exceeded')

    def terminal(self, run):
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE capacity_adoptions SET terminal=1 WHERE run=?', (run,))

    def dispatch(self, adoption, transport, endpoint, payload, descriptor):
        self.verify(adoption)
        if adoption['descriptor_sha256'] != _hash(descriptor):
            raise CapacityDenied('capacity_frozen_adoption_changed', 409)
        if adoption['policy_version'] is None:
            return None
        matches = [route for route in self.routes.get(adoption['policy_version'], ())
                   if route.endpoint == endpoint and route.model == payload.get('model')
                   and route.descriptor()['transport_sha256'] == CapacityRoute(transport, endpoint, payload.get('model'), route.binding).descriptor()['transport_sha256']]
        if len(matches) != 1:
            raise CapacityDenied('capacity_route_not_registered')
        return _CapacityDispatch(self, adoption, matches[0])

    def snapshot(self):
        with self.store.connection() as db:
            now = self._now(db)
            revision, version = db.execute('SELECT revision,version FROM capacity_dispatch_control WHERE id=1').fetchone()
            policies = [json.loads(row[0]) for row in db.execute('SELECT payload FROM capacity_policies ORDER BY version')]
            allocations = [json.loads(row[0]) for row in db.execute('SELECT payload FROM capacity_allocations ORDER BY id')]
            events = [{'sequence': seq, **json.loads(row)} for seq, row in db.execute('SELECT sequence,payload FROM capacity_dispatch_events ORDER BY sequence DESC LIMIT 100')]
        groups = {}
        for item in allocations:
            for group in item['groups']:
                out = groups.setdefault(group['physical'], {'group': group['group'], 'scope': group['scope'], 'rpm_used': 0, 'tpm_reserved': 0, 'concurrency_used': 0})
                if item['state'] != 'released':
                    out['rpm_used'] += now < group['rpm_until']
                    out['tpm_reserved'] += group['tokens'] if now < group['tpm_until'] else 0
                    out['concurrency_used'] += item['state'] in {'prepared', 'sent', 'uncertain'}
        return {'schema_version': 'capacity-dispatch-v1', 'revision': revision, 'active_version': version,
                'protocol': SERIALIZER, 'registered_policies': [{'version': p['version'], 'configuration_version': p['configuration_version'], 'max_seconds': p['max_seconds']} for p in policies],
                'groups': list(groups.values()), 'allocations': [{key: row[key] for key in ('id', 'policy_version', 'state', 'wire_sha256', 'serializer', 'groups')} for row in allocations],
                'events': events}

    def own(self, run):
        with self.store.connection() as db:
            row = db.execute('SELECT payload FROM capacity_adoptions WHERE run=?', (run,)).fetchone()
        if row is None:
            return None
        item = json.loads(row[0])
        return {key: item[key] for key in ('policy_version', 'configuration_version', 'protocol', 'accepted_at', 'deadline')}


class _CapacityDispatch:
    def __init__(self, controller, adoption, route):
        self.controller, self.adoption, self.route = controller, adoption, route
        self.id = None

    def prepare(self, call_id, request):
        controller, adoption, route = self.controller, self.adoption, self.route
        controller.verify(adoption)
        now = _time(controller.store.clock())
        snapshot = controller.store.snapshot(adoption['configuration_version'])
        binding = next((b for b in snapshot['bindings'] if b['id'] == route.binding), None)
        if binding is None:
            raise CapacityDenied('capacity_binding_unavailable')
        wire = hashlib.sha256(request.content).hexdigest()
        scope = {'protocol': SERIALIZER, 'endpoint': str(request.url), 'model': route.model,
                 'binding': binding, 'descriptor_sha256': adoption['descriptor_sha256']}
        scope_hash, tokens, token_until = _hash(scope), None, None
        groups, evidence = [], []
        for group_id in binding['groups']:
            group = next(g for g in snapshot['groups'] if g['id'] == group_id)
            limits = {}
            for dimension in ('rpm', 'tpm', 'concurrency'):
                local = next(d for d in group['local_limits'] if d['dimension'] == dimension)
                if local['status'] not in {'known', 'not_applicable'}:
                    raise CapacityDenied('capacity_local_limit_unavailable')
                evidence.append([group_id, 'local_limits', dimension, _hash(local)])
                effective = [local]
                if dimension in route.require_supplier:
                    supplier = next(d for d in group['supplier_limits'] if d['dimension'] == dimension)
                    if supplier['status'] not in {'known', 'not_applicable'}:
                        raise CapacityDenied('capacity_supplier_evidence_unavailable')
                    evidence.append([group_id, 'supplier_limits', dimension, _hash(supplier)])
                    effective.append(supplier)
                known = [d for d in effective if d['status'] == 'known']
                # Different supplier/local windows need a separate explicit group.
                if len({d['window_seconds'] for d in known}) > 1:
                    raise CapacityDenied('capacity_window_mismatch')
                limits[dimension] = min(known, key=lambda d: d['value']) if known else None
            if limits['tpm'] is not None and tokens is None:
                adapter = route.token_adapter
                if adapter is None:
                    raise CapacityDenied('capacity_token_bound_unavailable')
                try:
                    proof = adapter(request.content, {**scope, 'scope_sha256': scope_hash, 'wire_sha256': wire})
                except Exception:
                    raise CapacityDenied('capacity_token_bound_unavailable') from None
                if (type(proof) is not TokenUpperBound or type(proof.tokens) is not int or not 0 <= proof.tokens <= 10**12
                        or proof.wire_sha256 != wire or proof.scope_sha256 != scope_hash
                        or proof.version != getattr(adapter, 'version', None) or now >= _time(proof.valid_until)):
                    raise CapacityDenied('capacity_token_bound_invalid')
                _identity(proof.evidence_ref)
                tokens, token_until = proof.tokens, proof.valid_until
            groups.append({'group': group_id, 'scope': group['scope'], 'physical': _hash(group['scope']),
                'rpm_until': now + limits['rpm']['window_seconds'] if limits['rpm'] else now,
                'tpm_until': now + limits['tpm']['window_seconds'] if limits['tpm'] else now,
                'tokens': tokens if limits['tpm'] else 0,
                'caps': {key: item['value'] if item else None for key, item in limits.items()}})
        identifier = _hash([adoption['run'], call_id, route.descriptor()])
        with controller.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            now = controller._now(db, write=True)
            if token_until is not None and now >= token_until:
                raise CapacityDenied('capacity_token_bound_expired')
            if db.execute('SELECT 1 FROM capacity_allocations WHERE id=?', (identifier,)).fetchone():
                raise CapacityDenied('capacity_dispatch_already_prepared', 409)
            prior = [json.loads(row[0]) for row in db.execute('SELECT payload FROM capacity_allocations')]
            for group in groups:
                used = {'rpm': 0, 'tpm': 0, 'concurrency': 0}
                for item in prior:
                    if item['state'] == 'released':
                        continue
                    for older in item['groups']:
                        if older['physical'] == group['physical']:
                            used['rpm'] += now < older['rpm_until']
                            used['tpm'] += older['tokens'] if now < older['tpm_until'] else 0
                            used['concurrency'] += item['state'] in {'prepared', 'sent', 'uncertain'}
                for dimension in used:
                    cap = group['caps'][dimension]
                    amount = group['tokens'] if dimension == 'tpm' else 1
                    if cap is not None and used[dimension] + amount > cap:
                        # No group rows have been inserted, so all-group refusal is atomic.
                        raise CapacityDenied('capacity_' + dimension + '_exhausted')
            allocation = {'id': identifier, 'run': adoption['run'], 'policy_version': adoption['policy_version'],
                'wire_sha256': wire, 'scope_sha256': scope_hash, 'serializer': SERIALIZER,
                'state': 'prepared', 'prepared_at': now, 'groups': groups, 'evidence': evidence,
                'token_valid_until': token_until}
            db.execute('INSERT INTO capacity_allocations VALUES(?,?)', (identifier, _json(allocation)))
            controller._event(db, {'kind': 'prepared', 'allocation': identifier, 'version': adoption['policy_version'], 'at': now})
        self.id = identifier
        return {'protocol': SERIALIZER, 'wire_sha256': wire, 'scope_sha256': scope_hash,
                'capacity_allocation': identifier}

    def _update(self, state, *, tokens=None):
        if self.id is None:
            return
        with self.controller.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.controller._now(db, write=True)
            row = db.execute('SELECT payload FROM capacity_allocations WHERE id=?', (self.id,)).fetchone()
            value = json.loads(row[0])
            if state == 'released' and value['state'] != 'prepared':
                raise CapacityDenied('capacity_sent_cannot_release', 409)
            if state == 'sent' and value['state'] != 'prepared':
                raise CapacityDenied('capacity_dispatch_already_sent', 409)
            value['state'] = state
            if tokens is not None:
                for group in value['groups']:
                    if group['tokens'] and tokens <= group['tokens']:
                        group['tokens'] = tokens
            db.execute('UPDATE capacity_allocations SET payload=? WHERE id=?', (_json(value), self.id))
            self.controller._event(db, {'kind': state, 'allocation': self.id, 'at': now})

    def before_post(self):
        self.controller.verify(self.adoption)
        with self.controller.store.connection() as db:
            allocation = json.loads(db.execute('SELECT payload FROM capacity_allocations WHERE id=?', (self.id,)).fetchone()[0])
            now = self.controller._now(db)
        if allocation['token_valid_until'] is not None and now >= allocation['token_valid_until']:
            raise CapacityDenied('capacity_token_bound_expired')
        snapshot = self.controller.store.snapshot(self.adoption['configuration_version'])
        for group_id, side, dimension, digest in allocation['evidence']:
            group = next(group for group in snapshot['groups'] if group['id'] == group_id)
            item = next(item for item in group[side] if item['dimension'] == dimension)
            if _hash(item) != digest or item['status'] not in {'known', 'not_applicable'}:
                raise CapacityDenied('capacity_evidence_changed_or_expired')
        self._update('sent')
        return True

    def release(self):
        self._update('released')

    def complete(self, record):
        from .client import usage
        info = usage(record)
        raw = info.get('usage', {})
        tokens = None
        if isinstance(raw, dict) and all(type(raw.get(key)) is int and raw[key] >= 0 for key in ('prompt_tokens', 'completion_tokens')):
            tokens = raw['prompt_tokens'] + raw['completion_tokens']
        self._update('uncertain' if record.get('interrupted') or info['cost_unknown'] else 'complete', tokens=tokens)
