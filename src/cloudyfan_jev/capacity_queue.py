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

from .engine import Core

_SUBMITTING = ContextVar('capacity_queue_submitting', default=None)
_EXECUTING = ContextVar('capacity_queue_executing', default=None)


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
        for name, contract in self.contracts.items():
            if type(contract.runner.core) is not Core:
                raise ValueError('This preparation version supports original Core only')
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
