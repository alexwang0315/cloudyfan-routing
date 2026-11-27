import hashlib
import json
import subprocess
import sys

import pytest
from support import clean_env, free_port, start_server
from test_cli import cli, json_lines


class McpClient:
    """A minimal MCP client speaking JSON-RPC over the server's stdio."""

    def __init__(self, portal, name, save_dir):
        self.process = subprocess.Popen(
            [sys.executable, '-B', '-m', 'cloudyfami_portal', 'mcp', '--url', portal, '--name', name,
             '--save-dir', str(save_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env())
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.send({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params or {}})
        response = json.loads(self.process.stdout.readline())
        assert response['id'] == self.next_id
        return response

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def initialize(self):
        response = self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                               'clientInfo': {'name': 'test', 'version': '0'}})
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return response['result']

    def call(self, tool, **arguments):
        result = self.request('tools/call', {'name': tool, 'arguments': arguments})['result']
        assert result['isError'] is False, result['content'][0]['text']
        assert json.loads(result['content'][0]['text']) == result['structuredContent']
        return result['structuredContent']

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        return self.process.stderr.read()


@pytest.fixture
def portal(tmp_path):
    port = free_port()
    server = start_server(tmp_path / 'portal.sqlite3', port)
    yield f'http://127.0.0.1:{port}'
    server.kill()
    server.wait()


@pytest.fixture
def agent(portal, tmp_path):
    client = McpClient(portal, '甲', tmp_path / 'agent-inbox')
    client.initialize()
    yield client
    client.close()


def test_handshake_negotiates_a_version_and_lists_the_five_tools(portal, tmp_path):
    client = McpClient(portal, '甲', tmp_path)
    info = client.initialize()
    assert info['protocolVersion'] == '2025-06-18' and 'tools' in info['capabilities']
    assert info['serverInfo']['name'] == 'cloudyfami-portal'
    tools = client.request('tools/list')['result']['tools']
    assert {tool['name'] for tool in tools} == {
        'portal_send', 'portal_receive', 'portal_send_file', 'portal_status', 'portal_list_clients'}
    assert all(tool['inputSchema']['type'] == 'object' for tool in tools)
    assert client.request('ping')['result'] == {}
    client.close()


def test_an_unknown_protocol_version_gets_the_newest_supported_one(portal, tmp_path):
    client = McpClient(portal, '甲', tmp_path)
    response = client.request('initialize', {'protocolVersion': '1999-01-01', 'capabilities': {}})
    assert response['result']['protocolVersion'] == '2025-11-25'
    client.close()


def test_portal_send_reaches_a_command_line_listener(agent, portal):
    sent = agent.call('portal_send', recipient='乙', text='代理送出')
    [received] = json_lines(cli(portal, '乙', 'listen', '--count', '1', '--json', '--wait', '1').stdout)
    assert received['id'] == sent['id'] and received['text'] == '代理送出' and received['sender'] == '甲'
    status = agent.call('portal_status', id=sent['id'])
    assert status['state'] == 'acked'
    assert status == json_lines(cli(portal, '甲', 'status', sent['id'], '--json').stdout)[0]


def test_portal_receive_returns_what_the_command_line_listener_would_print(agent, portal, tmp_path):
    sent = json_lines(cli(portal, '乙', 'send', '甲', '命令列送出', '--wait-ack', '0', '--json').stdout)[0]
    [received] = agent.call('portal_receive', wait_seconds=2)['messages']
    assert received == {**received, 'id': sent['id'], 'sender': '乙', 'kind': 'text', 'text': '命令列送出'}
    assert agent.call('portal_receive')['messages'] == []
    assert json_lines(cli(portal, '乙', 'status', sent['id'], '--json').stdout)[0]['state'] == 'acked'

    json_lines(cli(portal, '乙', 'send', '乙', '自己給自己', '--wait-ack', '0', '--json').stdout)
    from_cli = json_lines(cli(portal, '乙', 'listen', '--count', '1', '--json', '--wait', '1').stdout)[0]
    cli(portal, '乙', 'send', '甲', '自己給自己', '--wait-ack', '0')
    from_mcp = agent.call('portal_receive', wait_seconds=2)['messages'][0]
    assert set(from_cli) == set(from_mcp)


