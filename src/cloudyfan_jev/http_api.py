"""Private synchronous HTTP task API, intentionally not a chat protocol server.

No sockets or servers are started here. Authentication, registered validators,
backend enforcement and retention approval belong to the embedding application.
"""
from contextlib import contextmanager, ExitStack
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
from functools import wraps
import time

from .budget import BudgetedCore, BudgetError
from .client import _strict_json
from .diagnostics import DiagnosticContract
from .privacy import ExecutionPolicy, PolicyDenied
from .retention import (RetentionError, RetentionPolicy, RetentionStore, digest,
                        inventory, open_file_paths, maintenance_lock, purge_exact)


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
                'budget_store': str(self.runner.store.path.resolve()),
                **({'enforcement': self.runner.enforcement_descriptor()}
                   if self.runner.enforcement_descriptor() is not None else {})}


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

    def capacity_tool(self, name, arguments):
        if name not in self.names:
            raise PolicyDenied('tool', 'not_registered')
        prepare = getattr(self.environment, 'capacity_tool', None)
        if not callable(prepare):
            from .capacity_dispatch import CapacityDenied
            raise CapacityDenied('capacity_tool_protocol_unavailable')
        return prepare(name, arguments)

    def validate(self, final):
        return self.environment.validate(final)


def _retained(method):
    @wraps(method)
    def wrapped(self, principal, *args, **kwargs):
        if method.__name__ == 'create':
            if not args or not isinstance(args[0], dict) or 'run_id' not in args[0]:
                return method(self, principal, *args, **kwargs)
            run_id = args[0]['run_id']
        else:
            run_id = args[0]
        internal = self.internal_id(principal, run_id)
        if self.retention is None:
            return method(self, principal, *args, **kwargs)
        queue = getattr(self, 'queue_scheduler', None)
        if (queue is not None and queue.capacity_protocol is not None
                and (method.__name__ == 'observe' or method.__name__ in {'status', 'result'}
                     and queue.contains(principal, run_id))):
            # A detached queue may predate a newly visible host binding. Host
            # observation still uses the original read-only access policy; it
            # cannot acquire a retention writer's guard or initialize metadata.
            self.queue_access(principal, run_id)
            return method(self, principal, *args, **kwargs)
        try:
            with self.retention.guard(internal):
                self.authorize(principal)
                self.retention.access(internal, create=False,
                    legacy_exists=(self.root / internal / 'job.json').exists())
                return method(self, principal, *args, **kwargs)
        except RetentionError as error:
            raise ServiceError(error.code, error.status) from None
    return wrapped


