"""隔離執行模型寫的 Python 函式並跑測試（盲測改程式題用）。

執行方式：
- 子行程用基底 Python 直接執行，旗標 ``-I -S -B``（不讀 PYTHON* 環境變數與使用者目錄、不載入 site 與
  第三方套件、不寫 .pyc；標準庫照常可用）。環境變數只留 ``PATH``。
- 工作目錄是一次性的 ``tempfile.TemporaryDirectory``，不碰專案資料夾；結果檔放在另一個子行程不可寫的
  暫存目錄。
- macOS 上以 ``sandbox-exec`` 套用拒絕預設的規則：禁止網路、禁止 fork／exec 其他程式、讀取只允許
  Python 安裝目錄、系統程式庫與工作目錄，寫入只允許工作目錄。``require_sandbox=True`` 而找不到
  ``sandbox-exec`` 時回 ``sandbox_unavailable``，絕不退回無隔離執行。
- ``require_sandbox=False`` 只給測試執行**可信的參考解**（CI 的 Ubuntu 沒有 sandbox-exec）。
- 驅動程式在子行程內設 CPU 時間、檔案大小、開檔數與（能設時）記憶體上限，每題另有計時器；
  父行程另有整體逾時，逾時就殺掉整個行程群組。
- macOS 不接受任何記憶體 rlimit（RLIMIT_AS／RLIMIT_DATA 都設不下去），所以父行程每
  ``MEMORY_POLL_SECONDS`` 秒看一次子行程的常駐記憶體，超過 ``MEMORY_BYTES`` 就殺掉並回 ``crashed``。
  第二版題目有大輸入，模型若開「值域大小的陣列」會一次要好幾 GB，沒有這道防線會拖垮整台機器。
- 模型程式的 print 被導向 /dev/null；驅動程式把結果寫到事先複製的私有檔案描述子，並加上隨機分隔標記。
- 期望值不進子行程：子行程只回傳每題的實際值，由父行程比對，所以模型程式讀不到隱藏測試的答案。
"""
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = 'cloudyfan-code-sandbox-v1'
SANDBOX_EXEC = '/usr/bin/sandbox-exec'
PER_TEST_SECONDS = 1.0
MAX_TIMEOUTS = 3
MAX_VALUE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MEMORY_BYTES = 1024 * 1024 * 1024
MEMORY_POLL_SECONDS = 0.05
DETAIL_CHARS = 200
STATUSES = ('ok', 'no_code', 'syntax_error', 'timeout', 'sandbox_unavailable', 'crashed')

DRIVER = r'''
import gc, json, os, resource, signal, sys

def main():
    request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    out = os.dup(1)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    cpu = int(request['cpu_seconds'])
    for name, value in (('RLIMIT_CPU', cpu), ('RLIMIT_FSIZE', request['max_output_bytes']), ('RLIMIT_NOFILE', 64),
                        ('RLIMIT_CORE', 0), ('RLIMIT_AS', request['memory_bytes'])):
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except (ValueError, OSError, AttributeError):
            pass

    class Timeout(BaseException):
        pass

    def alarm(*_):
        raise Timeout()

    signal.signal(signal.SIGALRM, alarm)
    per_test = float(request['per_test_seconds'])

    def guarded(fn, *args):
        signal.setitimer(signal.ITIMER_REAL, per_test)
        try:
            return fn(*args)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    report = {'load': 'ok', 'results': []}
    namespace = {'__name__': 'solution', '__builtins__': __builtins__}
    try:
        guarded(exec, compile(request['code'], 'solution.py', 'exec'), namespace)
    except Timeout:
        report['load'] = 'load_timeout'
    except BaseException as error:
        report['load'] = 'load_error: ' + type(error).__name__
    fn = namespace.get(request['entry_point'])
    if report['load'] == 'ok' and not callable(fn):
        report['load'] = 'entry_point_missing'
    if report['load'] == 'ok':
        timeouts = 0
        # 先把全部輸入解碼並凍結在 GC 之外：大輸入有上百萬個物件，否則循環 GC 會在計時中掃過它們。
        decoded = [json.loads(encoded) for encoded in request['tests']]
        gc.collect()
        gc.freeze()
        for args in decoded:
            if timeouts >= request['max_timeouts']:
                report['results'].append({'error': 'not_run'})
                continue
            try:
                value = guarded(fn, *args)
                text = json.dumps(value, allow_nan=False, ensure_ascii=False)
                if len(text) > request['max_value_bytes']:
                    report['results'].append({'error': 'result_too_large'})
                else:
                    report['results'].append({'value': text})
            except Timeout:
                timeouts += 1
                report['results'].append({'error': 'timeout'})
            except BaseException as error:
                report['results'].append({'error': type(error).__name__})
    data = (request['nonce'] + json.dumps(report, ensure_ascii=False) + '\n').encode('utf-8')
    while data:
        data = data[os.write(out, data):]

main()
'''


