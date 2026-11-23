"""整套殘檔防線本身：在暫存專案裡跑探針，確認淺層逐題擋、深層整場擋。"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBES = '''
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def test_leaves_shallow_file():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    (ROOT / 'runtime' / 'left.json').write_text('{}')

def test_leaves_deep_file():
    deep = ROOT / 'src' / 'a' / 'b' / 'c'
    deep.mkdir(parents=True)
    (deep / 'left.txt').write_text('x')

def test_cleans_up_after_itself():
    path = ROOT / 'runtime' / 'temporary.json'
    path.write_text('{}')
    path.unlink()
'''


def run_probe_project(tmp_path, probes):
    project = tmp_path / 'project'
    (project / 'tests').mkdir(parents=True)
    (project / 'src').mkdir()
    shutil.copy(ROOT / 'conftest.py', project / 'conftest.py')
    (project / 'tests' / 'test_probes.py').write_text(probes)
    return subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests'],
                          cwd=project, capture_output=True, text=True)


def test_guard_fails_the_test_and_session_that_leave_files(tmp_path):
    result = run_probe_project(tmp_path, PROBES)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'ERROR at teardown of test_leaves_shallow_file' in result.stdout
    assert 'runtime/left.json' in result.stdout
    assert 'test_cleans_up_after_itself' not in result.stdout.split('short test summary info')[-1]
    assert '整場測試在專案資料夾留下新檔' in result.stdout and 'src/a/b/c/left.txt' in result.stdout


def test_guard_passes_a_clean_project(tmp_path):
    result = run_probe_project(tmp_path, 'def test_nothing():\n    pass\n')
    assert result.returncode == 0, result.stdout + result.stderr
