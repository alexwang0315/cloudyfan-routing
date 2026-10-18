"""Bind a task to a score snapshot before any paid core call."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from ..engine import Core
from .catalog import select_profiles, verify_snapshot


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ScoreCore(Core):
    """A single-run binding; refreshing the store cannot alter this instance.

    Resume requires the original explicit snapshot id and original requirements.
    Expired snapshots may finish an existing run, never start another paid run.
    """
    def __init__(self, client, snapshot, requirements, decision_profile, *, run_id, clock=utc_now, **kwargs):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', run_id):
            raise ValueError('Invalid run id')
        verify_snapshot(snapshot)
        self._snapshot = deepcopy(snapshot)
        self._bound_run_id = run_id
        self._clock = clock
        self._manifest = Path(client.root) / 'runs' / run_id / 'manifest.json'
        if self._manifest.is_symlink() or self._manifest.parent.is_symlink():
            raise ValueError('Run manifest cannot use symlinks')
        if self._manifest.exists():
            frozen = json.loads(self._manifest.read_text())
            profiles = frozen['profiles']
            for tier in ('low', 'mid', 'high', 'open_source'):
                selection = profiles[tier]['capability_card']['score_selection']
                if selection['snapshot_id'] != snapshot['snapshot_id'] or selection['requirements'] != requirements:
                    raise ValueError('Resume requires the original snapshot and requirements')
            # Core will additionally verify decision, task, limits, contract and system.
        else:
            profiles = select_profiles(snapshot, requirements, now=clock())
        super().__init__(client, profiles, decision_profile, **kwargs)

    def run(self, run_id, task, environment_factory, *, contract_version, mode='cloudyfan'):
        if mode != 'cloudyfan':
            raise ValueError('ScoreCore is for CloudyFan routing; bind experiment baselines explicitly with Core')
        if run_id != self._bound_run_id:
            raise ValueError('ScoreCore is bound to one run id; bind a fresh snapshot for a new run')
        if not self._manifest.exists():
            verify_snapshot(self._snapshot, now=self._clock())
        return super().run(run_id, task, environment_factory, contract_version=contract_version, mode=mode)