def _interpreter():
    """回傳 (直譯器實際路徑, 安裝根目錄)；直接跑基底直譯器，不經 venv 的 pyvenv.cfg。"""
    executable = os.path.realpath(getattr(sys, '_base_executable', None) or sys.executable)
    return executable, os.path.realpath(sys.base_prefix)


def sandbox_available():
    return sys.platform == 'darwin' and os.access(SANDBOX_EXEC, os.X_OK)


def _quote(path):
    if '"' in path or '\\' in path:
        raise ValueError('Unsupported path for sandbox profile')
    return '"' + path + '"'


def sandbox_profile(work, executable, prefix):
    """拒絕預設；只開放執行直譯器、讀安裝目錄與系統程式庫、讀寫工作目錄。沒有網路、fork 或 exec。"""
    reads = ' '.join([f'(subpath {_quote(prefix)})', f'(subpath {_quote(os.path.dirname(executable))})',
                      f'(subpath {_quote(work)})', '(literal "/")', '(literal "/dev/null")',
                      '(literal "/dev/urandom")', '(literal "/dev/random")', '(subpath "/usr/lib")',
                      '(subpath "/System")', '(subpath "/private/var/db/dyld")', '(subpath "/Library/Apple")'])
    return '\n'.join([
        '(version 1)',
        '(deny default)',
        f'(allow process-exec (literal {_quote(executable)}))',
        f'(allow file-read* {reads})',
        '(allow file-read-metadata)',
        f'(allow file-write* (subpath {_quote(work)}) (literal "/dev/null"))',
        '(allow sysctl-read)',
    ]) + '\n'


def _short(text):
    return text if len(text) <= DETAIL_CHARS else text[:DETAIL_CHARS - 3] + '...'


def strict_equal(a, b):
    """JSON 值逐型別比對：True 不等於 1，1 不等於 1.0。"""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    return a == b


def _result(status, total, passed=0, failures=()):
    return {'status': status, 'passed': passed, 'total': total, 'failures': list(failures)}


def _syntax_ok(code):
    try:
        compile(code, 'solution.py', 'exec', dont_inherit=True)
        return True
    except (SyntaxError, ValueError, OverflowError, MemoryError, RecursionError):
        return False


