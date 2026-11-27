import hashlib
import json
import subprocess
import sys
import threading
import time

import pytest
from support import clean_env, free_port, start_server

from cloudyfami_portal import DeliveryPolicy, Relay
from cloudyfami_portal.cli import SettingsError, build_parser, client_settings
from cloudyfami_portal.http_link import HttpLink, PortalClient


@pytest.fixture
def portal(tmp_path):
    port = free_port()
    server = start_server(tmp_path / 'portal.sqlite3', port, '--ack-timeout', '1')
    yield f'http://127.0.0.1:{port}'
    server.kill()
    server.wait()


def cli(portal, name, *args, stdin=None, env=None, timeout=30):
    command = [sys.executable, '-B', '-m', 'cloudyfami_portal', args[0], '--url', portal, '--name', name, *args[1:]]
    return subprocess.run(command, check=False, input=stdin, capture_output=True, text=True, timeout=timeout,
                          env=clean_env(**(env or {})))


def json_lines(output):
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_send_and_listen_complete_a_round_trip_with_a_receipt(portal):
    listener = subprocess.Popen(
        [sys.executable, '-B', '-m', 'cloudyfami_portal', 'listen', '--url', portal, '--name', '乙',
         '--count', '1', '--json', '--wait', '1'], stdout=subprocess.PIPE, text=True, env=clean_env())
    sent = cli(portal, '甲', 'send', '乙', '你好', '--wait-ack', '10', '--json')
    assert sent.returncode == 0, sent.stderr
    [receipt] = json_lines(sent.stdout)
    assert receipt['state'] == 'acked'
    output, _ = listener.communicate(timeout=10)
    assert listener.returncode == 0
    [received] = json_lines(output)
    assert received == {**received, 'id': receipt['id'], 'sender': '甲', 'kind': 'text', 'text': '你好'}


def test_send_reads_the_text_from_stdin_when_none_is_given(portal):
    sent = cli(portal, '甲', 'send', '乙', '--wait-ack', '0', stdin='從標準輸入來的訊息')
    assert sent.returncode == 0, sent.stderr
    listened = cli(portal, '乙', 'listen', '--count', '1', '--json', '--wait', '1')
    assert json_lines(listened.stdout)[0]['text'] == '從標準輸入來的訊息'


def test_a_file_sent_from_the_command_line_is_saved_by_the_listener(portal, tmp_path):
    source = tmp_path / '錄製.bin'
    source.write_bytes(bytes(range(256)) * 20_000)
    sent = cli(portal, '甲', 'send-file', '乙', str(source), '--json')
    assert sent.returncode == 0, sent.stderr
    [record] = json_lines(sent.stdout)
    inbox = tmp_path / 'inbox'
    listened = cli(portal, '乙', 'listen', '--count', '1', '--json', '--wait', '1', '--save-dir', str(inbox))
    assert listened.returncode == 0, listened.stderr
    [received] = json_lines(listened.stdout)
    saved = inbox / '錄製.bin'
    assert received['file'] == str(saved) and received['kind'] == 'transfer'
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == record['sha256'] == received['sha256']


def test_status_and_log_show_the_delivery_history(portal):
    sent = json_lines(cli(portal, '甲', 'send', '乙', '紀錄', '--wait-ack', '0', '--json').stdout)[0]
    cli(portal, '乙', 'listen', '--count', '1', '--wait', '1')
    status = cli(portal, '甲', 'status', sent['id'], '--json')
    assert [step['state'] for step in json_lines(status.stdout)[0]['history']] == ['queued', 'delivered', 'acked']
    log = cli(portal, '甲', 'log')
    assert log.returncode == 0
    assert f'{sent["id"][:8]} 甲→乙 text' in log.stdout and 'delivered→acked [http]' in log.stdout


