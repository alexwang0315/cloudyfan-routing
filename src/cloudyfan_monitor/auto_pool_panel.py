"""換池紀錄頁（#80）：讀取 MRC 自動換池紀錄，操作員可回復上一份快照與切換總開關。

只包裝 MRC `AutoPoolController` 的既有介面（changes／rollback／set_auto_switch），
不自行改寫快照或分群；換池判斷仍完全在 MRC。
"""
from datetime import datetime, timezone

from .store import MonitorError

SCHEMA = 'monitor-auto-pool-v1'
TIERS = ('open_source', 'low', 'mid', 'high')
MAX_ROWS = 200


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class AutoPoolPanel:
    def __init__(self, controller, *, now=_utc_now, operator='monitor-operator'):
        from cloudyfan_jev.scores.auto_pool import AutoPoolController
        if type(controller) is not AutoPoolController:
            raise ValueError('Controlled auto-pool controller required')
        self.controller, self.now, self.operator = controller, now, operator

    def _current(self):
        try:
            return self.controller.store.load()
        except (OSError, ValueError, KeyError):
            raise MonitorError('auto_pool_unavailable', 503) from None

    def snapshot(self):
        current = self._current()
        try:
            changes = self.controller.changes()
            enabled = self.controller.auto_switch_enabled()
        except (OSError, ValueError):
            raise MonitorError('auto_pool_unavailable', 503) from None
        override = current.get('pool_override') or {}
        return {'schema_version': SCHEMA, 'configured': True, 'auto_switch_enabled': enabled,
                'current_snapshot_id': current['snapshot_id'],
                'current_grouping': {tier: [entry['config_id'] for entry in current['pools'].get(tier) or []]
                                     for tier in TIERS},
                'original_grouping': override.get('original_grouping'),
                'disabled_tiers': override.get('disabled_tiers', []),
                # 與 MRC rollback() 相同的條件：目前快照必須是某筆紀錄的「換後」快照。
                'rollback_available': any(row.get('after_snapshot_id') == current['snapshot_id']
                                          for row in changes),
                'changes': list(reversed(changes))[:MAX_ROWS]}

    def rollback(self, expected_snapshot_id):
        if self._current()['snapshot_id'] != expected_snapshot_id:
            raise MonitorError('auto_pool_changed_reload', 409)
        try:
            return self.controller.rollback(now=self.now(), operator=self.operator)
        except ValueError:
            # 沒有可回復的紀錄、或上一份快照已過期：照實拒絕，不硬改指標。
            raise MonitorError('auto_pool_rollback_unavailable', 409) from None

    def set_auto_switch(self, enabled):
        if type(enabled) is not bool:
            raise MonitorError('auto_switch_boolean_required')
        self.controller.set_auto_switch(enabled, now=self.now(), operator=self.operator)
        return self.snapshot()
