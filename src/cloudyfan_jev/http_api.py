"""Private synchronous HTTP task API, intentionally not a chat protocol server.

No sockets or servers are started here. Authentication, registered validators,
backend enforcement and retention approval belong to the embedding application.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from http import HTTPStatus

from .budget import BudgetedCore, BudgetError
from .client import _strict_json
from .diagnostics import DiagnosticContract
from .privacy import ExecutionPolicy, PolicyDenied


class ServiceError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)

    def body(self):
        return {'error': {'code': self.code}}


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _read(path):
    if path.is_symlink():
        raise ServiceError('unsafe_private_path', 403)
    return json.loads(path.read_text())


def _write(path, value):
    if path.is_symlink():
        raise ServiceError('unsafe_private_path', 403)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.state-', delete=False) as stream:
        os.chmod(stream.name, 0o600)
        stream.write(_encode(value))
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)


@contextmanager
def _lock(directory):
    path = directory / '.lock'
    if path.is_symlink():
        raise ServiceError('unsafe_private_path', 403)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceError('task_busy', 409) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass(frozen=True)
class RegisteredContract:
    version: str
    runner: BudgetedCore
    environment_factory: object
    tool_names: tuple[str, ...] = ()
    mode: str = 'cloudyfan'

    def descriptor(self):
        if (not isinstance(self.version, str) or not self.version.strip()
                or not isinstance(self.runner, BudgetedCore)
                or not callable(self.environment_factory)
                or not isinstance(self.runner.core.execution_policy, ExecutionPolicy)
                or not isinstance(self.runner.core.diagnostic_contract, DiagnosticContract)):
            raise ValueError('Registered contract requires controlled runner, explicit privacy and validator')
        core = self.runner.core
        return {**core.configuration_descriptor(),
                'version': self.version, 'mode': self.mode, 'tool_names': list(self.tool_names),
                'model_bounds': {key: asdict(bound) for key, bound in self.runner.model_bounds.items()},
                'tool_bounds': {key: asdict(bound) for key, bound in self.runner.tool_bounds.items()},
                'receipt_root': str(core.client.root.resolve()),
                'budget_store': str(self.runner.store.path.resolve())}


def _provenance(result, frozen):
    """Project task-owned identity references only; never expose capability prose."""
    def text_field(obj, key):
        value = obj.get(key) if isinstance(obj, dict) else None
        return value if isinstance(value, str) else None

    def call_metadata(call):
        classifier = call['tier'] == 'classifier'
        profile = frozen['decision'] if classifier else frozen['profiles'][call['tier']]
        card = profile.get('capability_card', {})
        selection = card.get('score_selection', {})
        evidence = card.get('benchmark_evidence', {})
        configuration = {key: text_field(profile, key) for key in (
            'model', 'model_version', 'effort', 'provider', 'backend',
            'quantization', 'engine', 'execution_location')}
        if classifier:
            configuration['model_version'] = text_field(profile, 'version')
        return {'call_id': call['call_id'], 'actual_model': text_field(call, 'model'),
                'actual_provider': text_field(call, 'provider'),
                'generation_id': text_field(call, 'generation_id'),
                'local_call_id': text_field(call, 'local_call_id'),
                'configuration': configuration,
                'score': {**{key: text_field(selection, key) for key in ('snapshot_id', 'policy_id', 'config_id')},
                          **{key: text_field(evidence, key) for key in ('source', 'source_version', 'metric')}}}

    return {'config_sha256': result['config_sha256'], 'contract_version': frozen['version'],
            'engine_version': frozen['engine_version'], 'routing_version': frozen['routing_version'],
            'attempts': [{'attempt': attempt['attempt'], 'tier': attempt['tier'],
                          'calls': [call_metadata(call) for call in attempt['calls']]}
                         for attempt in result['attempts']],
            'classifier_calls': [call_metadata(call) for call in (result.get('selection') or {}).get('calls', [])]}


class _RegisteredEnvironment:
    def __init__(self, environment, names):
        self.environment, self.names = environment, frozenset(names)
        self.replayable_tools = frozenset(getattr(environment, 'replayable_tools', ())) & self.names

    def tools_schema(self):
        tools = self.environment.tools_schema()
        if any(tool.get('function', {}).get('name') not in self.names for tool in tools):
            raise PolicyDenied('tool', 'not_registered')
        return tools

    def call(self, name, arguments):
        if name not in self.names:
            raise PolicyDenied('tool', 'not_registered')
        return self.environment.call(name, arguments)

    def validate(self, final):
        return self.environment.validate(final)


class TaskService:
    """Registered private jobs; create freezes inputs and execute is explicit.

    Principals come from the trusted authenticator and server allowlist, never JSON input.
    Separating creation and execution lets an outer scheduler queue a frozen job.
    Every execution gets a fresh private workspace for deterministic local replay.
    """
    def __init__(self, root, *, contracts, allowed_principal=None, allowed_principals=None, retention_policy=None):
        self.root = Path(root)
        if allowed_principal is not None and allowed_principals is not None:
            raise ValueError('Supply one server principal allowlist')
        principals = {allowed_principal} if allowed_principals is None else allowed_principals
        if (self.root.is_symlink() or not isinstance(principals, (set, frozenset, tuple, list))
                or not principals or any(not isinstance(p, str) or not p for p in principals)):
            raise ValueError('Private root and explicit principal required')
        self.allowed_principals = frozenset(principals)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.contracts = dict(contracts)
        self.allowed_principal, self.retention_policy = allowed_principal, retention_policy
        for contract in self.contracts.values():
            contract.descriptor()

    def authorize(self, principal):
        if not isinstance(principal, str) or principal not in self.allowed_principals:
            raise ServiceError('unauthorized', 401)

    def internal_id(self, principal, run_id):
        self.authorize(principal)
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', run_id):
            raise ServiceError('invalid_run_id')
        return 'http-' + hashlib.sha256(_encode([principal, run_id])).hexdigest()[:48]

    def _directory(self, principal, run_id):
        directory = self.root / self.internal_id(principal, run_id)
        if directory.is_symlink():
            raise ServiceError('unsafe_private_path', 403)
        return directory

    def _job(self, principal, run_id):
        directory = self._directory(principal, run_id)
        if not (directory / 'job.json').exists():
            raise ServiceError('task_not_found', 404)
        return directory, _read(directory / 'job.json')

    def create(self, principal, payload):
        self.authorize(principal)
        if not isinstance(payload, dict) or set(payload) != {'run_id', 'contract_id', 'prompt'}:
            raise ServiceError('unsupported_task_fields')
        if (not isinstance(payload['prompt'], str) or not payload['prompt'].strip()
                or len(payload['prompt'].encode()) > 32768):
            raise ServiceError('invalid_text_prompt')
        contract = self.contracts.get(payload['contract_id']) if isinstance(payload['contract_id'], str) else None
        if contract is None:
            raise ServiceError('unregistered_contract')
        directory = self._directory(principal, payload['run_id'])
        frozen = json.loads(_encode({'request': payload, 'registered': contract.descriptor()}))
        directory.mkdir(mode=0o700, exist_ok=True)
        with _lock(directory):
            path = directory / 'job.json'
            if path.exists():
                if _read(path) != frozen:
                    raise ServiceError('frozen_task_changed', 409)
            else:
                _write(path, frozen)
                _write(directory / 'state.json', {'run_id': payload['run_id'], 'status': 'ready'})
        return self.status(principal, payload['run_id'])

    def _costs(self, principal, run_id, job, *, refresh=False):
        contract = self.contracts.get(job['request']['contract_id'])
        if contract is None:
            return {'complete_api_usd': None, 'known_api_usd': None, 'unknown_events': None,
                    'host_and_human_usd': None}
        runner = contract.runner
        if refresh:
            runner.sync_client(runner.client)
            if runner.local_backend:
                runner.sync_client(runner.local_backend)
        internal = self.internal_id(principal, run_id)
        events = [event for event in runner.store.report()['events'] if event['run'] == internal]
        known = sum((Decimal(event['cost']) for event in events if event['cost'] is not None), Decimal(0))
        unknown = sum(event['cost'] is None for event in events)
        return {'complete_api_usd': None if unknown else str(known), 'known_api_usd': str(known),
                'unknown_events': unknown, 'host_and_human_usd': None}

    def status(self, principal, run_id):
        directory, job = self._job(principal, run_id)
        return {**_read(directory / 'state.json'), 'costs': self._costs(principal, run_id, job)}

    def result(self, principal, run_id):
        directory, job = self._job(principal, run_id)
        if not (directory / 'outcome.json').exists():
            raise ServiceError('result_not_available', 409)
        contract = self.contracts.get(job['request']['contract_id'])
        if contract is None or _encode(contract.descriptor()) != _encode(job['registered']):
            raise ServiceError('frozen_configuration_changed', 409)
        try:
            result = contract.runner.core.read_completed(self.internal_id(principal, run_id))
        except Exception:
            raise ServiceError('result_evidence_unavailable', 503) from None
        return {**_read(directory / 'outcome.json'),
                'provenance': _provenance(result, job['registered']),
                'costs': self._costs(principal, run_id, job, refresh=True)}

    def execute(self, principal, run_id):
        directory, job = self._job(principal, run_id)
        with _lock(directory):
            contract = self.contracts.get(job['request']['contract_id'])
            if contract is None or _encode(contract.descriptor()) != _encode(job['registered']):
                raise ServiceError('frozen_configuration_changed', 409)
            if (directory / 'outcome.json').exists():
                return self.status(principal, run_id)
            _write(directory / 'state.json', {'run_id': run_id, 'status': 'running'})
            def environment():
                workspace = Path(tempfile.mkdtemp(prefix='workspace-', dir=directory))
                return _RegisteredEnvironment(contract.environment_factory(workspace), contract.tool_names)
            try:
                registered = contract.runner
                runner = BudgetedCore(registered.core, registered.store,
                    model_bounds=registered.model_bounds, verify_bound=registered.verify_bound,
                    tool_bounds={name: bound for name, bound in registered.tool_bounds.items()
                                 if name in contract.tool_names})
                result = runner.run(self.internal_id(principal, run_id),
                    {'prompt': job['request']['prompt']}, environment,
                    contract_version=contract.version, mode=contract.mode)
                state = {'run_id': run_id, 'status': result['status']}
                _write(directory / 'outcome.json', {**state,
                    'answer': result['attempts'][-1]['final'],
                    'first_public_pass': result['first_public_pass'],
                    'final_public_pass': result['final_public_pass'],
                    'costs': {'complete_api_usd': result['budget']['run_total_usd'],
                              'known_api_usd': result['budget']['run_known_usd'],
                              'unknown_events': result['budget']['run_unknown_events'],
                              'model_and_classifier': result['costs']},
                    'wall_seconds': result['wall_seconds']})
            except PolicyDenied as error:
                state = {'run_id': run_id, 'status': 'policy_denied', 'error': error.body()['error']}
            except BudgetError:
                unknown = contract.runner.store.report()['unknown_events']
                state = {'run_id': run_id, 'status': 'accounting_interrupted' if unknown else 'policy_denied',
                         'error': {'code': 'unknown_cost' if unknown else 'budget_admission_denied'}}
            except Exception:
                unknown = contract.runner.store.report()['unknown_events']
                state = {'run_id': run_id, 'status': 'accounting_interrupted' if unknown else 'engineering_interrupted',
                         'error': {'code': 'unknown_cost' if unknown else 'execution_interrupted'}}
            _write(directory / 'state.json', state)
            return self.status(principal, run_id)


class HTTPApp:
    """WSGI adapter. The injected authenticator must return a trusted principal."""
    def __init__(self, service, *, authenticate, audit=None):
        self.service, self.authenticate, self.audit = service, authenticate, audit

    def __call__(self, environ, start_response):
        code = 200
        try:
            principal = self.authenticate(environ)
            self.service.authorize(principal)
            method, path = environ['REQUEST_METHOD'], environ.get('PATH_INFO', '')
            match = re.fullmatch(r'/v1/tasks/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(/result|/resume)?', path)
            if method == 'POST' and (path == '/v1/tasks' or (match and match[2] == '/resume')):
                if environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
                    raise ServiceError('json_required', 415)
                try:
                    length = int(environ.get('CONTENT_LENGTH') or 0)
                except ValueError:
                    raise ServiceError('invalid_body_length', 413) from None
                if length < 1 or length > 65536:
                    raise ServiceError('invalid_body_length', 413)
                try:
                    payload = _strict_json(environ['wsgi.input'].read(length))
                except (ValueError, UnicodeError):
                    raise ServiceError('invalid_json') from None
                if path == '/v1/tasks':
                    self.service.create(principal, payload)
                    body = self.service.execute(principal, payload['run_id'])
                else:
                    if payload != {}:
                        raise ServiceError('resume_does_not_accept_configuration')
                    body = self.service.execute(principal, match[1])
            elif method == 'GET' and match and match[2] != '/resume':
                body = (self.service.result(principal, match[1]) if match[2] == '/result'
                        else self.service.status(principal, match[1]))
            else:
                raise ServiceError('endpoint_not_supported', 404)
            code = {'policy_denied': 403, 'accounting_interrupted': 409,
                    'engineering_interrupted': 503}.get(body.get('status'), 200)
        except ServiceError as error:
            code, body = error.status, error.body()
        except Exception:
            code, body = 500, {'error': {'code': 'service_interrupted'}}
        if self.audit:
            try:
                self.audit({'event': 'http_task_request', 'http_status': code})
            except Exception:
                pass
        encoded = _encode(body)
        start_response(f'{code} {HTTPStatus(code).phrase}',
                       [('Content-Type', 'application/json'), ('Content-Length', str(len(encoded))),
                        ('Cache-Control', 'no-store')])
        return [encoded]
