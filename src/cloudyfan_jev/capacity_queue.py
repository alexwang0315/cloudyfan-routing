"""Opt-in host contract for pure preparation and deterministic local continuation.

Registration is an explicit host responsibility, never a proof of arbitrary
Python purity, environment qualification or supplier capacity. Unsupported
external effects are refused before acceptance instead of silently replayed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
import tempfile

from .context_preparation import task_prompt
from .engine import Core

_SUBMITTING = ContextVar('capacity_queue_submitting', default=None)
_EXECUTING = ContextVar('capacity_queue_executing', default=None)


class QueueWaiting(RuntimeError):
    """Never-dispatched next boundary; not a denied/paid transport response."""
    code = 'queue_waiting_worker'


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


class QueuePreparation:
    protocol = 'capacity-queue-v1'

    def __init__(self, version, *, contracts, tools, replayable_tools=None):
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', version):
            raise ValueError('Explicit bounded preparation version required')
        if not contracts or set(tools) != set(contracts):
            raise ValueError('Every queue contract requires a host-frozen tool schema')
        self.version, self.contracts = version, dict(contracts)
        self.tools, self.replayable = deepcopy(tools), deepcopy(replayable_tools or {})
        frozen = {}
        from .scores.backup_runtime import BackupCategoryCore
        from .scores.category_routing import CategoryCore
        for name, contract in self.contracts.items():
            if type(contract.runner.core.queue_template()) not in {Core, BackupCategoryCore, CategoryCore}:
                raise ValueError('A supported original Core preparation protocol is required')
            schema = self.tools[name]
            names = [tool.get('function', {}).get('name') for tool in schema]
            replayable = self.replayable.get(name, [])
            if (not isinstance(schema, list) or len(encoded(schema)) > 32768
                    or any(not isinstance(tool, str) or tool not in contract.tool_names for tool in names)
                    or len(names) != len(set(names)) or set(replayable) != set(names)
                    or any(tool not in contract.runner.tool_bounds or contract.runner.tool_bounds[tool].external for tool in names)):
                raise ValueError('Only explicitly replayable deterministic local tools can queue')
            frozen[name] = {'registered': contract.descriptor(), 'tools': schema,
                            'replayable_tools': sorted(replayable)}
        self._descriptor = {'protocol': self.protocol, 'version': version, 'contracts': frozen}
        self.sha256 = hashlib.sha256(encoded(self._descriptor)).hexdigest()

    def descriptor(self):
        return deepcopy(self._descriptor)

    def verify_contract(self, name, contract):
        if (name not in self.contracts
                or encoded(contract.descriptor()) != encoded(self._descriptor['contracts'][name]['registered'])):
            from .http_api import ServiceError
            raise ServiceError('queue_preparation_contract_changed', 409)

    @contextmanager
    def accepting(self, scheduler):
        token = _SUBMITTING.set(scheduler)
        try:
            yield
        finally:
            _SUBMITTING.reset(token)

    @staticmethod
    def is_accepting(scheduler):
        return _SUBMITTING.get() is scheduler

    @staticmethod
    def execution():
        return _EXECUTING.get()

    def _transport(self, runner, next_request):
        from .capacity_dispatch import prepare_request, require_model_transport
        transport = runner.local_backend if next_request['local'] else runner.client
        require_model_transport(transport)
        payload = json.loads(next_request['payload_json'])
        if payload != next_request['payload']:
            raise ValueError('Prepared payload and ordered serialization differ')
        if next_request['local']:
            if 'prepare_payload' in vars(transport):
                raise ValueError('Overridden local preparation is unsupported')
            wire = prepare_request(transport.http, transport.config['endpoint'], transport.prepare_payload(payload))
        else:
            wire = prepare_request(transport.http, next_request['endpoint'], payload,
                headers={'Authorization': 'Bearer ' + transport.key, 'X-OpenRouter-Metadata': 'enabled'})
        if hashlib.sha256(wire.content).hexdigest() != next_request['wire_sha256']:
            from .http_api import ServiceError
            raise ServiceError('queue_prepared_request_changed', 409)
        return transport, wire

    def prepare_next(self, service, row):
        from .capacity_dispatch import prepare_request
        from .http_api import _read, ServiceError
        directory, job = service._job(row['principal'], row['run_id'])
        contract_id = job['request']['contract_id']
        contract = service._registered_contract(job)
        self.verify_contract(contract_id, contract)
        core, task = contract.runner.core.queue_binding(row['id'], {'prompt': task_prompt(job)})
        pointer = directory / 'queue-next.json'
        if pointer.exists():
            reference = _read(pointer)
            next_request = _read(directory / 'queue-boundaries' / (reference['sha256'] + '.json'))
            if hashlib.sha256(encoded(next_request)).hexdigest() != reference['sha256']:
                raise ServiceError('queue_continuation_changed', 409)
        else:
            adoption = service.capacity_controller.context(row['id'], row['principal'], job['registered'])
            def check_window(profile):
                transport = contract.runner.local_backend if profile.get('backend') == 'local' else contract.runner.client
                return service.capacity_controller.candidate_window(adoption, transport, profile, job['registered'])
            initial = core.queue_initial(row['id'], task, mode=contract.mode,
                tools=self.tools[contract_id], candidate_window=check_window)
            endpoint = (contract.runner.local_backend.config['endpoint'] if initial['local'] else
                        'https://openrouter.ai/api/v1/chat/completions')
            transport = contract.runner.local_backend if initial['local'] else contract.runner.client
            payload = transport.prepare_payload(initial['payload']) if initial['local'] else initial['payload']
            wire = prepare_request(transport.http, endpoint, payload,
                headers=None if initial['local'] else {'Authorization': 'Bearer ' + transport.key,
                                                     'X-OpenRouter-Metadata': 'enabled'})
            next_request = {'protocol': self.protocol, 'version': self.version, 'preparation_sha256': self.sha256,
                'run': row['id'], 'contract_id': contract_id, 'registered_sha256': hashlib.sha256(encoded(job['registered'])).hexdigest(),
                **initial, 'endpoint': endpoint, 'wire_sha256': hashlib.sha256(wire.content).hexdigest()}
            next_request['payload_json'] = json.dumps(initial['payload'], ensure_ascii=False, allow_nan=False)
        if (next_request['run'] != row['id'] or next_request['contract_id'] != contract_id
                or next_request['version'] != self.version or next_request['preparation_sha256'] != self.sha256
                or next_request['registered_sha256'] != hashlib.sha256(encoded(job['registered'])).hexdigest()):
            raise ServiceError('queue_continuation_changed', 409)
        core.queue_validate(next_request)
        transport, wire = self._transport(contract.runner, next_request)
        adoption = service.capacity_controller.context(row['id'], row['principal'], job['registered'])
        if adoption.get('queue_version') != self.version or adoption.get('queue_sha256') != self.sha256:
            raise ServiceError('queue_frozen_policy_changed', 409)
        dispatch = service.capacity_controller.dispatch(adoption, transport, next_request['endpoint'],
            next_request['payload'], job['registered'])
        snapshot = service.capacity_controller.store.snapshot(adoption['configuration_version'])
        _, groups = service.capacity_controller.window_scope(adoption, dispatch.route, snapshot, next_request['wire_sha256'])
        return {'request': next_request, 'transport': transport, 'wire': wire, 'dispatch': dispatch,
                'groups': {group['physical'] for group in groups}, 'contract': contract}

    def accept(self, service, row):
        directory, job = service._job(row['principal'], row['run_id'])
        contract = service._registered_contract(job)
        # The host explicitly registered this environment's pure preparation
        # and deterministic local replay contract. Check its actual schema
        # before accepting work; registration is not proof of Python purity.
        workspace = Path(tempfile.mkdtemp(prefix='queue-preparation-', dir=directory))
        self.verify_environment(job['request']['contract_id'], contract.environment_factory(workspace))
        core, task = contract.runner.core.queue_binding(row['id'], {'prompt': task_prompt(job)})
        core.queue_accept(row['id'], row['created'])

    def save_boundary(self, service, row, next_request):
        from .http_api import _write
        from .receipt_store import _immutable
        root = service.root / row['id']
        boundaries = root / 'queue-boundaries'
        boundaries.mkdir(mode=0o700, exist_ok=True)
        fingerprint = hashlib.sha256(encoded(next_request)).hexdigest()
        path = boundaries / (fingerprint + '.json')
        if path.exists():
            if json.loads(path.read_text()) != next_request:
                raise ValueError('Immutable queue boundary changed')
        else:
            _immutable(path, next_request)
        _write(root / 'queue-next.json', {'protocol': self.protocol, 'sha256': fingerprint})

    @contextmanager
    def running(self, scheduler, row, prepared, claim):
        execution = {'scheduler': scheduler, 'controller': scheduler.service.capacity_controller,
            'run': row['id'], 'row': row, 'prepared': prepared, 'claim': claim, 'consumed': False}
        token = _EXECUTING.set(execution)
        try:
            yield execution
        finally:
            _EXECUTING.reset(token)

    def consume(self, owner, transport, cid, endpoint, payload, wire, *, gate=None):
        from .capacity_dispatch import CapacityDenied
        execution = self.execution()
        if execution is None or execution['controller'] is not owner.capacity_controller or execution['run'] != owner._capacity_context['run']:
            raise CapacityDenied('capacity_queue_claim_required', 403)
        scheduler, row = execution['scheduler'], execution['row']
        scheduler.verify_claim(row['id'], execution['claim'])
        request = execution['prepared']['request']
        actual = {**request, 'call_id': cid, 'endpoint': endpoint, 'payload': deepcopy(payload),
                  'payload_json': json.dumps(payload, ensure_ascii=False, allow_nan=False),
                  'local': hasattr(transport, 'config'), 'wire_sha256': hashlib.sha256(wire.content).hexdigest()}
        actual.pop('gate', None)
        if gate is not None:
            actual['gate'] = deepcopy(gate)
        if execution['consumed']:
            self.save_boundary(scheduler.service, row, actual)
            raise QueueWaiting()
        if actual != request or execution['prepared']['transport'] is not transport:
            from .receipt_store import _immutable
            _immutable(scheduler.service.root / row['id'] / ('queue-refusal-' + execution['claim'] + '.json'),
                {'protocol': self.protocol, 'expected': request, 'actual': actual})
            raise CapacityDenied('capacity_queue_request_changed', 409)
        dispatch = execution['prepared']['dispatch']
        if dispatch.id is None:
            raise CapacityDenied('capacity_queue_claim_required', 403)
        execution['consumed'] = True
        return dispatch, {'protocol': 'httpx-prepared-json-v1', 'wire_sha256': request['wire_sha256'],
            'scope_sha256': execution['evidence']['scope_sha256'], 'capacity_allocation': dispatch.id}

    def ordered_payload(self, cid, endpoint, payload):
        """Retain only ordering from the original never-dispatched boundary.

        Engine replay can read canonical sorted checkpoints. Values, canonical
        intent and identity must still match; this never substitutes new content.
        The real HTTPx builder below serializes these exact frozen ordered bytes.
        """
        execution = self.execution()
        if execution is None or execution['consumed']:
            return payload
        request = execution['prepared']['request']
        if cid != request['call_id'] or endpoint != request['endpoint'] or payload != request['payload']:
            return payload
        return json.loads(request['payload_json'])

    def verify_environment(self, contract_id, environment):
        tools = environment.tools_schema()
        if encoded(tools) != encoded(self.tools[contract_id]) or set(getattr(environment, 'replayable_tools', ())) != set(self.replayable.get(contract_id, [])):
            from .http_api import ServiceError
            raise ServiceError('queue_environment_contract_changed', 409)
