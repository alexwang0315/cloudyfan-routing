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
from pathlib import Path
from urllib.parse import urlsplit

from .receipt_store import ReceiptStore, _immutable

from .capacity import CapacityStore, _identity, _time, _live_capacity

_INGRESS = ContextVar('capacity_authenticated_ingress', default=None)
SERIALIZER = 'httpx-prepared-json-v1'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _transport_identity(transport):
    """Private runtime equality only; never serialized or projected."""
    http = transport.http
    return (getattr(transport, 'key', None), id(http), id(http._transport), http.auth,
            tuple(http.headers.raw), tuple((c.domain, c.path, c.name, c.value) for c in http.cookies.jar),
            tuple((kind, tuple(map(id, hooks))) for kind, hooks in http.event_hooks.items()),
            tuple((str(pattern.pattern), id(value)) for pattern, value in http._mounts.items()))


def _group_usage(allocations, group, now):
    used = {'rpm': 0, 'tpm': 0, 'concurrency': 0}
    for item in allocations:
        if item['state'] == 'released':
            continue
        held = item['state'] in {'prepared', 'sent', 'uncertain'}
        for older in item['groups']:
            if older['physical'] == group['physical']:
                used['rpm'] += older['caps']['rpm'] is not None and (held or now < older['rpm_until'])
                used['tpm'] += older['tokens'] if held or now < older['tpm_until'] else 0
                used['concurrency'] += held
    return used


class CapacityDenied(RuntimeError):
    control_plane_error = True

    def __init__(self, code, status=429):
        if not re.fullmatch(r'capacity_[a-z_]{1,64}', code):
            raise ValueError('Safe capacity reason required')
        self.code, self.status = code, status
        super().__init__(code)


def _require_http_protocol(http):
    import httpx
    methods = ('send', 'build_request', '_send_handling_auth', '_send_handling_redirects',
               '_send_single_request', '_build_request_auth', '_build_redirect_request')
    if (type(http) is not httpx.Client or any(name in vars(http) for name in methods)
            or http.auth is not None or any(http.event_hooks.values()) or http.follow_redirects
            or any(transport is not None for transport in http._mounts.values())):
        raise CapacityDenied('capacity_transport_protocol_unsupported')
    transport = http._transport
    if type(transport) is not httpx.MockTransport and not (
            type(transport) is httpx.HTTPTransport and getattr(transport._pool, '_retries', None) == 0):
        raise CapacityDenied('capacity_transport_protocol_unsupported')


def prepare_request(http, endpoint, payload, *, headers=None):
    """Supported one-send httpx protocol. Custom hooks/auth/retries fail closed."""
    _require_http_protocol(http)
    request = http.build_request('POST', endpoint, json=payload, headers=headers, timeout=180)
    if request.method != 'POST' or str(request.url) != endpoint:
        raise CapacityDenied('capacity_transport_protocol_unsupported')
    return request


def require_model_transport(transport):
    """Opt-in supports the audited receipt/admission call protocol only."""
    from .client import Client
    from .local import LocalBackend
    if type(transport) not in {Client, LocalBackend} or 'call' in vars(transport):
        raise CapacityDenied('capacity_transport_protocol_unsupported')


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
    provider_name: str | None = None
    accepted_models: tuple = ()

    def descriptor(self):
        # Hashes bind controlled runtime identity without persisting secret values.
        config = getattr(self.transport, 'config', None)
        return {'transport_sha256': _hash({'root': str(self.transport.root.absolute()),
                    'configuration': config}),
                'endpoint': self.endpoint, 'model': self.model, 'binding': _identity(self.binding),
                'token_adapter_version': getattr(self.token_adapter, 'version', None),
                'require_supplier': list(self.require_supplier), 'provider_name': self.provider_name,
                'accepted_models': list(self.accepted_models)}


@dataclass(frozen=True)
class CapacityToolRequest:
    """A host environment opts into one registered physical HTTP POST."""
    transport: object = field(repr=False)
    payload: dict = field(repr=False)


