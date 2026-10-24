"""Picklable adapter for the published real resilience host.

The existing bounded runner owns sockets, physical MockTransport hold/kill
signals, thread shutdown and IPC. No admission or witness is substituted here.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace


def assemble_resilience_host(root, index, clock, origin, options, before_response):
    from capacity_resilience_fixture import build_resilience_host, build_resilience_health_host
    if options.get('health_core'):
        original = build_resilience_health_host(root, clock=clock, origin=origin,
            before_response=before_response)
        service = original.queue.service
        protected = [Path(root) / name for name in ('capacity.sqlite', 'queue.sqlite', 'health')]
        protected += [service.root, original.controller.store.integrity.root,
            *(budget.path for budget in original.budgets.values()),
            *(contract.runner.client.root for contract in service.contracts.values())]

        def close():
            # The real boundary fixture owns these exact request records.
            # Persist even failed/side requests on normal process shutdown.
            path = Path(root) / ('health-process-side-requests-' + str(os.getpid()) + '.json')
            path.write_text(json.dumps({'pid': os.getpid(), 'requests': original.requests,
                'side_requests': original.side_requests}, allow_nan=False))
            path.chmod(0o600)
            original.close()

        host = SimpleNamespace(app=original.app, queue=original.queue,
            credential=original.credentials['studio'].token, credentials=original.credentials,
            operator_token=original.operator_token, protected_paths=tuple(protected), close=close)
    else:
        host = build_resilience_host(root, clock=clock, origin=origin, options=options,
            before_response=before_response)
    host.protected_paths += (host.queue.integrity.root,)
    return host
