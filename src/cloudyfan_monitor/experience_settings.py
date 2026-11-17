"""客戶端經驗共用設定（#80）：每個客戶端的「貢獻」「使用」開關。

設定存在 MRC 契約經驗庫（`ExperienceStore`）自己的檔案裡；Monitor 只透過它的
`freeze_settings()` 讀取，寫入一律走 `ExperienceStore.update()`（檔案鎖保護的
重讀–修改–原子存檔），與 MRC 其他程序的經驗回寫互斥，不碰經驗內容、不呼叫改寫。
"""
from pathlib import Path

from .store import MonitorError

SCHEMA = 'monitor-experience-settings-v1'


def _no_rewrite(*args, **kwargs):
    # 調整開關不需要改寫；萬一被呼叫就失敗，避免 Monitor 默默觸發模型。
    raise RuntimeError('Monitor never rewrites experience facts')


class ExperienceSettings:
    def __init__(self, path):
        self.path = Path(path)

    def _open(self):
        from cloudyfan_jev.experience import ExperienceStore
        try:
            if self.path.exists():
                return ExperienceStore.open(self.path, rewriter=_no_rewrite)
            return ExperienceStore(rewriter=_no_rewrite)
        except (OSError, ValueError, KeyError, TypeError):
            raise MonitorError('experience_store_unavailable', 503) from None

    def snapshot(self, clients):
        store = self._open()
        return {'schema_version': SCHEMA, 'configured': True,
                'stored': self.path.exists(),
                'clients': [{'client': client['id'], 'label': client['label'], **store.freeze_settings(client['id'])}
                            for client in clients]}

    def change(self, client, *, contribute=None, use=None):
        if contribute is None and use is None:
            raise MonitorError('experience_setting_required')
        if any(value is not None and type(value) is not bool for value in (contribute, use)):
            raise MonitorError('experience_setting_boolean_required')
        from cloudyfan_jev.experience import ExperienceStore

        def toggle(store):
            try:
                store.set_client_settings(client, contribute=contribute, use=use)
            except ValueError:
                raise MonitorError('experience_client_invalid') from None
            return store.freeze_settings(client)

        # 持 MRC 的檔案鎖重讀最新內容再寫回：跨程序的經驗回寫不會被覆蓋。
        try:
            _, frozen = ExperienceStore.update(self.path, toggle, rewriter=_no_rewrite)
        except (OSError, ValueError, KeyError, TypeError):
            raise MonitorError('experience_store_unavailable', 503) from None
        return {'client': client, **frozen}