class CapacityToolTransport(ReceiptStore):
    """Single-send tool adapter with durable private request/response evidence."""
    def __init__(self, http, root, *, endpoint, version, decode):
        from .capacity import _private_path
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not callable(decode)):
            raise ValueError('Controlled HTTPS tool endpoint and decoder required')
        root = Path(root).absolute()
        _private_path(root / '.capacity-tool.lock', create=True)
        super().__init__(root)
        self.http, self.decode = http, decode
        self.config = {'protocol': 'capacity-tool-http-v1', 'version': _identity(version), 'endpoint': endpoint}

    def prepare(self, cid, payload):
        if (self._directory(cid) / 'intent.json').exists():
            raise CapacityDenied('capacity_tool_replay_forbidden', 409)
        return prepare_request(self.http, self.config['endpoint'], payload)

    def post(self, cid, request, before_post, *, response_headers=None):
        from .capacity import _private_path
        _private_path(self.root / '.capacity-tool.lock')
        with self._lock():
            directory = self._directory(cid)
            if (directory / 'intent.json').exists():
                raise CapacityDenied('capacity_tool_replay_forbidden', 409)
            directory.mkdir(mode=0o700)
            _immutable(directory / 'intent.json', {'call_id': cid, 'configuration': self.config,
                'wire_sha256': hashlib.sha256(request.content).hexdigest()})
            denied_code = None
            try:
                admitted = before_post() is True
            except Exception as error:
                denied_code = error.code if isinstance(error, CapacityDenied) else None
                admitted = False
            if not admitted:
                record = {'call_id': cid, 'http_status': None, 'raw': None,
                    'not_dispatched': True, 'error': 'enforcement_denied_before_post'}
                if denied_code:
                    record['capacity_denial_code'] = denied_code
            else:
                try:
                    response = self.http.send(request, follow_redirects=False)
                    record = {'call_id': cid, 'http_status': response.status_code, 'raw': response.text}
                except Exception:
                    record = {'call_id': cid, 'http_status': None, 'raw': None, 'interrupted': True,
                              'error': 'capacity_tool_transport_failure'}
                if record['http_status'] is not None and callable(response_headers):
                    try:
                        observation = response_headers(response)
                    except Exception:
                        observation = {'protocol': 'capacity-window-headers-v1', 'values': [],
                                       'invalid': True, 'observed_at': None}
                    if observation is not None:
                        record['window_headers'] = observation
            _immutable(directory / 'response.json', record)
            return record

    def result(self, record):
        import httpx
        if record.get('http_status') is None or record.get('raw') is None:
            from .budget import BudgetError
            raise BudgetError('External tool result unknown; execution stopped')
        return self.decode(httpx.Response(record['http_status'], text=record['raw']))


def require_tool_transport(transport):
    if (type(transport) is not CapacityToolTransport
            or any(name in vars(transport) for name in ('prepare', 'post', 'result'))):
        raise CapacityDenied('capacity_transport_protocol_unsupported')


