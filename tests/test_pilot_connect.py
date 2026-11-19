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
        ('openai/gpt-oss-120b', 'passed', 1)]
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


def test_listing_endpoints_shows_each_provider_with_its_own_price():
    seen = []

    def api(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={'data': {'id': 'openai/gpt-oss-120b', 'endpoints': [
            {'provider_name': 'BaseTen', 'tag': 'baseten/fp4', 'quantization': 'fp4', 'context_length': 131072,
             'max_completion_tokens': 32768, 'pricing': {'prompt': '0.0000001', 'completion': '0.0000005'},
             'supported_parameters': ['tools', 'reasoning'], 'uptime_last_30m': 99.5, 'status': 0},
            {'provider_name': 'Other', 'tag': 'other', 'pricing': {'prompt': '0.0000002', 'completion': '0.0000006'}}]}})
    rows = pilot.list_endpoints('or-key', 'openai/gpt-oss-120b', transport=httpx.MockTransport(api))
    assert seen == ['https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints']
    assert rows[0] == {'provider_name': 'BaseTen', 'tag': 'baseten/fp4', 'quantization': 'fp4',
                       'context_length': 131072, 'max_completion_tokens': 32768, 'input_per_mtok': '0.1',
                       'output_per_mtok': '0.5', 'tools': True, 'reasoning': True, 'uptime_last_30m': 99.5,
                       'status': 0}
    assert rows[1]['provider_name'] == 'Other' and rows[1]['tools'] is False
    with pytest.raises(ValueError):
        pilot.list_endpoints('or-key', '../etc/passwd', transport=httpx.MockTransport(api))


class BusyOpenRouter:
    """第一個模型被上游以 429 拒絕（帶請求編號、核帳查不到）；第二個模型正常回答。"""
    def __init__(self):
        self.posts, self.gets = [], []

    def __call__(self, request):
        if request.method == 'GET':
            self.gets.append(str(request.url))
            return httpx.Response(404, json={'error': {'message': 'Generation not found', 'code': 404}})
        body = json.loads(request.content)
        self.posts.append(body['model'])
        if body['model'] == 'openai/gpt-oss-20b':
            return httpx.Response(429, headers={'x-generation-id': 'gen-1795075455-busy'}, json={
                'error': {'message': 'Provider returned error', 'code': 429, 'metadata': {
                    'raw': 'temporarily rate-limited upstream', 'provider_name': 'DeepInfra', 'is_byok': False,
                    'limit_source': 'upstream_provider_shared_pool'}},
                'openrouter_metadata': {'requested': 'openai/gpt-oss-20b', 'is_byok': False}})
        return httpx.Response(200, json={'id': 'gen-ok-1', 'model': body['model'], 'provider': 'Crusoe',
            'usage': {'cost': .0003, 'prompt_tokens': 20, 'completion_tokens': 3},
            'choices': [{'message': {'role': 'assistant', 'content': '2'}, 'finish_reason': 'stop'}]})


def test_upstream_429_is_recorded_as_policy_zero_fee_and_does_not_block_the_next_model(tmp_path):
    from cloudyfan_jev.rejection_fees import Text429FeePolicy
    source = tmp_path / 'openrouter-pricing.txt'
    source.write_text('No. ' + Text429FeePolicy.EXCERPT + ', so with fallback you pay only for the run that answers.')
    api = BusyOpenRouter()
    targets = [pilot.openrouter_target('openai/gpt-oss-20b', provider='deepinfra/bf16', provider_name='DeepInfra',
                                       input_per_mtok='0.03', output_per_mtok='0.14'),
               pilot.openrouter_target('openai/gpt-oss-120b', provider='crusoe/bf16', provider_name='Crusoe',
                                       input_per_mtok='0.05', output_per_mtok='0.25')]
    rows = pilot.connect(tmp_path / 'run', targets, openrouter_key='or-key', anthropic_key=None, cap_usd='1',
                         transports={'openrouter': httpx.MockTransport(api)}, rejection_policy_source=source)
    assert rows[0]['address'] == 'openai/gpt-oss-20b' and rows[0]['status'] != 'error'
    assert rows[0]['provider_rejected'] is True and rows[0]['cost_usd'] == '0'
    assert rows[0]['cost_basis'] == 'provider_error_no_charge_policy'
    assert rows[1]['cost_basis'] == 'response_usage_cost'
    assert rows[1]['status'] == 'passed' and rows[1]['cost_usd'] == '0.0003'
    report = json.loads((tmp_path / 'run' / 'connect-report.json').read_text())
    assert report['unknown_events'] == 0
    assert list((tmp_path / 'run' / 'receipts' / 'calls').glob('*/rejection-fee.json'))


