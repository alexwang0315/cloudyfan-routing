"""整套測試的殘檔防線：任何題型都不可在專案資料夾留下新檔。

測試輸出應寫到 tmp_path；被測程式若只肯寫進專案（例如 runtime/），
測試要用不重複的檔名，並在結束時自己清掉。

- 每個測試結束後，檢查專案最上面三層有沒有多出檔案或目錄，有就讓該測試失敗。
  只看淺層，是因為主 checkout 的 runtime/ 有數萬個舊檔，每題全掃太慢。
- 整場開始與結束各全掃一次，抓更深層的殘檔；有新增就讓整場失敗。
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
# 工具自己的環境與快取，不算測試產物；.DS_Store 由 Finder 隨時產生。
IGNORED_NAMES = frozenset({'.git', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache', '.DS_Store'})
WATCH_DEPTH = 2


def _names(directory):
    try:
        return frozenset(name for name in os.listdir(directory) if name not in IGNORED_NAMES)
    except (FileNotFoundError, NotADirectoryError):
        return frozenset()


def _walk(max_depth=None):
    """回傳 ROOT 底下的目錄（到 max_depth 層）與所有路徑。"""
    directories, paths = [], set()
    for current, dirnames, filenames in os.walk(ROOT):
        depth = len(Path(current).relative_to(ROOT).parts)
        dirnames[:] = [name for name in dirnames if name not in IGNORED_NAMES]
        if max_depth is None or depth <= max_depth:
            directories.append(Path(current))
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []
            continue
        paths.update(Path(current, name) for name in [*dirnames, *filenames] if name not in IGNORED_NAMES)
    return directories, paths


def _report(paths):
    return ', '.join(sorted(str(path.relative_to(ROOT)) for path in paths))


class _ShallowWatch:
    """以目錄修改時間為觸發，只重讀有變動的淺層目錄。"""

    def __init__(self):
        self.seen = {}
        for directory in _walk(WATCH_DEPTH)[0]:
            self.seen[directory] = self._state(directory)

    @staticmethod
    def _state(directory):
        try:
            return directory.stat().st_mtime_ns, _names(directory)
        except FileNotFoundError:
            return None, frozenset()

    def added(self):
        fresh = []
        for directory, (mtime, names) in self.seen.items():
            state = self._state(directory)
            if state[0] == mtime:
                continue
            # 更新基準，讓後面的測試不會被前一題的殘檔連累；整場檢查仍會擋下。
            self.seen[directory] = state
            fresh.extend(directory / name for name in state[1] - names)
        return fresh


_session_paths = set()
_watch = None


def pytest_sessionstart(session):
    global _watch
    _session_paths.update(_walk()[1])
    _watch = _ShallowWatch()


@pytest.fixture(autouse=True)
def no_project_residue():
    yield
    added = _watch.added()
    if added:
        pytest.fail('測試在專案資料夾留下新檔，請改寫到 tmp_path 或在結束時清掉：' + _report(added),
                    pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    added = _walk()[1] - _session_paths
    if added:
        reporter = session.config.pluginmanager.get_plugin('terminalreporter')
        message = '整場測試在專案資料夾留下新檔：' + _report(added)
        if reporter:
            reporter.ensure_newline()
            reporter.write_line(message, red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