class CapacityController:
    """A registered host authority. Neither request JSON nor browser registers policy."""
    def __init__(self, store, *, credential_active, history_limit=100, max_live_allocations=1000,
                 window_backend=None):
        if type(store) is not CapacityStore or not callable(credential_active):
            raise ValueError('Controlled capacity store and credential verifier required')
        if (type(history_limit) is not int or not 1 <= history_limit <= 100
                or type(max_live_allocations) is not int or not 1 <= max_live_allocations <= 10000):
            raise ValueError('Bounded capacity history and live state required')
        self.history_limit, self.max_live_allocations = history_limit, max_live_allocations
        self.store, self.credential_active = store, credential_active
        self.routes, self._route_credentials = {}, {}
        self.window_backend = window_backend
        with store.connection(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS capacity_policies(version TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_dispatch_control(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,version TEXT);
                INSERT OR IGNORE INTO capacity_dispatch_control VALUES(1,0,NULL);
                CREATE TABLE IF NOT EXISTS capacity_adoptions(run TEXT PRIMARY KEY,payload TEXT NOT NULL,terminal INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_allocations(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_dispatch_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_invalid_policies(version TEXT PRIMARY KEY,reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_dispatch_metadata(id INTEGER PRIMARY KEY CHECK(id=1),clock_floor REAL NOT NULL);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(capacity_allocations)')}
            if 'state' not in columns:
                db.execute('ALTER TABLE capacity_allocations ADD COLUMN state TEXT')
                db.execute('ALTER TABLE capacity_allocations ADD COLUMN horizon REAL')
            for identifier, encoded in db.execute('SELECT id,payload FROM capacity_allocations WHERE state IS NULL').fetchall():
                value = json.loads(encoded)
                horizon = max((max(g['rpm_until'], g['tpm_until']) for g in value['groups']), default=value['prepared_at'])
                db.execute('UPDATE capacity_allocations SET state=?,horizon=? WHERE id=?', (value['state'], horizon, identifier))
            db.execute('CREATE INDEX IF NOT EXISTS capacity_hot_state ON capacity_allocations(state,horizon)')
            db.execute('CREATE INDEX IF NOT EXISTS capacity_live_adoption ON capacity_adoptions(terminal)')

    def _now(self, db, *, write=False):
        try:
            now = _time(self.store.clock())
        except (ValueError, OverflowError):
            raise CapacityDenied('capacity_clock_invalid', 409) from None
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

    def register(self, version, *, configuration_version, routes, max_seconds, windows_version=None, queue_version=None):
        _identity(version)
        _identity(configuration_version)
        if type(max_seconds) is not int or not 1 <= max_seconds <= 86400 or not routes or len(routes) > 512:
            raise ValueError('Bounded registered dispatch policy required')
        config = self.store.snapshot(configuration_version)
        if windows_version is not None:
            _identity(windows_version)
            self._window_backend(windows_version)
        if queue_version is not None:
            _identity(queue_version)
            self._queue_backend(queue_version)
        if not config['configured'] or config['configuration_version'] != configuration_version:
            raise ValueError('Registered capacity configuration required')
        bindings = {binding['id'] for binding in config['bindings']}
        physical = [_hash(group['scope']) for group in config['groups']]
        if len(physical) != len(set(physical)):
            raise ValueError('One capacity group per physical scope required')
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
            if (not isinstance(route.accepted_models, (tuple, list)) or len(route.accepted_models) > 32
                    or any(not isinstance(model, str) or not model or len(model) > 256 for model in route.accepted_models)
                    or route.provider_name is not None and (not isinstance(route.provider_name, str)
                        or not route.provider_name or len(route.provider_name) > 256)):
                raise ValueError('Explicit actual model and provider identities required')
            if route.token_adapter is not None:
                _identity(descriptor['token_adapter_version'])
            descriptors.append(descriptor)
        value = {'version': version, 'configuration_version': configuration_version,
                 'configuration_sha256': config['configuration_sha256'], 'max_seconds': max_seconds,
                 'protocol': SERIALIZER, 'routes': descriptors}
        if windows_version is not None:
            value['windows_version'] = windows_version
        if queue_version is not None:
            value['queue_version'] = queue_version
            value['queue_sha256'] = self._queue_backend(queue_version).sha256
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT payload FROM capacity_policies WHERE version=?', (version,)).fetchone()
            if prior and prior[0] != _json(value):
                raise ValueError('An immutable capacity policy cannot change')
            db.execute('INSERT OR IGNORE INTO capacity_policies VALUES(?,?)', (version, _json(value)))
        self.routes[version] = tuple(routes)
        self._route_credentials[version] = tuple(_transport_identity(route.transport) for route in routes)

    def _queue_backend(self, version):
        from .capacity_queue import QueuePreparation
        backend = getattr(self, 'queue_backend', None)
        if type(backend) is not QueuePreparation or backend.version != version:
            raise CapacityDenied('capacity_queue_backend_unavailable')
        return backend

    def queue_policy(self, run=None):
        with self.store.connection() as db:
            if run is not None:
                row = db.execute('SELECT payload FROM capacity_adoptions WHERE run=?', (run,)).fetchone()
                return json.loads(row[0]).get('queue_version') if row else None
            version = db.execute('SELECT version FROM capacity_dispatch_control WHERE id=1').fetchone()[0]
            row = db.execute('SELECT payload FROM capacity_policies WHERE version=?', (version,)).fetchone()
        return json.loads(row[0]).get('queue_version') if row else None

    def automatic_reconciliation_allowed(self, adoption):
        if adoption.get('queue_version') is not None:
            return False
        with self.store.connection() as db:
            row = db.execute('SELECT payload FROM capacity_policies WHERE version=?', (adoption['policy_version'],)).fetchone()
        if row is None:
            if adoption['policy_version'] is None:
                return True
            raise CapacityDenied('capacity_policy_not_registered')
        return json.loads(row[0]).get('windows_version') is None

    def _window_backend(self, version):
        backend = self.window_backend
        if (backend is None or getattr(backend, 'protocol', None) != 'capacity-windows-v1'
                or getattr(backend, 'version', None) != version
                or any(not callable(getattr(backend, name, None)) for name in ('check', 'record_response', 'snapshot'))):
            raise CapacityDenied('capacity_window_backend_unavailable')
        return backend

    def window_scope(self, adoption, route, snapshot, wire_sha256):
        binding = next((b for b in snapshot['bindings'] if b['id'] == route.binding), None)
        if binding is None:
            raise CapacityDenied('capacity_binding_unavailable')
        groups = [{'group': group['id'], 'scope': group['scope'], 'physical': _hash(group['scope'])}
            for group in snapshot['groups'] if group['id'] in binding['groups']]
        return {'protocol': 'capacity-windows-v1', 'policy_version': adoption['policy_version'],
            'configuration_version': snapshot['configuration_version'],
            'configuration_sha256': snapshot['configuration_sha256'], 'endpoint': route.endpoint,
            'model': route.model, 'binding': binding, 'descriptor_sha256': adoption['descriptor_sha256'],
            'provider_name': route.provider_name, 'accepted_models': list(route.accepted_models or (route.model,)),
            'wire_sha256': wire_sha256}, groups

    def check_window(self, db, scope, groups, now, version):
        if version is None:
            return {'status': 'open', 'not_before': None, 'evidence_refs': [], 'revision': 0,
                    'reason_code': 'capacity_window_not_applicable'}
        try:
            result = self._window_backend(version).check(db, scope, groups, now)
            if (not isinstance(result, dict) or set(result) != {
                    'status', 'not_before', 'evidence_refs', 'revision', 'reason_code'}
                    or result['status'] not in {'open', 'wait', 'unknown', 'denied'}
                    or type(result['revision']) is not int or result['revision'] < 0
                    or not isinstance(result['evidence_refs'], list) or len(result['evidence_refs']) > 32
                    or not isinstance(result['reason_code'], str)
                    or not re.fullmatch(r'capacity_[a-z_]{1,64}', result['reason_code'])):
                raise ValueError('Invalid window decision')
            for reference in result['evidence_refs']:
                _identity(reference)
            if result['not_before'] is not None:
                _time(result['not_before'])
            return result
        except CapacityDenied:
            raise
        except Exception:
            raise CapacityDenied('capacity_window_state_unavailable') from None

    def candidate_window(self, adoption, transport, profile, descriptor):
        with self.store.connection() as db:
            policy = db.execute('SELECT payload FROM capacity_policies WHERE version=?', (adoption['policy_version'],)).fetchone()
        if policy is not None and json.loads(policy[0]).get('windows_version') is None:
            return None
        endpoint = transport.config['endpoint'] if profile.get('backend') == 'local' else 'https://openrouter.ai/api/v1/chat/completions'
        dispatch = self.dispatch(adoption, transport, endpoint, {'model': profile['model_version']}, descriptor)
        snapshot = self.store.snapshot(adoption['configuration_version'])
        scope, groups = self.window_scope(adoption, dispatch.route, snapshot, None)
        with self.store.connection() as db:
            return self.check_window(db, scope, groups, self._now(db), dispatch.windows_version)

    def window_snapshot(self):
        if self.window_backend is None:
            with self.store.connection() as db:
                configured = any(json.loads(row[0]).get('windows_version') is not None
                    for row in db.execute('SELECT payload FROM capacity_policies'))
            if configured:
                return {'configured': True, 'protocol': 'capacity-windows-v1', 'status': 'unknown',
                        'reason_code': 'capacity_window_backend_unavailable'}
            return {'configured': False, 'protocol': 'capacity-windows-v1', 'status': 'not_applicable'}
        try:
            backend = self._window_backend(self.window_backend.version)
            result = backend.snapshot()
            if not isinstance(result, dict):
                raise ValueError('Invalid window snapshot')
            return result
        except Exception:
            return {'configured': True, 'protocol': 'capacity-windows-v1', 'status': 'unknown',
                    'reason_code': 'capacity_window_state_unavailable'}

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
            if policy is not None and policy.get('queue_version') is not None:
                backend = self._queue_backend(policy['queue_version'])
                if policy['queue_sha256'] != backend.sha256:
                    raise CapacityDenied('capacity_queue_contract_changed')
                value.update(queue_version=backend.version, queue_sha256=backend.sha256)
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
            invalid = db.execute('SELECT reason FROM capacity_invalid_policies WHERE version=?', (adoption['policy_version'],)).fetchone()
        if invalid:
            raise CapacityDenied('capacity_window_observation_unavailable' if invalid[0] ==
                'capacity_window_observation_unavailable' else 'capacity_token_proof_invalidated')
        if row is None or json.loads(row[0]) != adoption or row[1]:
            raise CapacityDenied('capacity_adoption_unavailable', 403)
        if not self.credential_active(adoption['principal'], adoption['credential'], adoption['scope'], now):
            raise CapacityDenied('capacity_credential_inactive', 403)
        if adoption['deadline'] is not None and now >= adoption['deadline']:
            raise CapacityDenied('capacity_deadline_exceeded')

    def refused(self, run, code):
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._now(db, write=True)
            except CapacityDenied:
                now = None
            row = db.execute('SELECT payload FROM capacity_adoptions WHERE run=?', (run,)).fetchone()
            if row is not None:
                value = json.loads(row[0])
                self._event(db, {'kind': 'refused', 'version': value['policy_version'], 'reason_code': code, 'at': now})

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
        with self.store.connection() as db:
            policy = json.loads(db.execute('SELECT payload FROM capacity_policies WHERE version=?', (adoption['policy_version'],)).fetchone()[0])
        matches = []
        for index, route in enumerate(self.routes.get(adoption['policy_version'], ())):
            if (route.endpoint == endpoint and route.model == payload.get('model')
                    and route.descriptor() in policy['routes']
                    and route.descriptor()['transport_sha256'] == CapacityRoute(
                        transport, endpoint, payload.get('model'), route.binding).descriptor()['transport_sha256']):
                pin = self._route_credentials[adoption['policy_version']][index]
                if _transport_identity(transport) != pin:
                    raise CapacityDenied('capacity_transport_binding_changed')
                matches.append((route, pin))
        if len(matches) != 1:
            raise CapacityDenied('capacity_route_not_registered')
        return _CapacityDispatch(self, adoption, *matches[0], windows_version=policy.get('windows_version'))

    def snapshot(self):
        with self.store.connection() as db:
            now = self._now(db)
            revision, version = db.execute('SELECT revision,version FROM capacity_dispatch_control WHERE id=1').fetchone()
            policies = [json.loads(row[0]) for row in db.execute('SELECT payload FROM capacity_policies ORDER BY version')]
            allocation_count = db.execute('SELECT COUNT(*) FROM capacity_allocations').fetchone()[0]
            event_count = db.execute('SELECT COUNT(*) FROM capacity_dispatch_events').fetchone()[0]
            limit = min(100, max(1, self.history_limit))
            allocations = [json.loads(row[0]) for row in db.execute('SELECT payload FROM capacity_allocations ORDER BY rowid DESC LIMIT ?', (limit,))]
            hot = [json.loads(row[0]) for row in db.execute("SELECT payload FROM capacity_allocations WHERE state IN ('prepared','sent','uncertain') OR (state='complete' AND horizon>?)", (now,))]
            events = [{'sequence': seq, **json.loads(row)} for seq, row in db.execute('SELECT sequence,payload FROM capacity_dispatch_events ORDER BY sequence DESC LIMIT ?', (limit,))]
        groups = {group['physical']: {'group': group['group'], 'scope': group['scope'], 'rpm_used': 0,
            'tpm_reserved': 0, 'concurrency_used': 0} for item in allocations for group in item['groups']}
        for item in hot:
            for group in item['groups']:
                out = groups.setdefault(group['physical'], {'group': group['group'], 'scope': group['scope'], 'rpm_used': 0, 'tpm_reserved': 0, 'concurrency_used': 0})
                if item['state'] != 'released':
                    held = item['state'] in {'prepared', 'sent', 'uncertain'}
                    out['rpm_used'] += group['caps']['rpm'] is not None and (held or now < group['rpm_until'])
                    out['tpm_reserved'] += group['tokens'] if held or now < group['tpm_until'] else 0
                    out['concurrency_used'] += item['state'] in {'prepared', 'sent', 'uncertain'}
        return {'schema_version': 'capacity-dispatch-v1', 'revision': revision, 'active_version': version,
                'protocol': SERIALIZER, 'registered_policies': [{
                    **{key: p[key] for key in ('version', 'configuration_version', 'max_seconds')},
                    **({'windows_version': p['windows_version']} if 'windows_version' in p else {})} for p in policies],
                'allocation_count': allocation_count, 'allocation_history_complete': allocation_count == len(allocations),
                'event_count': event_count, 'event_history_complete': event_count == len(events),
                'groups': list(groups.values()), 'allocations': [{key: row[key] for key in ('id', 'policy_version', 'state', 'wire_sha256', 'serializer', 'groups')} for row in allocations],
                'events': events, 'windows': self.window_snapshot()}

    def own(self, run):
        with self.store.connection() as db:
            row = db.execute('SELECT payload FROM capacity_adoptions WHERE run=?', (run,)).fetchone()
        if row is None:
            return None
        item = json.loads(row[0])
        return {key: item[key] for key in ('policy_version', 'configuration_version', 'protocol', 'accepted_at', 'deadline')}


class _CapacityDispatch:
    def __init__(self, controller, adoption, route, credential_pin, *, windows_version=None):
        self.controller, self.adoption, self.route = controller, adoption, route
        self._credential_pin = credential_pin
        self.id = None
        self.windows_version = windows_version

    def response_headers(self, response):
        if self.windows_version is None:
            return None
        names = {'retry-after', 'ratelimit-reset', 'x-ratelimit-reset',
                 'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens', 'date'}
        values, seen, invalid, size = [], set(), False, 0
        secret = getattr(self.route.transport, 'key', None)
        for name, value in response.headers.multi_items():
            name = name.lower()
            if name not in names:
                continue
            size += len(name.encode()) + len(value.encode())
            bad = (name in seen or len(values) >= 8 or len(value) > 160 or size > 1024
                   or any(c in value for c in ('\r', '\n', '\x00'))
                   or bool(secret and secret in value))
            invalid = invalid or bad
            seen.add(name)
            if not bad:
                values.append([name, value])
        try:
            observed = _time(self.controller.store.clock())
        except Exception:
            observed, invalid = None, True
        return {'protocol': 'capacity-window-headers-v1', 'values': values,
                'invalid': invalid, 'observed_at': observed}

    def _record_window_response(self, record):
        if self.windows_version is None:
            return
        root = self.route.transport.root
        response = root / 'calls' / record['call_id'] / 'response.json'
        snapshot = self.controller.store.snapshot(self.adoption['configuration_version'])
        with self.controller.store.connection() as db:
            allocation = json.loads(db.execute('SELECT payload FROM capacity_allocations WHERE id=?', (self.id,)).fetchone()[0])
        scope, groups = self.controller.window_scope(self.adoption, self.route, snapshot, allocation['wire_sha256'])
        scope['physical_groups'] = groups
        try:
            scope['observed_at'] = _time(record['window_headers']['observed_at'])
        except Exception:
            scope.update(observed_at=None, time_error='capacity_clock_invalid')
        try:
            self.controller._window_backend(self.windows_version).record_response(
                {'root': str(root.resolve()), 'call_id': record['call_id'],
                 'response_sha256': hashlib.sha256(response.read_bytes()).hexdigest()}, scope)
        except Exception:
            with self.controller.store.connection(write=True) as db:
                db.execute('INSERT OR IGNORE INTO capacity_invalid_policies VALUES(?,?)',
                    (self.adoption['policy_version'], 'capacity_window_observation_unavailable'))
            raise CapacityDenied('capacity_window_observation_unavailable') from None

    def prepare(self, call_id, request, *, reserve=True):
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
                evidence.append([group_id, 'local_limits', dimension, _hash(local), local['valid_until']])
                effective = [local]
                if dimension in route.require_supplier:
                    supplier = next(d for d in group['supplier_limits'] if d['dimension'] == dimension)
                    if supplier['status'] not in {'known', 'not_applicable'}:
                        raise CapacityDenied('capacity_supplier_evidence_unavailable')
                    evidence.append([group_id, 'supplier_limits', dimension, _hash(supplier), supplier['valid_until']])
                    effective.append(supplier)
                known = [d for d in effective if d['status'] == 'known']
                # Different windows for one physical scope are unsupported.
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
                try:
                    valid = (type(proof) is TokenUpperBound and type(proof.tokens) is int and 0 <= proof.tokens <= 10**12
                        and proof.wire_sha256 == wire and proof.scope_sha256 == scope_hash
                        and proof.version == getattr(adapter, 'version', None) and now < _time(proof.valid_until))
                    if valid:
                        _identity(proof.evidence_ref)
                except (ValueError, TypeError, OverflowError):
                    valid = False
                if not valid:
                    raise CapacityDenied('capacity_token_bound_invalid')
                tokens, token_until = proof.tokens, proof.valid_until
            groups.append({'group': group_id, 'scope': group['scope'], 'physical': _hash(group['scope']),
                'rpm_window': limits['rpm']['window_seconds'] if limits['rpm'] else None,
                'tpm_window': limits['tpm']['window_seconds'] if limits['tpm'] else None,
                'rpm_until': now + limits['rpm']['window_seconds'] if limits['rpm'] else now,
                'tpm_until': now + limits['tpm']['window_seconds'] if limits['tpm'] else now,
                'tokens': tokens if limits['tpm'] else 0,
                'caps': {key: item['value'] if item else None for key, item in limits.items()}})
        identifier = _hash([adoption['run'], call_id, route.descriptor()])
        window_scope, physical_groups = controller.window_scope(adoption, route, snapshot, wire)
        with controller.store.connection(write=reserve) as db:
            if reserve:
                db.execute('BEGIN IMMEDIATE')
            now = controller._now(db, write=reserve)
            window = controller.check_window(db, window_scope, physical_groups, now, self.windows_version)
            if window['status'] != 'open':
                raise CapacityDenied(window['reason_code'])
            if token_until is not None and now >= token_until:
                raise CapacityDenied('capacity_token_bound_expired')
            if db.execute('SELECT 1 FROM capacity_allocations WHERE id=?', (identifier,)).fetchone():
                raise CapacityDenied('capacity_dispatch_already_prepared', 409)
            prior = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM capacity_allocations WHERE state IN ('prepared','sent','uncertain') OR (state='complete' AND horizon>?) LIMIT ?",
                (now, controller.max_live_allocations + 1))]
            if len(prior) >= controller.max_live_allocations:
                raise CapacityDenied('capacity_live_state_limit_exceeded')
            for group in groups:
                used = _group_usage(prior, group, now)
                for dimension in used:
                    cap = group['caps'][dimension]
                    amount = group['tokens'] if dimension == 'tpm' else 1
                    if cap is not None and used[dimension] + amount > cap:
                        # No group rows have been inserted, so all-group refusal is atomic.
                        raise CapacityDenied('capacity_' + dimension + '_exhausted')
            if not reserve:
                return {'protocol': SERIALIZER, 'wire_sha256': wire, 'scope_sha256': scope_hash,
                        'capacity_allocation': identifier}
            allocation = {'id': identifier, 'run': adoption['run'], 'policy_version': adoption['policy_version'],
                'wire_sha256': wire, 'scope_sha256': scope_hash, 'serializer': SERIALIZER,
                'state': 'prepared', 'prepared_at': now, 'groups': groups, 'evidence': evidence,
                'token_valid_until': token_until, 'evidence_valid_until': min(item[4] for item in evidence)}
            if self.windows_version is not None:
                allocation.update(window_scope=window_scope, windows_version=self.windows_version,
                    window_decision=window, physical_groups=physical_groups)
            horizon = max((max(g['rpm_until'], g['tpm_until']) for g in groups), default=now)
            db.execute('INSERT INTO capacity_allocations VALUES(?,?,?,?)', (identifier, _json(allocation), 'prepared', horizon))
            controller._event(db, {'kind': 'prepared', 'allocation': identifier, 'version': adoption['policy_version'], 'at': now})
        self.id = identifier
        return {'protocol': SERIALIZER, 'wire_sha256': wire, 'scope_sha256': scope_hash,
                'capacity_allocation': identifier}

    def _update(self, state, *, tokens=None, bound_violation=False):
        if self.id is None:
            return
        with self.controller.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM capacity_allocations WHERE id=?', (self.id,)).fetchone()
            value = json.loads(row[0])
            try:
                now = self.controller._now(db, write=True)
            except CapacityDenied:
                if state != 'released' or value['state'] != 'prepared':
                    raise
                # Durable never-dispatched proof can release without inventing
                # a current valid clock or moving its persisted floor backwards.
                now = None
            if state == 'released' and value['state'] != 'prepared':
                raise CapacityDenied('capacity_sent_cannot_release', 409)
            if state == 'sent' and value['state'] != 'prepared':
                raise CapacityDenied('capacity_dispatch_already_sent', 409)
            if state == 'sent':
                if value.get('windows_version') is not None:
                    decision = self.controller.check_window(db, value['window_scope'],
                        value['physical_groups'], now, value['windows_version'])
                    if decision['status'] != 'open':
                        raise CapacityDenied(decision['reason_code'])
                if value['token_valid_until'] is not None and now >= value['token_valid_until']:
                    raise CapacityDenied('capacity_token_bound_expired')
                if value.get('evidence_valid_until') is not None and now >= value['evidence_valid_until']:
                    raise CapacityDenied('capacity_evidence_changed_or_expired')
                prior = [json.loads(row[0]) for row in db.execute(
                    "SELECT payload FROM capacity_allocations WHERE id!=? AND (state IN ('prepared','sent','uncertain') OR (state='complete' AND horizon>?)) LIMIT ?",
                    (self.id, now, self.controller.max_live_allocations + 1))]
                if len(prior) >= self.controller.max_live_allocations:
                    raise CapacityDenied('capacity_live_state_limit_exceeded')
                for group in value['groups']:
                    used = _group_usage(prior, group, now)
                    for dimension, amount in [('rpm', 1), ('tpm', group['tokens']), ('concurrency', 1)]:
                        if group['caps'][dimension] is not None and used[dimension] + amount > group['caps'][dimension]:
                            raise CapacityDenied('capacity_' + dimension + '_exhausted')
                value['dispatch_intent_at'] = now
            value['state'] = state
            if state == 'complete':
                # Completion is a trustworthy upper bound on physical send time.
                # A marker, scheduling gap or pool wait is not wire-time proof.
                value['completed_at'] = now
                for group in value['groups']:
                    for dimension in ('rpm', 'tpm'):
                        window = group.get(dimension + '_window', group[dimension + '_until'] - value['prepared_at'])
                        group[dimension + '_until'] = now + window if group['caps'][dimension] is not None else now
            if tokens is not None:
                for group in value['groups']:
                    if group['caps']['tpm'] is not None:
                        group['tokens'] = tokens
            if bound_violation:
                value['reason_code'] = 'capacity_token_bound_violated'
                db.execute('INSERT OR IGNORE INTO capacity_invalid_policies VALUES(?,?)',
                    (value['policy_version'], 'capacity_token_bound_violated'))
                self.controller._event(db, {'kind': 'bound_violation', 'allocation': self.id,
                    'version': value['policy_version'], 'reason_code': 'capacity_token_bound_violated', 'at': now})
            horizon = max((max(group['rpm_until'], group['tpm_until']) for group in value['groups']), default=value['prepared_at'])
            db.execute('UPDATE capacity_allocations SET payload=?,state=?,horizon=? WHERE id=?', (_json(value), state, horizon, self.id))
            self.controller._event(db, {'kind': state, 'allocation': self.id, 'at': now})

    def before_post(self):
        self.controller.verify(self.adoption)
        _require_http_protocol(self.route.transport.http)
        if _transport_identity(self.route.transport) != self._credential_pin:
            raise CapacityDenied('capacity_transport_binding_changed')
        with self.controller.store.connection() as db:
            allocation = json.loads(db.execute('SELECT payload FROM capacity_allocations WHERE id=?', (self.id,)).fetchone()[0])
            now = self.controller._now(db)
        if allocation['token_valid_until'] is not None and now >= allocation['token_valid_until']:
            raise CapacityDenied('capacity_token_bound_expired')
        snapshot = self.controller.store.snapshot(self.adoption['configuration_version'])
        for saved in allocation['evidence']:
            group_id, side, dimension, digest = saved[:4]
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
        accepted = self.route.accepted_models or (self.route.model,)
        local = record.get('execution_kind') == 'local'
        identity = (info.get('model') in accepted and not record.get('duplicate_generation_id')
            and (record.get('configuration') == self.route.transport.descriptor() if local else
                 bool(info.get('generation_id')) and info.get('provider') == self.route.provider_name))
        tokens = None
        if identity and isinstance(raw, dict):
            pairs = [('prompt_tokens', 'completion_tokens'), ('input_tokens', 'output_tokens')]
            quantities = [raw[a] + raw[b] for a, b in pairs
                if all(type(raw.get(key)) is int and raw[key] >= 0 for key in (a, b))]
            if quantities and len(set(quantities)) == 1 and (
                    'total_tokens' not in raw or type(raw['total_tokens']) is int and raw['total_tokens'] == quantities[0]):
                tokens = quantities[0]
        with self.controller.store.connection() as db:
            allocation = json.loads(db.execute('SELECT payload FROM capacity_allocations WHERE id=?', (self.id,)).fetchone()[0])
        violation = tokens is not None and any(group['caps']['tpm'] is not None and tokens > group['tokens']
                                             for group in allocation['groups'])
        self._record_window_response(record)
        self._update('uncertain' if record.get('interrupted') or info['cost_unknown'] or not identity or violation else 'complete',
                     tokens=tokens, bound_violation=violation)
        return 'capacity_token_bound_violated' if violation else None