def test_files_go_both_ways_between_an_agent_and_the_command_line(agent, portal, tmp_path):
    source = tmp_path / '雷達錄製.bin'
    source.write_bytes(bytes(range(256)) * 30_000)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    sent = agent.call('portal_send_file', recipient='乙', path=str(source))
    assert sent['sha256'] == digest and sent['name'] == '雷達錄製.bin'
    inbox = tmp_path / 'cli-inbox'
    [received] = json_lines(cli(portal, '乙', 'listen', '--count', '1', '--json', '--wait', '1',
                                '--save-dir', str(inbox)).stdout)
    assert hashlib.sha256((inbox / '雷達錄製.bin').read_bytes()).hexdigest() == digest == received['sha256']

    cli(portal, '乙', 'send-file', '甲', str(source))
    [incoming] = agent.call('portal_receive', wait_seconds=2)['messages']
    assert incoming['kind'] == 'transfer' and incoming['name'] == '雷達錄製.bin'
    with open(incoming['file'], 'rb') as saved:
        assert hashlib.sha256(saved.read()).hexdigest() == digest


def test_portal_list_clients_matches_the_command_line(agent, portal):
    agent.call('portal_send', recipient='乙', text='hi')
    from_mcp = agent.call('portal_list_clients')['clients']
    assert [(item['name'], item['online']) for item in from_mcp] == [('甲', True)]
    assert from_mcp == json_lines(cli(portal, '甲', 'clients', '--json').stdout)[0]['clients']


def test_two_agents_trade_a_message_each_way(portal, tmp_path):
    alice, bob = McpClient(portal, '甲', tmp_path / 'a'), McpClient(portal, '乙', tmp_path / 'b')
    alice.initialize()
    bob.initialize()
    question = alice.call('portal_send', recipient='乙', text='乙，你那邊幾點？')
    [got] = bob.call('portal_receive', wait_seconds=2)['messages']
    assert got['text'] == '乙，你那邊幾點？' and got['id'] == question['id']
    bob.call('portal_send', recipient='甲', text='晚上十點')
    [answer] = alice.call('portal_receive', wait_seconds=2)['messages']
    assert answer['sender'] == '乙' and answer['text'] == '晚上十點'
    assert alice.call('portal_status', id=question['id'])['state'] == 'acked'
    assert {item['name'] for item in alice.call('portal_list_clients')['clients']} == {'甲', '乙'}
    alice.close()
    bob.close()


def test_protocol_and_tool_errors_are_reported_the_way_mcp_expects(agent):
    assert agent.request('no/such/method')['error']['code'] == -32601
    assert agent.request('tools/call', {'name': 'no_such_tool'})['error']['code'] == -32602
    assert agent.request('tools/call', {'name': 'portal_send', 'arguments': {'text': 'x'}})['error']['code'] == -32602
    failed = agent.request('tools/call', {'name': 'portal_send', 'arguments': {'recipient': 'a/b', 'text': 'x'}})
    assert failed['result']['isError'] is True and 'recipient' in failed['result']['content'][0]['text']
    missing = agent.request('tools/call', {'name': 'portal_send_file',
                                           'arguments': {'recipient': '乙', 'path': '/no/such/file'}})
    assert missing['result']['isError'] is True
    agent.process.stdin.write('not json\n')
    agent.process.stdin.flush()
    assert json.loads(agent.process.stdout.readline())['error']['code'] == -32700


def test_stdout_carries_only_protocol_messages(portal, tmp_path):
    client = McpClient(f'http://127.0.0.1:{free_port()}', '甲', tmp_path)
    client.initialize()
    failed = client.request('tools/call', {'name': 'portal_list_clients', 'arguments': {}})
    assert failed['result']['isError'] is True
    stderr = client.close()
    assert 'portal_list_clients failed' in stderr