def test_exec_handler_gets_the_body_and_a_failure_leaves_the_message_unacknowledged(portal, tmp_path):
    first = json_lines(cli(portal, '甲', 'send', '乙', '交給處理程式', '--wait-ack', '0', '--json').stdout)[0]
    failing = cli(portal, '乙', 'listen', '--exec', 'exit 3', '--timeout', '2', '--wait', '1')
    assert failing.returncode == 0 and 'handler failed' in failing.stderr
    status = json_lines(cli(portal, '甲', 'status', first['id'], '--json').stdout)[0]
    assert status['state'] == 'delivered'

    out = tmp_path / 'handled.txt'
    handled = cli(portal, '乙', 'listen', '--count', '1', '--wait', '1',
                  '--exec', f'printf "%s " "$CLOUDYFAMI_SENDER" > {out}; cat >> {out}')
    assert handled.returncode == 0, handled.stderr
    assert out.read_text(encoding='utf-8') == '甲 交給處理程式'
    assert json_lines(cli(portal, '甲', 'status', first['id'], '--json').stdout)[0]['state'] == 'acked'


def test_listen_timeout_ends_on_time_even_during_a_long_poll(portal):
    started = time.monotonic()
    result = cli(portal, '乙', 'listen', '--timeout', '1.5', '--wait', '20')
    assert result.returncode == 0 and time.monotonic() - started < 6


def test_missing_settings_are_explained(tmp_path):
    result = subprocess.run([sys.executable, '-B', '-m', 'cloudyfami_portal', 'send', '乙', 'hi'],
                            check=False, capture_output=True, text=True, env=clean_env(HOME=str(tmp_path)))
    assert result.returncode != 0 and 'CLOUDYFAMI_PORTAL_URL' in result.stderr


def test_settings_come_from_arguments_then_environment_then_config_file(tmp_path, monkeypatch):
    config = tmp_path / 'client.toml'
    config.write_text('url = "http://from-file:1"\nname = "file-name"\n', encoding='utf-8')
    parser = build_parser()
    for key in ('CLOUDYFAMI_PORTAL_URL', 'CLOUDYFAMI_PORTAL_NAME', 'CLOUDYFAMI_PORTAL_CONFIG'):
        monkeypatch.delenv(key, raising=False)
    assert client_settings(parser.parse_args(['log', '--config', str(config)])) == ('http://from-file:1', 'file-name')
    monkeypatch.setenv('CLOUDYFAMI_PORTAL_NAME', 'env-name')
    assert client_settings(parser.parse_args(['log', '--config', str(config)])) == ('http://from-file:1', 'env-name')
    args = parser.parse_args(['log', '--config', str(config), '--name', 'arg-name', '--url', 'http://arg:2'])
    assert client_settings(args) == ('http://arg:2', 'arg-name')
    config.write_text('not toml', encoding='utf-8')
    with pytest.raises(SettingsError):
        client_settings(parser.parse_args(['log', '--config', str(config)]))


def test_listener_survives_the_portal_going_away_without_missing_or_repeating(tmp_path):
    relay = Relay(tmp_path / 'portal.sqlite3', policy=DeliveryPolicy(ack_timeout=0.5))
    port = free_port()
    link = HttpLink(relay, port=port).start()
    url = f'http://127.0.0.1:{port}'
    alice = PortalClient(url, '甲')
    bob = PortalClient(url, '乙', retry_delays=())
    seen, stop, lost_receipt = [], threading.Event(), threading.Event()

    def handle(envelope):
        seen.append(envelope.body.decode('utf-8'))
        if envelope.body == b'3':
            link.stop()
            lost_receipt.set()

    listener = threading.Thread(target=bob.listen, args=(handle,),
                                kwargs={'stop': stop, 'wait': 0.5, 'backoff': (0.1,)})
    listener.start()
    try:
        for n in (1, 2, 3):
            alice.send('乙', str(n))
        assert lost_receipt.wait(10)
        time.sleep(0.5)
        link = HttpLink(relay, port=port).start()
        for n in (4, 5, 6):
            alice.send('乙', str(n))
        deadline = time.monotonic() + 10
        while len(seen) < 6 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(1.5)
        assert seen == ['1', '2', '3', '4', '5', '6']
        assert sum(item['to'] == 'acked' for item in alice.transmissions()) == 6
    finally:
        stop.set()
        listener.join(timeout=5)
        link.stop()
        relay.close()
