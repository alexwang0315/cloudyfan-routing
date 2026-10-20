"""Resumable exact-model qualification capture, separate from routing and grading.

The application supplies a shared authorized BudgetStore and a real bound verifier.
No credentials, real transport, authorizations or qualification approvals are created
here. The bundled CLI only constructs MockTransport and marks everything synthetic.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

import httpx

from ..budget import BudgetError, _exact, money
from ..client import CHAT, not_dispatched, usage
from ..diagnostics import DiagnosticContract
from ..engine import _lock, _save
from .catalog import digest
from .configuration import configuration_digest
from .sql_capability import captured_attempt, grade_model
from .sql_exam import cases, manifest

ROLES = ('open_source', 'low', 'mid', 'high')
VERSION = 'sql-model-capture-v1'
SYSTEM = 'Answer the public task with one JSON object containing a sql string. No tools are available.'


class CaptureStopped(RuntimeError):
    pass


class QualificationRunner:
    """Capture four pinned settings in a fixed order; sealed grading is opt-in later.

    Synthetic admission additionally requires an actual httpx.MockTransport. Real
    callers own spending authority, enforceable bounds, provider qualification and
    credential isolation. A true callback or JSON reference is not supplier proof.
    """
    def __init__(self, client, store, table, *, model_bounds, verify_bound, synthetic=False,
                 environment_factory=None, environment_version=None):
        if type(synthetic) is not bool or not callable(verify_bound):
            raise ValueError('Explicit synthetic mode and bound verifier required')
        if synthetic and not isinstance(client.http._transport, httpx.MockTransport):
            raise ValueError('Synthetic capture requires MockTransport')
        if not synthetic and isinstance(client.http._transport, httpx.MockTransport):
            raise ValueError('MockTransport capture must be labeled synthetic')
        if environment_factory is not None and (not synthetic or not callable(environment_factory)
                or not isinstance(environment_version, str) or not environment_version.strip()):
            raise ValueError('Injected environments require synthetic mode and an explicit version')
        self.client, self.store = client, store
        self.table, self.bounds = deepcopy(table), dict(model_bounds)
        self.verify_bound, self.synthetic = verify_bound, synthetic
        self.environment_factory = environment_factory or (lambda case: case.contract().environment())
        self.environment_version = environment_version or 'frozen-sql-environment'
        configs = self.table.get('configurations', [])
        if (self.table.get('suite_id') != manifest()['suite_id']
                or [c.get('role') for c in configs] != list(ROLES)):
            raise ValueError('Frozen suite and all four ordered settings required')
        self.entries = {c['role']: c['entry'] for c in configs}
        for config in configs:
            entry = config['entry']
            if config['configuration_sha256'] != configuration_digest(entry):
                raise ValueError('Configuration digest mismatch')
            p = entry['profile']
            if p.get('endpoint', CHAT) != CHAT or p.get('backend') == 'local':
                raise ValueError('This exam qualifies pinned Chat Completions settings only')
            if p['accepted_models'] != [p['model_version']]:
                raise ValueError('Exact model identity required, no alias fallback')
            if p['model_version'] not in self.bounds:
                raise BudgetError('Missing confirmed model bound')
        self.diagnostics = DiagnosticContract(version='sql-exam-public-v1', source='frozen-sql-contract')

    def _directory(self, run_id, *, create=True):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', run_id):
            raise ValueError('A short stable exam ID is required')
        parent = self.client.root / 'exams'
        directory = parent / run_id
        if parent.is_symlink() or directory.is_symlink():
            raise ValueError('Exam paths cannot be symlinks')
        if create:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def _cid(self, run_id, role, task, attempt=0):
        return f'exam-{digest(run_id)[:16]}-{task}-{role}-{attempt}'

    def _event(self, cid):
        return 'exam:' + digest(str(self.client.root.resolve())) + ':' + cid

    def _verify(self, payload, *, post=False):
        bound = self.bounds[payload['model']]
        verifier = getattr(self.verify_bound, 'verify_post' if post else 'verify_dispatch', None)
        valid = (verifier(self.client, CHAT, payload, bound) if callable(verifier)
                 else self.verify_bound('model', payload['model'], payload, bound))
        if valid is not True:
            raise BudgetError('Enforceable complete bound unconfirmed')
        return True

    def _payload(self, role, prompt, previous=None):
        p = self.entries[role]['profile']
        messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}]
        if previous is not None:
            messages += [{'role': 'assistant', 'content': previous['answer']},
                         {'role': 'user', 'content': 'Repair using only this public diagnostic: '
                          + json.dumps(previous['diagnostic'], ensure_ascii=False, sort_keys=True)}]
        if len(json.dumps(messages, ensure_ascii=False).encode()) > 32768:
            raise CaptureStopped('public_context_limit')
        return {'model': p['model_version'], 'reasoning': {'effort': p['effort']},
                'provider': {'only': [p['provider']], 'allow_fallbacks': False, 'require_parameters': True},
                'messages': messages, 'max_tokens': 128 if prompt == 'Reply OK.' else 4096}

    def _frozen(self):
        descriptor = getattr(self.verify_bound, 'descriptor', None)
        return {'version': VERSION, 'table': self.table, 'synthetic': self.synthetic,
                'root': str(self.client.root.resolve()), 'budget': str(self.store.path.resolve()),
                'system': SYSTEM, 'max_tokens': 4096, 'max_context_bytes': 32768,
                'max_attempts': 2, 'classifier_calls': 0,
                'environment_version': self.environment_version,
                'bounds': {k: asdict(v) for k, v in sorted(self.bounds.items())},
                'bound_verifier': descriptor() if callable(descriptor) else None,
                'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'grader_sha256': hashlib.sha256(Path(__file__).with_name('sql_capability.py').read_bytes()).hexdigest(),
                'public_contracts': {c.id: c.contract().contract_version for c in cases()}}

    def _capture_call(self, run_id, role, cid, payload):
        if role not in ROLES:
            raise CaptureStopped('unexpected_exam_role')
        p = self.entries[role]['profile']
        if (payload.get('model') != p['model_version']
                or payload.get('reasoning') != {'effort': p['effort']}
                or payload.get('provider') != {'only': [p['provider']], 'allow_fallbacks': False,
                                               'require_parameters': True}):
            raise CaptureStopped('frozen_request_identity_changed')
        directory = self.client._directory(cid)
        event_id = self._event(cid)
        if (directory / 'intent.json').exists():
            # Reuse only. Client refuses any incomplete intent rather than resend.
            if not any(e['id'] == event_id for e in self.store.report()['events']):
                raise CaptureStopped('missing_budget_event')
            record = self.client.call(cid, CHAT, payload)
        else:
            self._verify(payload)
            fresh = self.store.begin_event('sql-exam-' + digest(run_id)[:20], event_id, 'model',
                money(self.bounds[payload['model']].usd), digest({'url': CHAT, 'payload': payload}))
            if fresh is not True:
                raise CaptureStopped('missing_intent_for_existing_event')
            record = self.client.call(cid, CHAT, payload, before_post=lambda: self._verify(payload, post=True))
        info = usage(record)
        denied = not_dispatched(record)
        receipt = ('not-dispatched:' + digest(cid) if denied else info['generation_id'])
        self.store.record_event(event_id, cost_usd=info['cost_usd'] if receipt else None,
                                receipt_id=receipt, verified=denied or info['cost_source'] == 'generation_total_cost')
        if denied:
            raise CaptureStopped('enforcement_denied_before_post')
        if (info['cost_unknown'] or not info['generation_id'] or record.get('duplicate_generation_id')):
            raise CaptureStopped('unknown_fee_or_generation_identity')
        if record.get('http_status') != 200:
            raise CaptureStopped('provider_failure')
        if info['model'] != p['model_version'] or info['provider'] != p['provider_name']:
            raise CaptureStopped('model_or_provider_mismatch')
        data = json.loads(record['raw'])
        if data.get('model') != p['model_version'] or data.get('provider') != p['provider_name']:
            raise CaptureStopped('model_or_provider_mismatch')
        message = data['choices'][0]['message']
        if message.get('tool_calls') or not isinstance(message.get('content'), str):
            raise CaptureStopped('unsupported_or_missing_text')
        return record

    def capture(self, run_id, *, max_new_requests=None):
        """Resume in fixed order; optional request quota pauses without selecting tasks.

        All failures retain receipts and the full reservation. No automatic GET or
        retry occurs. Resolve unknown charges explicitly through the existing Client
        before resuming; this method itself cannot turn an unknown into zero.
        """
        if max_new_requests is not None and (type(max_new_requests) is not int or max_new_requests < 0):
            raise ValueError('Request quota must be a nonnegative integer')
        directory = self._directory(run_id)
        new_calls = 0
        with _lock(directory / '.lock'):
            _save(directory / 'manifest.json', self._frozen())
            try:
                for role in ROLES:
                    self._verify(self._payload(role, 'Reply OK.'))
                    self._verify(self._payload(role, cases()[0].contract().task()['prompt']))
                reserve = _exact((money(self.bounds[self.entries[r]['profile']['model_version']].usd) for r in ROLES), 101)
                budget_run = 'sql-exam-' + digest(run_id)[:20]
                # Refresh a saved response into its original event before reserve:
                # a crash may have occurred between durable response and accounting.
                self._refresh_billing(run_id)
                self.store.reserve(budget_run, digest(self._frozen()), reserve)
                if (directory / 'capture-complete.json').exists():
                    self._check_seal(run_id)
                    self.store.finish(budget_run)
                    self._billing_revision(run_id)
                    return self.inspect(run_id, status='capture_complete')
                jobs = [(r, None) for r in ROLES]
                for index, case in enumerate(cases()):
                    jobs.extend((ROLES[(index+j) % 4], case) for j in range(4))
                for role, case in jobs:
                    env = None
                    previous = None
                    for attempt in range(1 if case is None else 2):
                        cid = self._cid(run_id, role, 'identity' if case is None else case.id, attempt)
                        saved = (self.client._directory(cid) / 'intent.json').exists()
                        if not saved and max_new_requests is not None and new_calls >= max_new_requests:
                            return self.inspect(run_id, status='paused')
                        checkpoint = directory / (cid + '-public.json')
                        if case is not None and env is None and (not saved or not checkpoint.exists()):
                            env = self.environment_factory(case)  # preflight BEFORE POST
                        payload = self._payload(role, 'Reply OK.' if case is None else case.contract().task()['prompt'], previous)
                        record = self._capture_call(run_id, role, cid, payload)
                        new_calls += int(not saved)
                        if case is None:
                            break
                        normalized = captured_attempt(self.entries[role], case,
                            json.loads((self.client._directory(cid) / 'intent.json').read_text()), record)
                        if checkpoint.exists():
                            public = json.loads(checkpoint.read_text())
                            if public['response_sha256'] != normalized['response_sha256']:
                                raise CaptureStopped('saved_public_response_changed')
                        else:
                            public = {'response_sha256': normalized['response_sha256'],
                                      'diagnostic': self.diagnostics.project(env.validate(normalized['answer']))}
                            _save(checkpoint, public)
                        previous = {'answer': normalized['answer'], 'diagnostic': public['diagnostic']}
                        if public['diagnostic']['passed'] is True:
                            break
                complete = {'manifest_sha256': digest(self._frozen()), 'original_capture': self._answer_seal(run_id)}
                _save(directory / 'capture-complete.json', complete)
                self.store.finish(budget_run)
                self._billing_revision(run_id)
                return self.inspect(run_id, status='capture_complete')
            except Exception as error:
                # Only a safe class/code, never arbitrary provider prose or secrets.
                reason = str(error) if isinstance(error, CaptureStopped) else type(error).__name__
                _save(directory / f'interruption-{len(list(directory.glob("interruption-*.json")))+1:04d}.json',
                      {'kind': type(error).__name__, 'reason': reason})
                return self.inspect(run_id, status='stopped', reason=reason)

    def _refresh_billing(self, run_id):
        """Refresh original ledger events from saved responses; never perform GET/POST."""
        for path in sorted((self.client.root / 'calls').glob('*/intent.json')):
            cid = path.parent.name
            if not cid.startswith(f'exam-{digest(run_id)[:16]}-') or not (path.parent / 'response.json').exists():
                continue
            intent = json.loads(path.read_text())
            self._capture_call(run_id, cid.rsplit('-', 2)[-2], cid, intent['payload'])

    def _answer_seal(self, run_id):
        """Bind original intent/response/checkpoints, independently of later fee receipts."""
        directory = self._directory(run_id, create=False)
        originals = {}
        for path in sorted((self.client.root / 'calls').glob('*/intent.json')):
            cid = path.parent.name
            if not cid.startswith(f'exam-{digest(run_id)[:16]}-'):
                continue
            public = directory / (cid + '-public.json')
            originals[cid] = {'intent': digest(json.loads(path.read_text())),
                              'response': digest(self.client._original_record(path.parent)),
                              'public': digest(json.loads(public.read_text())) if public.exists() else None}
        return originals

    def _check_seal(self, run_id):
        complete = json.loads((self._directory(run_id, create=False) / 'capture-complete.json').read_text())
        if (complete['manifest_sha256'] != digest(self._frozen())
                or complete['original_capture'] != self._answer_seal(run_id)):
            raise ValueError('Original capture changed after sealing')

    def _billing_revision(self, run_id):
        body = {'records': {r: digest(self.records(run_id, r)) for r in ROLES},
                'accounting': self.inspect(run_id), 'synthetic': self.synthetic}
        revision = digest(body)
        _save(self._directory(run_id, create=False) / f'billing-{revision}.json', body)
        return revision

    def records(self, run_id, role):
        """Normalize saved answers only; never query the supplier or sealed assessor."""
        if role not in ROLES:
            raise ValueError('Unknown qualification role')
        rows = []
        for case in cases():
            attempts = []
            for index in range(2):
                cid = self._cid(run_id, role, case.id, index)
                directory = self.client._directory(cid)
                if (directory / 'intent.json').exists() and (directory / 'response.json').exists():
                    attempts.append(captured_attempt(self.entries[role], case,
                        json.loads((directory / 'intent.json').read_text()), self.client._load(directory)))
            rows.append({'task_id': case.id, 'attempts': attempts})
        return rows

    def inspect(self, run_id, *, status=None, reason=None):
        """Read-only accounting, including identity probes and interrupted calls."""
        directory = self._directory(run_id, create=False)
        if not (directory / 'manifest.json').is_file():
            raise ValueError('No saved exam')
        events = [e for e in self.store.report()['events']
                  if e['run'] == 'sql-exam-' + digest(run_id)[:20]]
        known = _exact(money(e['cost']) for e in events if e['cost'] is not None)
        unknown = sum(e['cost'] is None for e in events)
        reservation = next((r for r in self.store.report()['reservations']
                            if r['run'] == 'sql-exam-' + digest(run_id)[:20]), None)
        completed = (directory / 'capture-complete.json').exists()
        return {'version': VERSION, 'synthetic': self.synthetic,
                'status': status or ('capture_complete' if completed else 'incomplete'), 'reason': reason,
                'calls': len(events), 'classifier_calls': 0, 'known_api_usd': str(known),
                'unknown_cost_count': unknown, 'api_usd': None if unknown else str(known),
                'reserve_usd': reservation['amount'] if reservation else None,
                'formal_qualification': 'pending'}

    def grade(self, run_id, *, now):
        """Explicit post-capture sealed grading; does not dispatch or approve a pool."""
        directory = self._directory(run_id)
        with _lock(directory / '.lock'):
            _save(directory / 'manifest.json', self._frozen())
            self._check_seal(run_id)
            self._refresh_billing(run_id)
            billing_revision = self._billing_revision(run_id)
            reports = {}
            for role in ROLES:
                records = self.records(run_id, role)
                reports[role] = grade_model(self.entries[role], records, now=now, synthetic=self.synthetic)
            return {'synthetic': self.synthetic, 'models': reports, 'capture': self.inspect(run_id),
                    'billing_revision': billing_revision, 'formal_qualification': 'pending', 'pool_activated': False}