def test_without_the_policy_an_upstream_429_still_stops_as_unknown_cost(tmp_path):
    api = BusyOpenRouter()
    rows = pilot.connect(tmp_path / 'run', [pilot.openrouter_target(
        'openai/gpt-oss-20b', provider='deepinfra/bf16', provider_name='DeepInfra',
        input_per_mtok='0.03', output_per_mtok='0.14')], openrouter_key='or-key', anthropic_key=None, cap_usd='1',
        transports={'openrouter': httpx.MockTransport(api)})
    assert rows[0]['status'] == 'error'


def test_calibration_runs_each_task_on_the_open_model_and_reports_pass_rates(tmp_path):
    from cloudyfan_jev import hard_tasks
    tasks = [hardtask for hardtask in (hard_tasks.make('A', 1, 0), hard_tasks.make('F', 1, 0))]

    class Solver:
        def __init__(self):
            self.requests = []

        def __call__(self, request):
            body = json.loads(request.content)
            self.requests.append(body)
            prompt = body['messages'][1]['content']
            task = next(t for t in tasks if t['prompt'] == prompt)
            answer = task['answer'] if task['family'] == 'A' else '0'
            return httpx.Response(200, json={'id': f'gen-cal-{len(self.requests)}', 'model': body['model'],
                'provider': 'Crusoe', 'usage': {'cost': .0002, 'prompt_tokens': 300, 'completion_tokens': 900},
                'choices': [{'message': {'role': 'assistant', 'content': 'work\nANSWER: ' + answer},
                             'finish_reason': 'stop'}]})
    api = Solver()
    target = pilot.openrouter_target('openai/gpt-oss-120b', provider='crusoe/bf16', provider_name='Crusoe',
                                     input_per_mtok='0.05', output_per_mtok='0.25')
    rows = pilot.calibrate(tmp_path / 'cal', tasks, target, openrouter_key='or-key', cap_usd='1',
                           transports={'openrouter': httpx.MockTransport(api)})
    assert [(r['id'], r['first_pass'], r['final_pass'], r['attempts']) for r in rows] == [
        (tasks[0]['id'], True, True, 1), (tasks[1]['id'], False, False, 2)]
    assert rows[1]['checks'] == ['answer_incorrect'] and rows[0]['cost_usd'] == '0.0002'
    assert api.requests[0]['messages'][0]['content'] == pilot.CALIBRATION_SYSTEM
    assert all(r['model'] == 'openai/gpt-oss-120b' for r in api.requests)
    report = json.loads((tmp_path / 'cal' / 'calibration-report.json').read_text())
    assert report['summary']['first_pass'] == 1 and report['summary']['tasks'] == 2
    assert report['by_family']['A'] == {'tasks': 1, 'first_pass': 1, 'final_pass': 1}
    # 標準答案不寫進報告。
    assert all('answer' not in row and 'prompt' not in row for row in report['rows'])
    assert tasks[1]['answer'] not in json.dumps(report)  # 細胞自動機的答案是一長串 0／1
    # 同一個資料夾重跑：讀舊收據，不重付。
    again = pilot.calibrate(tmp_path / 'cal', tasks, target, openrouter_key='or-key', cap_usd='1',
                            transports={'openrouter': httpx.MockTransport(api)})
    assert len(api.requests) == 3 and [r['first_pass'] for r in again] == [True, False]
