"""Offline-capable local benchmark orchestration; no engine/hardware management.

Engine probes are trusted measurement adapters, never a correctness oracle.
Every executed answer goes through the supplied Core public validation contract.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
import re
from time import monotonic

from .engine import _encoded, _lock, _read, _save
from .ledger import summarize


@dataclass(frozen=True)
class ModelSetup:
    model_version: str
    quantization: str
    engine: str
    hardware: dict
    resources: dict

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in
               (self.model_version, self.quantization, self.engine)):
            raise ValueError('Model version, quantization and engine must be explicit')
        if not isinstance(self.hardware, dict) or not self.hardware or not isinstance(self.resources, dict) or not self.resources:
            raise ValueError('Hardware and resource settings must be explicit')
        _encoded(asdict(self))


class _Events:
    def __init__(self, clock):
        self.clock, self.rows = clock, []

    def emit(self, event):
        allowed = {f'{name}_{edge}' for name in ('load', 'queue', 'prompt', 'generation', 'switch')
                   for edge in ('start', 'end')} | {'first_token', 'oom', 'interrupted'}
        now = self.clock()
        if event not in allowed or isinstance(now, bool) or not math.isfinite(now):
            raise ValueError('Invalid engine event or clock')
        if self.rows and now < self.rows[-1]['at_seconds']:
            raise ValueError('Engine clock moved backward')
        self.rows.append({'event': event, 'at_seconds': now})
        if event == 'oom': raise MemoryError('Engine reported out of memory')
        if event == 'interrupted': raise RuntimeError('Engine reported interruption')

    def metrics(self):
        result = {}
        for name in ('load', 'queue', 'prompt', 'generation', 'switch'):
            starts = [r['at_seconds'] for r in self.rows if r['event'] == name + '_start']
            ends = [r['at_seconds'] for r in self.rows if r['event'] == name + '_end']
            result[name + '_seconds'] = (ends[0] - starts[0] if len(starts) == len(ends) == 1
                                        and ends[0] >= starts[0] else None)
        first = [r['at_seconds'] for r in self.rows if r['event'] == 'first_token']
        queued = [r['at_seconds'] for r in self.rows if r['event'] == 'queue_start']
        result['ttft_seconds'] = (first[0] - queued[0] if len(first) == len(queued) == 1
                                  and first[0] >= queued[0] else None)
        return result


class BenchmarkHarness:
    def __init__(self, core, root, *, clock=monotonic):
        self.core, self.root, self.clock = core, Path(root), clock
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def run(self, case_id, task, environment_factory, *, contract_version, configuration,
            prepare=None, evidence_kind='synthetic', measurements=None, rates=None,
            mode='cloudyfan'):
        if not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,59}', case_id):
            raise ValueError('Invalid benchmark case ID')
        if evidence_kind not in {'synthetic', 'measured'}:
            raise ValueError('Evidence kind must be synthetic or measured')
        config = asdict(configuration)
        measurements, rates = measurements or {}, rates or {}
        allowed_measurements = {'peak_memory_bytes', 'average_power_w', 'energy_kwh', 'human_seconds'}
        allowed_rates = {'electricity_usd_per_kwh', 'host_usd_per_hour', 'human_usd_per_hour'}
        if set(measurements) - allowed_measurements or set(rates) - allowed_rates:
            raise ValueError('Unknown resource measurement or price field')
        for value in [*measurements.values(), *rates.values()]:
            _number(value)
        tier = {'cloudyfan': 'open_source', 'open_source_first': 'open_source',
                'low_first': 'low', 'fixed_high': 'high'}.get(mode)
        profile = self.core.profiles.get(tier, {})
        if profile.get('model_version') != config['model_version']:
            raise ValueError('Benchmark configuration differs from executed model')
        if profile.get('backend') == 'local' and any(profile.get(k) != config[k] for k in ('quantization', 'engine')):
            raise ValueError('Benchmark configuration differs from local quantization or engine')
        directory = self.root / case_id
        if directory.is_symlink(): raise ValueError('Benchmark directory cannot be a symlink')
        directory.mkdir(mode=0o700, exist_ok=True)
        frozen = {'task': task, 'contract_version': contract_version, 'configuration': config,
                  'evidence_kind': evidence_kind, 'mode': mode,
                  'measurements': measurements or {}, 'rates': rates or {},
                  'core_configuration': self.core.configuration_descriptor()}
        with _lock(directory / '.lock'):
            _save(directory / 'manifest.json', frozen)
            if (directory / 'report.json').exists():
                return _read(directory / 'report.json')
            events = _Events(self.clock)
            started = self.clock()
            result, failure = None, None
            tool_evidence = {'declared': False}
            def measured_environment():
                environment = environment_factory()
                tool_evidence['declared'] = bool(environment.tools_schema())
                return environment
            try:
                if prepare is not None: prepare(events.emit)
                result = self.core.run('benchmark-' + case_id, task, measured_environment,
                                      contract_version=contract_version, mode=mode)
            except Exception as error:
                names = {row['event'] for row in events.rows}
                failure = ('out_of_memory' if isinstance(error, MemoryError) or 'oom' in names else
                           'engine_interrupted' if 'interrupted' in names else 'execution_error')
            ledger = self._ledger('benchmark-' + case_id)
            elapsed = self.clock() - started
            resources = {k: (measurements or {}).get(k) for k in
                         ('peak_memory_bytes', 'average_power_w', 'energy_kwh', 'human_seconds')}
            report = {'case_id': case_id, 'configuration': deepcopy(config), 'contract_version': contract_version,
                      'configuration_sha256': hashlib.sha256(_encoded(config)).hexdigest(),
                      'evidence_kind': evidence_kind, 'hardware_status': 'not_validated', 'concurrency_validated': False,
                      'status': result['status'] if result else 'engineering_interruption',
                      'public_passed': result['final_public_pass'] if result else None, 'failure': failure,
                      'ledger': ledger,
                      'events': events.rows, 'metrics': {**events.metrics(), 'wall_seconds': elapsed},
                      'measurements': resources,
                      'costs': _costs(ledger['api_usd'], elapsed, resources, rates,
                                      tool_cost=None if tool_evidence['declared'] else '0'),
                      'core_result': result}
            _save(directory / 'report.json', report)
            return report

    def _ledger(self, run_id):
        rows = []
        clients = [self.core.client]
        if self.core.local_backend is not None: clients.append(self.core.local_backend)
        call_pattern = re.compile(re.escape('run-' + run_id + '-') + r'(?:a[0-9]+-t[0-9]+|decision-(?:low_mid|mid_high))\Z')
        for client in clients:
            rows.extend(row for row in summarize(client)['rows'] if call_pattern.fullmatch(row['call_id']))
        known = sum((Decimal(row['cost_usd']) for row in rows if row['cost_usd'] is not None
                     and row['evidence_status'] != 'duplicate_identity'), Decimal(0))
        unknown = sum(bool(row['cost_unknown']) for row in rows)
        return {'calls': len(rows), 'known_api_usd': str(known), 'api_usd': None if unknown else str(known),
                'unknown_calls': unknown, 'rows': rows}


def _number(value):
    if value is None: return None
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number < 0:
            raise ValueError('Resource quantities and prices must be finite and nonnegative')
        return number
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid resource quantity or price') from None


def _costs(api, elapsed, measurements, rates, *, tool_cost):
    def multiply(quantity, rate, divisor=1):
        quantity, rate = _number(quantity), _number(rate)
        return None if quantity is None or rate is None else quantity * rate / Decimal(divisor)
    values = {'api_usd': _number(api), 'external_tool_usd': _number(tool_cost),
              'electricity_usd': multiply(measurements['energy_kwh'], rates.get('electricity_usd_per_kwh')),
              'host_usd': multiply(elapsed, rates.get('host_usd_per_hour'), 3600),
              'human_usd': multiply(measurements['human_seconds'], rates.get('human_usd_per_hour'), 3600)}
    complete = None if any(value is None for value in values.values()) else sum(values.values())
    resources = [v for k, v in values.items() if k != 'api_usd']
    scenario = None if any(v is None for v in resources) else sum(resources)
    return {**{key: str(value) if value is not None else None for key, value in values.items()},
            'complete_usd': str(complete) if complete is not None else None,
            'hypothetical_zero_model_fee_complete_usd': str(scenario) if scenario is not None else None}
