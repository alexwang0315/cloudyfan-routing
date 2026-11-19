"""階段 0 連線驗證腳本（#81 試跑）：全部離線，兩家廠商都只接 httpx.MockTransport。"""
import json

import httpx
import pytest

from cloudyfan_jev import pilot
from test_vendors import AnthropicAPI


class OpenRouterAPI:
    def __init__(self, answer='2'):
        self.answer, self.requests = answer, []

    def __call__(self, request):
        self.requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [
                {'id': 'openai/gpt-oss-120b', 'context_length': 131072,
                 'pricing': {'prompt': '0.0000002', 'completion': '0.0000006'}},
                {'id': 'anthropic/claude-x', 'context_length': 1000000,
                 'pricing': {'prompt': '0.000004', 'completion': '0.00002'}}]})
        body = json.loads(request.content)
        return httpx.Response(200, json={'id': f'gen-or-{len(self.requests)}', 'model': body['model'],
            'provider': 'BaseTen', 'usage': {'cost': .0003, 'prompt_tokens': 20, 'completion_tokens': 3},
            'choices': [{'message': {'role': 'assistant', 'content': self.answer}, 'finish_reason': 'stop'}]})


def keyfile(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(value + '\n')
    return path


def test_key_file_must_be_a_single_line_regular_file(tmp_path):
    assert pilot.read_key(keyfile(tmp_path, 'a.key', 'secret-1')) == 'secret-1'
    bad = tmp_path / 'two.key'
    bad.write_text('one\ntwo\n')
    with pytest.raises(ValueError):
        pilot.read_key(bad)
    (tmp_path / 'link.key').symlink_to(tmp_path / 'a.key')
    with pytest.raises(ValueError):
        pilot.read_key(tmp_path / 'link.key')
    with pytest.raises(ValueError):
        pilot.read_key(tmp_path / 'missing.key')


def test_listing_open_models_is_a_free_catalog_read(tmp_path):
    api = OpenRouterAPI()
    rows = pilot.list_models('or-key', match=['gpt-oss'], transport=httpx.MockTransport(api))
    assert rows == [{'id': 'openai/gpt-oss-120b', 'context_length': 131072,
                     'input_per_mtok': '0.2', 'output_per_mtok': '0.6'}]
    assert [r.method for r in api.requests] == ['GET']


def test_connect_runs_one_tiny_task_per_model_through_the_full_stack(tmp_path):
    anthropic, openrouter = AnthropicAPI('2'), OpenRouterAPI('2')
    targets = [pilot.anthropic_target('claude-haiku-5-5'), pilot.anthropic_target('claude-fable-5-2'),
               pilot.openrouter_target('openai/gpt-oss-120b', provider='baseten/fp4', provider_name='BaseTen',
                                       input_per_mtok='0.2', output_per_mtok='0.6')]
    rows = pilot.connect(tmp_path / 'run', targets, openrouter_key='or-key', anthropic_key='an-key', cap_usd='1',
                         transports={'openrouter': httpx.MockTransport(openrouter),
                                     'anthropic': httpx.MockTransport(anthropic)})
    assert [(r['address'], r['status'], r['calls']) for r in rows] == [
        ('anthropic:claude-haiku-5-5', 'passed', 1), ('anthropic:claude-fable-5-2', 'passed', 1),
        ('openrouter:openai/gpt-oss-120b', 'passed', 1)]
    assert rows[1]['cost_usd'] == '0.016' and rows[1]['cost_basis'] == 'computed_from_vendor_token_usage'
    assert rows[2]['cost_usd'] == '0.0003' and rows[2]['provider'] == 'BaseTen'
    assert rows[0]['input_tokens'] == 1000 and rows[0]['output_tokens'] == 200
    # 兩把金鑰各走各的。
    assert all(r.headers['x-api-key'] == 'an-key' and 'authorization' not in r.headers for r in anthropic.requests)
    assert all(r.headers['authorization'] == 'Bearer or-key' for r in openrouter.requests)
    assert json.loads(openrouter.requests[0].content)['model'] == 'openai/gpt-oss-120b'
    report = json.loads((tmp_path / 'run' / 'connect-report.json').read_text())
    assert report['total_known_usd'] == '0.0165' and report['cap_usd'] == '1'
    assert 'an-key' not in json.dumps(report) and 'or-key' not in json.dumps(report)


def test_connect_stops_at_the_stage_cap_before_sending(tmp_path):
    anthropic = AnthropicAPI('2')
    rows = pilot.connect(tmp_path / 'run', [pilot.anthropic_target('claude-fable-5-2')],
                         openrouter_key=None, anthropic_key='an-key', cap_usd='0.01',
                         transports={'openrouter': httpx.MockTransport(OpenRouterAPI()),
                                     'anthropic': httpx.MockTransport(anthropic)})
    assert rows[0]['status'] == 'not_run' and 'budget' in rows[0]['reason'].lower()
    assert anthropic.requests == []


def test_connect_reports_a_vendor_error_without_hiding_it(tmp_path):
    anthropic = AnthropicAPI(status=529)
    rows = pilot.connect(tmp_path / 'run', [pilot.anthropic_target('claude-haiku-5-5')],
                         openrouter_key=None, anthropic_key='an-key', cap_usd='1',
                         transports={'openrouter': httpx.MockTransport(OpenRouterAPI()),
                                     'anthropic': httpx.MockTransport(anthropic)})
    assert rows[0]['status'] == 'error' and len(anthropic.requests) == 1


def test_openrouter_target_needs_a_key_and_missing_keys_are_refused(tmp_path):
    with pytest.raises(ValueError, match='OpenRouter'):
        pilot.connect(tmp_path / 'run', [pilot.openrouter_target('a/b', provider='p', provider_name='P',
                                                                 input_per_mtok='1', output_per_mtok='1')],
                      openrouter_key=None, anthropic_key='an-key', cap_usd='1')
    with pytest.raises(ValueError, match='Anthropic'):
        pilot.connect(tmp_path / 'run2', [pilot.anthropic_target('claude-haiku-5-5')],
                      openrouter_key='or-key', anthropic_key=None, cap_usd='1')


def test_env_file_yields_only_the_named_variable(tmp_path):
    env = tmp_path / '.env'
    env.write_text('# comment\nOTHER_SECRET=do-not-touch\nexport ANTHROPIC_API_KEY="sk-test-123"\n\nTHIRD=x\n')
    assert pilot.read_env_key(env, 'ANTHROPIC_API_KEY') == 'sk-test-123'
    env.write_text("ANTHROPIC_API_KEY='sk-single'\n")
    assert pilot.read_env_key(env, 'ANTHROPIC_API_KEY') == 'sk-single'
    env.write_text('ANTHROPIC_API_KEY=plain # trailing\n')
    assert pilot.read_env_key(env, 'ANTHROPIC_API_KEY') == 'plain'
    with pytest.raises(ValueError, match='not found') as missing:
        pilot.read_env_key(env, 'OPENROUTER_API_KEY')
    assert 'plain' not in str(missing.value)            # 錯誤訊息不帶任何內容
    env.write_text('ANTHROPIC_API_KEY=a\nANTHROPIC_API_KEY=b\n')
    with pytest.raises(ValueError, match='more than once'):
        pilot.read_env_key(env, 'ANTHROPIC_API_KEY')
    env.write_text('ANTHROPIC_API_KEY=\n')
    with pytest.raises(ValueError, match='empty'):
        pilot.read_env_key(env, 'ANTHROPIC_API_KEY')
    with pytest.raises(ValueError):
        pilot.read_env_key(env, 'bad name')


def test_command_line_key_can_come_from_a_key_file_or_one_env_variable(tmp_path):
    import argparse
    env = tmp_path / '.env'
    env.write_text('OPENROUTER_API_KEY=or-from-env\nCLAUDE_API_KEY=an-from-env\n')
    args = argparse.Namespace(openrouter_key_file=None, openrouter_env_file=env, openrouter_env_name='OPENROUTER_API_KEY')
    assert pilot.key_from_args(args, 'openrouter') == 'or-from-env'
    args = argparse.Namespace(openrouter_key_file=keyfile(tmp_path, 'k', 'or-file'), openrouter_env_file=None,
                              openrouter_env_name='OPENROUTER_API_KEY')
    assert pilot.key_from_args(args, 'openrouter') == 'or-file'
    args = argparse.Namespace(openrouter_key_file=None, openrouter_env_file=None, openrouter_env_name='X')
    assert pilot.key_from_args(args, 'openrouter') is None