def _kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def _rss_bytes(pid):
    """子行程目前的常駐記憶體（位元組）；讀不到就回 0。Linux 讀 /proc，macOS 用 ps。"""
    try:
        with open(f'/proc/{pid}/statm') as stream:
            return int(stream.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(pid)], capture_output=True, text=True,
                             timeout=2, env={'PATH': '/usr/bin:/bin'}).stdout
        return int(out.strip() or 0) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def run_tests(code, entry_point, tests, *, timeout_s=10, per_test_s=PER_TEST_SECONDS, require_sandbox=True):
    """執行 ``code`` 中的 ``entry_point``，逐一比對 ``tests``（``[{'args': [...], 'expected': ...}]``）。

    回傳 ``{'status', 'passed', 'total', 'failures': [{'index', 'kind': 'wrong'|'error', 'detail'}]}``。
    ``detail`` 會帶實際值與期望值，呼叫端決定能不能公開（隱藏測試的 detail 絕不可給模型）。
    ``timeout_s`` 是整體上限；``per_test_s`` 是每個測試的上限（預設 1 秒，不可超過整體上限）。
    """
    total = len(tests)
    if not isinstance(code, str) or not code.strip():
        return _result('no_code', total)
    if not _syntax_ok(code):
        return _result('syntax_error', total)
    if require_sandbox and not sandbox_available():
        return _result('sandbox_unavailable', total)
    executable, prefix = _interpreter()
    nonce = 'CLOUDYFAN-RESULT-' + secrets.token_hex(16) + ':'
    request = json.dumps({
        'code': code, 'entry_point': entry_point, 'nonce': nonce,
        'tests': [json.dumps(t['args'], ensure_ascii=False) for t in tests],
        'per_test_seconds': min(float(per_test_s), float(timeout_s)), 'max_timeouts': MAX_TIMEOUTS,
        'cpu_seconds': int(timeout_s) + 1, 'memory_bytes': MEMORY_BYTES, 'max_value_bytes': MAX_VALUE_BYTES,
        'max_output_bytes': MAX_OUTPUT_BYTES,
    }, ensure_ascii=False).encode('utf-8')
    with tempfile.TemporaryDirectory(prefix='cf-code-') as work_dir, \
            tempfile.TemporaryDirectory(prefix='cf-code-out-') as out_dir:
        work = os.path.realpath(work_dir)
        (Path(work) / 'driver.py').write_text(DRIVER)
        command = [executable, '-I', '-S', '-B', 'driver.py']
        if require_sandbox:
            profile = Path(work) / 'profile.sb'
            profile.write_text(sandbox_profile(work, executable, prefix))
            os.chmod(profile, 0o400)
            command = [SANDBOX_EXEC, '-f', str(profile)] + command
        out_path = Path(out_dir) / 'result.out'
        with open(out_path, 'wb') as out, tempfile.TemporaryFile() as stdin:
            stdin.write(request)
            stdin.seek(0)
            process = subprocess.Popen(command, cwd=work, stdin=stdin, stdout=out, stderr=subprocess.DEVNULL,
                                       env={'PATH': '/usr/bin:/bin'}, start_new_session=True, close_fds=True)
            deadline = time.monotonic() + float(timeout_s)
            try:
                while process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _kill(process)
                        return _result('timeout', total)
                    try:
                        process.wait(timeout=min(MEMORY_POLL_SECONDS, remaining))
                    except subprocess.TimeoutExpired:
                        if _rss_bytes(process.pid) > MEMORY_BYTES:
                            _kill(process)
                            return _result('crashed', total)
            finally:
                if process.poll() is None:
                    _kill(process)
        with open(out_path, 'rb') as stream:
            raw = stream.read(MAX_OUTPUT_BYTES + 1)
    if process.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
        return _result('timeout', total)
    report = _parse(raw, nonce)
    if report is None or process.returncode != 0:
        return _result('crashed', total)
    return _score(report, tests)


def _parse(raw, nonce):
    if len(raw) > MAX_OUTPUT_BYTES:
        return None
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None
    lines = [line for line in text.splitlines() if line.startswith(nonce)]
    if len(lines) != 1:
        return None
    try:
        report = json.loads(lines[0][len(nonce):])
    except ValueError:
        return None
    if not isinstance(report, dict) or not isinstance(report.get('load'), str) \
            or not isinstance(report.get('results'), list):
        return None
    return report


def _score(report, tests):
    total = len(tests)
    if report['load'] != 'ok':
        failures = [{'index': i, 'kind': 'error', 'detail': report['load']} for i in range(total)]
        return _result('timeout' if report['load'] == 'load_timeout' else 'ok', total, 0, failures)
    results = report['results']
    if len(results) != total:
        return _result('crashed', total)
    passed, failures = 0, []
    for index, (test, item) in enumerate(zip(tests, results)):
        if not isinstance(item, dict):
            return _result('crashed', total)
        if 'error' in item:
            failures.append({'index': index, 'kind': 'error', 'detail': _short(str(item['error']))})
            continue
        try:
            actual = json.loads(item['value'])
        except (KeyError, TypeError, ValueError):
            return _result('crashed', total)
        if strict_equal(actual, test['expected']):
            passed += 1
        else:
            failures.append({'index': index, 'kind': 'wrong', 'detail': _short(
                'expected ' + json.dumps(test['expected'], ensure_ascii=False)
                + ', got ' + json.dumps(actual, ensure_ascii=False))})
    timed_out = any(f['detail'] == 'timeout' for f in failures)
    return _result('timeout' if timed_out else 'ok', total, passed, failures)