class TaskService:
    """Registered private jobs; create freezes inputs and execute is explicit.

    Principals come from the trusted authenticator and server allowlist, never JSON input.
    Separating creation and execution lets an outer scheduler queue a frozen job.
    Every execution gets a fresh private workspace for deterministic local replay.
    """
    @property
    def capacity_controller(self):
        return self._capacity_controller

    @capacity_controller.setter
    def capacity_controller(self, controller):
        self._capacity_controller = controller
        if controller is not None and hasattr(self, 'root'):
            controller.store.integrity.bind(self.root, 'capacity')

    def __init__(self, root, *, contracts, allowed_principal=None, allowed_principals=None, retention_policy=None,
                 principal_active=None, clock=time.time, retention_protected_paths=(), capacity_controller=None):
        self.capacity_controller = capacity_controller
        self.queue_scheduler = None
        self.root = Path(root)
        if allowed_principal is not None and allowed_principals is not None:
            raise ValueError('Supply one server principal allowlist')
        principals = {allowed_principal} if allowed_principals is None else allowed_principals
        if (self.root.is_symlink() or not isinstance(principals, (set, frozenset, tuple, list))
                or not principals or any(not isinstance(p, str) or not p for p in principals)):
            raise ValueError('Private root and explicit principal required')
        if principal_active is not None and not callable(principal_active):
            raise ValueError('Principal activity callback must be callable')
        if retention_policy is not None and not isinstance(retention_policy, RetentionPolicy):
            raise ValueError('Retention requires an explicit versioned policy')
        if retention_policy is None and (self.root / '.retention').exists():
            raise ValueError('Existing retention metadata requires an explicit policy')
        self.retention_protected_paths = tuple(Path(p).resolve() for p in retention_protected_paths)
        self.principal_active = principal_active
        self.allowed_principals = frozenset(principals)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if capacity_controller is not None:
            capacity_controller.store.integrity.bind(self.root, 'capacity')
        self.contracts = dict(contracts)
        self.allowed_principal, self.retention_policy = allowed_principal, retention_policy
        self.retention = (RetentionStore(self.root / '.retention', retention_policy, clock=clock)
                          if retention_policy is not None else None)
        self.retention_guard = self.retention_erase_queue = self.retention_maintenance = None
        self.retention_open_files = open_file_paths
        for contract in self.contracts.values():
            contract.descriptor()

    def authorize(self, principal):
        if not isinstance(principal, str) or principal not in self.allowed_principals:
            raise ServiceError('unauthorized', 401)
        try:
            active = self.principal_active is None or self.principal_active(principal) is True
        except Exception:
            raise ServiceError('principal_authorization_unavailable', 503) from None
        if not active:
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

    @_retained
    def create(self, principal, payload):
        self.authorize(principal)
        if self.capacity_controller is not None and self.capacity_controller.queue_policy() is not None:
            from .capacity_queue import QueuePreparation
            if self.queue_scheduler is None:
                raise ServiceError('capacity_queue_backend_unavailable', 503)
            if not QueuePreparation.is_accepting(self.queue_scheduler):
                return self.queue_scheduler.submit(principal, payload)
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
        if self.retention is not None:
            self.retention.access(self.internal_id(principal, payload['run_id']), create=True,
                                  legacy_exists=(directory / 'job.json').exists())
        directory.mkdir(mode=0o700, exist_ok=True)
        with _lock(directory):
            path = directory / 'job.json'
            if path.exists():
                if _read(path) != frozen:
                    raise ServiceError('frozen_task_changed', 409)
            else:
                if self.capacity_controller is not None:
                    try:
                        self.capacity_controller.adopt(self.internal_id(principal, payload['run_id']), principal, frozen['registered'])
                    except Exception as error:
                        from .capacity_dispatch import CapacityDenied
                        if isinstance(error, CapacityDenied):
                            raise ServiceError(error.code, error.status) from None
                        raise
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

    @_retained
    def status(self, principal, run_id):
        if self.queue_scheduler is not None and self.queue_scheduler.contains(principal, run_id):
            return self.queue_scheduler.status(principal, run_id)
        return self.queue_task_status(principal, run_id)

    @_retained
    def queue_task_status(self, principal, run_id):
        """Original task-owned fields for the controlled queue projection."""
        return self._task_status(principal, run_id)

    def queue_access(self, principal, run_id):
        """Read-only original access policy; polling never expires or renews it."""
        import math
        self.authorize(principal)
        if self.retention is not None:
            internal = self.internal_id(principal, run_id)
            lifecycle = self.retention.lookup(internal)
            if lifecycle is None:
                raise ServiceError('retention_metadata_missing', 409)
            now = self.retention.clock()
            if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
                raise ServiceError('retention_clock_invalid', 503)
            import sqlite3
            with sqlite3.connect(self.retention.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                floor = db.execute('SELECT observed FROM clock WHERE singleton=1').fetchone()
            if floor is not None and now < floor[0]:
                raise ServiceError('retention_clock_rollback', 503)
            if lifecycle['policy'] != self.retention_policy.descriptor():
                raise ServiceError('retention_policy_changed', 409)
            if lifecycle['state'] == 'retired':
                raise ServiceError('task_retired', 410)
            if lifecycle['state'] != 'active' or now >= lifecycle['expires_at']:
                raise ServiceError('task_expired', 410)

    def queue_observe(self, principal, run_id):
        self.queue_access(principal, run_id)
        return self._task_status(principal, run_id)

    def _task_status(self, principal, run_id):
        directory, job = self._job(principal, run_id)
        result = {**_read(directory / 'state.json'), 'costs': self._costs(principal, run_id, job)}
        if self.capacity_controller is not None:
            try:
                result['capacity'] = self.capacity_controller.own(self.internal_id(principal, run_id))
            except (OSError, ValueError, RuntimeError):
                result['capacity'] = {'status': 'unavailable', 'reason_code': 'capacity_store_integrity_unavailable'}
        return result

    def _registered_contract(self, job):
        contract = self.contracts.get(job['request']['contract_id'])
        if contract is None or _encode(contract.descriptor()) != _encode(job['registered']):
            raise ServiceError('frozen_configuration_changed', 409)
        return contract

    def _verified_completed(self, principal, run_id, directory, job):
        if not (directory / 'outcome.json').exists():
            raise ServiceError('result_not_available', 409)
        contract = self._registered_contract(job)
        try:
            return contract.runner.core.read_completed(self.internal_id(principal, run_id))
        except Exception:
            raise ServiceError('result_evidence_unavailable', 503) from None

    @_retained
    def observe(self, principal, run_id):
        """Host-only read projection: one completed-evidence validation and budget report.

        Raw completed_result is for a trusted host's public projection, not a new
        HTTP response. No execution, receipt reconciliation or shared sync occurs.
        """
        directory, job = self._job(principal, run_id)
        state = _read(directory / 'state.json')
        completed = None
        if state['status'] in ('passed', 'failed', 'unverifiable'):
            completed = self._verified_completed(principal, run_id, directory, job)
        else:
            self._registered_contract(job)
        return {'state': {**state, 'costs': self._costs(principal, run_id, job)},
                'completed_result': completed}

    @_retained
    def result(self, principal, run_id, *, refresh=True):
        if type(refresh) is not bool:
            raise ValueError('Result refresh must be bool')
        directory, job = self._job(principal, run_id)
        result = self._verified_completed(principal, run_id, directory, job)
        return {**_read(directory / 'outcome.json'),
                'provenance': _provenance(result, job['registered']),
                'costs': self._costs(principal, run_id, job, refresh=refresh)}

    @_retained
    def execute(self, principal, run_id):
        directory, job = self._job(principal, run_id)
        internal = self.internal_id(principal, run_id)
        if self.capacity_controller is not None and self.capacity_controller.queue_policy(internal) is not None:
            from .capacity_queue import QueuePreparation
            execution = QueuePreparation.execution()
            if execution is None or execution.get('scheduler') is not self.queue_scheduler or execution.get('run') != internal:
                if self.queue_scheduler is None:
                    raise ServiceError('capacity_queue_backend_unavailable', 503)
                return self.queue_scheduler.status(principal, run_id)
        with _lock(directory):
            contract = self.contracts.get(job['request']['contract_id'])
            if contract is None or _encode(contract.descriptor()) != _encode(job['registered']):
                raise ServiceError('frozen_configuration_changed', 409)
            if ((directory / 'outcome.json').exists()
                    or _read(directory / 'state.json').get('status') == 'capacity_denied'):
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
                                 if name in contract.tool_names}, capacity_controller=self.capacity_controller)
                context = (self.capacity_controller.context(internal, principal, job['registered'])
                           if self.capacity_controller is not None else None)
                result = runner.run(internal,
                    {'prompt': job['request']['prompt']}, environment,
                    contract_version=contract.version, mode=contract.mode,
                    **({"capacity_context": context, "capacity_descriptor": job["registered"]}
                       if self.capacity_controller is not None else {}))
                if self.capacity_controller is not None:
                    self.capacity_controller.terminal(internal)
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
            except Exception as error:
                from .capacity_dispatch import CapacityDenied
                if isinstance(error, CapacityDenied):
                    # This protocol makes capacity refusal terminal. Release only
                    # its unused monetary workflow reserve; actual/provisional
                    # fees and the independent capacity ledger retain authority.
                    try:
                        contract.runner.store.finish(internal)
                    except BudgetError:
                        state = {'run_id': run_id, 'status': 'accounting_interrupted', 'error': {'code': 'unknown_cost'}}
                        _write(directory / 'state.json', state)
                        return self.queue_task_status(principal, run_id)
                    state = {'run_id': run_id, 'status': 'capacity_denied', 'error': {'code': error.code}}
                    self.capacity_controller.refused(internal, error.code)
                    self.capacity_controller.terminal(internal)
                    _write(directory / 'state.json', state)
                    return self.queue_task_status(principal, run_id)
                from .capacity_queue import QueueWaiting
                if isinstance(error, QueueWaiting):
                    state = {'run_id': run_id, 'status': 'queued'}
                    _write(directory / 'state.json', state)
                    return self.queue_task_status(principal, run_id)
                unknown = contract.runner.store.report()['unknown_events']
                state = {'run_id': run_id, 'status': 'accounting_interrupted' if unknown else 'engineering_interrupted',
                         'error': {'code': 'unknown_cost' if unknown else 'execution_interrupted'}}
            _write(directory / 'state.json', state)
            return self.queue_task_status(principal, run_id)


    def mark_queued(self, principal, run_id):
        internal = self.internal_id(principal, run_id)
        if self.retention is not None:
            with self.retention.guard(internal):
                self.retention.access(internal)
                self.retention.mark_queued(internal)

    def retention_preview(self, principal, run_id):
        """Operator-only exact inventory. This method never deletes or dispatches."""
        internal = self.internal_id(principal, run_id)
        if self.retention is None:
            raise ServiceError('retention_not_configured', 409)
        try:
            with self.retention.guard(internal):
                return self._retention_preview(internal)
        except RetentionError as error:
            raise ServiceError(error.code, error.status) from None

    def _retention_preview(self, internal):
        lifecycle = self.retention.lookup(internal)
        blockers, targets, events = [], [], []
        never_dispatched, retirement_marker = False, None
        if lifecycle is None:
            raise RetentionError('retention_metadata_missing', 409)
        if lifecycle['policy'] != self.retention_policy.descriptor():
            blockers.append('retention_policy_changed')
        if lifecycle['state'] == 'active' and self.retention._now() < lifecycle['expires_at']:
            blockers.append('retention_not_due')
        directory = self.root / internal
        queue = None
        if lifecycle['queued']:
            if not callable(self.retention_guard) or not callable(self.retention_erase_queue):
                blockers.append('queue_retention_unavailable')
            else:
                try:
                    observed = self.retention_guard(internal)
                    keys = {'terminal', 'request_erased', 'request_sha256', 'bytes', 'evidence_ref'}
                    queue = {key: observed.get(key) for key in keys} if isinstance(observed, dict) else None
                    if (not isinstance(queue, dict) or set(observed) != keys or queue.get('terminal') is not True
                            or type(queue.get('request_erased')) is not bool
                            or not isinstance(queue.get('request_sha256'), str)
                            or not re.fullmatch(r'[a-f0-9]{64}', queue['request_sha256'])
                            or type(queue.get('bytes')) is not int or queue['bytes'] < 0
                            or (queue['request_erased'] and queue['bytes'] != 0)
                            or (not queue['request_erased'] and queue['bytes'] == 0)
                            or not isinstance(queue.get('evidence_ref'), str)
                            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._/@-]{0,255}', queue['evidence_ref'])):
                        blockers.append('queue_not_terminal_or_unverified')
                except Exception:
                    queue = None
                    blockers.append('queue_retention_unavailable')
        try:
            job = _read(directory / 'job.json')
            state = _read(directory / 'state.json')
            contract = self.contracts.get(job['request']['contract_id'])
            if contract is None or _encode(contract.descriptor()) != _encode(job['registered']):
                raise ValueError('contract changed')
            runner = contract.runner
            report = runner.store.report()
            reservation = [r for r in report['reservations'] if r['run'] == internal]
            events = [e for e in report['events'] if e['run'] == internal]
            core_run = runner.client.root / 'runs' / internal
            retirement_marker = {'path': str((core_run / '.retired.json').absolute()),
                                 'root': str((runner.client.root / 'runs').absolute())}
            if report['unknown_events'] or report['violations'] or report['unbounded_history_events']:
                blockers.append('unresolved_shared_accounting')
            paths = [(directory, self.root)]
            clients = [runner.client] + ([runner.local_backend] if runner.local_backend is not None else [])
            call_events = {}
            from .client import usage
            for client in clients:
                calls_root = client.root / 'calls'
                for call in sorted(calls_root.glob('run-' + internal + '-*')):
                    paths.append((call, calls_root))
                    event_id = 'model:' + digest(str(client.root.resolve())) + ':' + call.name
                    try:
                        info = usage(client._load(call))
                    except (OSError, ValueError, TypeError):
                        blockers.append('receipt_inventory_mismatch')
                        call_events[event_id] = (None, None)
                        continue
                    receipt = info.get('local_call_id') or info.get('generation_id')
                    if info.get('local_call_id'):
                        receipt = 'local:' + digest(str(client.root.resolve())) + ':' + receipt
                    call_events[event_id] = (receipt, info['cost_usd'])
            expected = {e['id']: (e['receipt'], e['cost']) for e in events if e['kind'] == 'model'}
            if (set(call_events) != set(expected) or any(
                    actual[0] != expected[key][0] or actual[1] is None or expected[key][1] is None
                    or Decimal(actual[1]) != Decimal(expected[key][1])
                    for key, actual in call_events.items() if key in expected)):
                blockers.append('receipt_inventory_mismatch')
            unstarted = state.get('status') == 'ready' and not reservation and not events
            artifacts = core_run.exists() or core_run.is_symlink() or len(paths) > 1
            never_dispatched = unstarted and not artifacts
            if never_dispatched:
                # Absence is zero-fee evidence only in these controlled, safe roots.
                namespaces = [core_run.parent, *(client.root / 'calls' for client in clients)]
                if any(path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
                       for path in namespaces):
                    blockers.append('unsafe_never_dispatched_namespace')
                    never_dispatched = False
                if any(not (client.root / 'calls').is_dir() for client in clients):
                    blockers.append('unsafe_never_dispatched_namespace')
                    never_dispatched = False
            else:
                paths.insert(1, (core_run, core_run.parent))
                if unstarted and artifacts:
                    blockers.append('never_dispatched_evidence_conflict')
                if state.get('status') not in {'passed', 'failed', 'unverifiable'}:
                    blockers.append('task_not_completed')
                if len(reservation) != 1 or reservation[0]['status'] != 'complete':
                    blockers.append('reservation_not_complete')
                if not events or any(e['cost'] is None for e in events):
                    blockers.append('unknown_cost')
                if any(not e['verified'] or not e['receipt'] for e in events):
                    blockers.append('unverified_cost')
            protected = [runner.store.path.absolute(), self.retention.path.absolute(),
                         *self.retention_protected_paths]
            # A permanent Core sentinel may be created for a never-run task, but
            # neither that creation nor any deletion may touch protected storage.
            for target, _ in [*paths, (core_run, core_run.parent)]:
                if any(p == target.absolute() or target.absolute() in p.parents
                       or p in target.absolute().parents for p in protected):
                    blockers.append('protected_database_overlap')
            for target, root in paths:
                targets.append(inventory(target, root, open_files=self.retention_open_files))
            if len({t['path'] for t in targets}) != len(targets):
                blockers.append('overlapping_targets')
            if any(a['path'] != b['path'] and Path(a['path']) in Path(b['path']).parents
                   for a in targets for b in targets):
                blockers.append('overlapping_targets')
            if any(t['status'] != 'closed' for t in targets):
                blockers.append('unsafe_or_open_targets')
        except Exception:
            blockers.append('task_evidence_unavailable')
        billing = [{key: e.get(key) for key in ('id', 'run', 'kind', 'cost', 'verified', 'receipt')}
                   for e in events]
        plan = {'schema_version': 1, 'internal_id': internal, 'lifecycle': lifecycle,
                'targets': targets, 'queue': queue, 'billing': billing,
                'never_dispatched': never_dispatched, 'retirement_marker': retirement_marker,
                'blockers': sorted(set(blockers)), 'eligible': not blockers,
                'maintenance_required': True}
        return {**plan, 'sha256': digest(plan)}


    def retention_apply(self, principal, run_id, preview, *, approval):
        """Explicit exact-plan operator maintenance; never exposed over HTTP."""
        internal = self.internal_id(principal, run_id)
        if self.retention is None:
            raise ServiceError('retention_not_configured', 409)
        if (not isinstance(preview, dict) or preview.get('internal_id') != internal
                or preview.get('sha256') != digest({k: v for k, v in preview.items() if k != 'sha256'})
                or not isinstance(approval, dict) or set(approval) != {'plan_sha256', 'reference'}
                or approval['plan_sha256'] != preview['sha256']
                or not isinstance(approval['reference'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._/@-]{0,255}', approval['reference'])):
            raise ServiceError('retention_approval_required', 403)
        preview, approval = json.loads(_encode(preview)), json.loads(_encode(approval))
        if not callable(self.retention_maintenance):
            raise ServiceError('retention_maintenance_required', 409)
        try:
            with (self.retention.guard(internal),
                  self.retention_maintenance(internal) as maintenance, ExitStack() as locks):
                if (not isinstance(maintenance, dict) or maintenance.get('workers_stopped') is not True
                        or not isinstance(maintenance.get('evidence_ref'), str)
                        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._/@-]{0,255}', maintenance['evidence_ref'])):
                    raise RetentionError('retention_workers_not_stopped', 409)
                before = self._retention_preview(internal)
                if before != preview:
                    raise RetentionError('retention_plan_changed', 409)
                if not before['eligible']:
                    raise RetentionError('retention_plan_ineligible', 409)
                directory = self.root / internal
                job = _read(directory / 'job.json')
                runner = self.contracts[job['request']['contract_id']].runner
                core_run = runner.client.root / 'runs' / internal
                locks.enter_context(maintenance_lock(directory / '.lock'))
                if not before['never_dispatched']:
                    locks.enter_context(maintenance_lock(core_run / '.lock'))
                clients = [runner.client] + ([runner.local_backend] if runner.local_backend else [])
                for client in sorted(clients, key=lambda c: str(c.root)):
                    locks.enter_context(maintenance_lock(client.root / '.client.lock'))
                current = self._retention_preview(internal)
                if current != preview or not current['eligible']:
                    raise RetentionError('retention_plan_changed', 409)
                audit = {'schema_version': 1, 'internal_id': internal,
                         'plan_sha256': preview['sha256'], 'approval_reference': approval['reference'],
                         'billing': current['billing'], 'billing_sha256': digest(current['billing']),
                         'never_dispatched': current['never_dispatched'],
                         'verified_api_usd': str(sum((Decimal(event['cost']) for event in current['billing']), Decimal(0))),
                         'maintenance_reference': maintenance['evidence_ref'],
                         'target_hashes': [{'path_sha256': digest(t['path']), 'sha256': t['sha256']}
                                           for t in current['targets']]}
                # Commit the permanent no-reuse decision before irreversible erasure.
                self.retention.retire(internal, audit)
                from .receipt_store import _immutable, _fsync_dir
                if current['never_dispatched']:
                    # The approved maintenance fence excludes all namespace writers.
                    # Keep the new directory permanently; it is not a purge target.
                    if core_run.exists() or core_run.is_symlink():
                        raise RetentionError('retention_target_changed', 409)
                    core_run.parent.mkdir(mode=0o700, exist_ok=True)
                    _fsync_dir(core_run.parent.parent)
                    core_run.mkdir(mode=0o700)
                    _fsync_dir(core_run.parent)
                    locks.enter_context(maintenance_lock(core_run / '.lock'))
                _immutable(core_run / '.retired.json', {k: audit[k] for k in (
                    'schema_version', 'internal_id', 'plan_sha256', 'approval_reference', 'billing_sha256')})
                if current['lifecycle']['queued'] and not current['queue']['request_erased']:
                    self.retention_erase_queue(internal, current['queue']['request_sha256'],
                                               approval['reference'])
                    erased = self.retention_guard(internal)
                    if (erased.get('terminal') is not True or erased.get('request_erased') is not True
                            or erased.get('request_sha256') != current['queue']['request_sha256']):
                        raise RetentionError('queue_erasure_unconfirmed', 409)
                purge_exact(current['targets'])
                return {'status': 'retired', 'internal_id': internal,
                        'plan_sha256': preview['sha256'], 'billing_sha256': audit['billing_sha256']}
        except RetentionError as error:
            raise ServiceError(error.code, error.status) from None
        except Exception:
            # A persisted tombstone survives partial erasure. No resend or reset.
            raise ServiceError('retention_maintenance_interrupted', 503) from None


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
            match = re.fullmatch(r'/v1/tasks/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(/result|/resume|/cancel)?', path)
            if method == 'POST' and (path == '/v1/tasks' or (match and match[2] in ('/resume', '/cancel'))):
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
                    if match[2] == '/cancel':
                        if self.service.queue_scheduler is None:
                            raise ServiceError('capacity_queue_not_configured', 409)
                        body = self.service.queue_scheduler.cancel(principal, match[1])
                    else:
                        body = self.service.execute(principal, match[1])
            elif method == 'GET' and match and match[2] not in ('/resume', '/cancel'):
                body = (self.service.result(principal, match[1]) if match[2] == '/result'
                        else self.service.status(principal, match[1]))
            else:
                raise ServiceError('endpoint_not_supported', 404)
            code = {'policy_denied': 403, 'accounting_interrupted': 409,
                    'engineering_interrupted': 503, 'queued': 202, 'accepting': 202}.get(body.get('status'), 200)
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
