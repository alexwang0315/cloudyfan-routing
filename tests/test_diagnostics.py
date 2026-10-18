"""Public validation and bounded repair through Core.run; transport is synthetic."""
import json

from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.diagnostics import DiagnosticContract
from test_engine import API, TASK, core


def test_declared_public_diagnostic_reaches_only_one_repair(tmp_path):
    class PublicValidator(ArithmeticEnvironment):
        def validate(self, final):
            return {'passed': final == '2', 'code': 'integer_sum', 'message': 'Return one integer.'}

    api = API()
    base = core(tmp_path, api)
    profiles, decision = example_config()
    engine = Core(base.client, profiles, decision, diagnostic_contract=DiagnosticContract(
        version='sum-diagnostics-v1', source='public-sum-validator-v2',
        allowed_fields=('code', 'message'), max_bytes=512))
    result = engine.run('diagnostic', TASK, PublicValidator, contract_version='sum-v2')
    assert result['status'] == 'passed'
    assert len(result['attempts']) == 2
    public = result['attempts'][0]['public']
    assert public == {'passed': False, 'code': 'integer_sum', 'message': 'Return one integer.',
                      'diagnostic_version': 'sum-diagnostics-v1',
                      'diagnostic_source': 'public-sum-validator-v2'}
    requests = json.dumps(api.requests[1:])
    assert 'Return one integer.' in requests and 'public-sum-validator-v2' in requests
    assert len(api.requests) == 3


def test_nested_unknown_diagnostic_rejected_before_selector(tmp_path):
    import pytest

    class Nested(ArithmeticEnvironment):
        def validate(self, final):
            return {'passed': False, 'checks': [{'unregistered': 'UNPUBLISHED_FACT'}]}

    api = API()
    with pytest.raises(ValueError, match='scalar'):
        core(tmp_path, api).run('nested', TASK, Nested, contract_version='v1')
    assert len(api.requests) == 1
    assert 'UNPUBLISHED_FACT' not in json.dumps(api.requests)


def test_invalid_contract_cannot_authorize_diagnostics(tmp_path):
    import pytest

    with pytest.raises(ValueError, match='Diagnostic contract'):
        DiagnosticContract(version='', source='', allowed_fields=('passed',), max_bytes=0)


def test_external_effect_is_not_replayed_after_interruption(tmp_path):
    import pytest

    effects = []
    class External(ArithmeticEnvironment):
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'notify', 'parameters': {'type': 'object'}}}]
        def call(self, name, args):
            effects.append(name)
            raise OSError('effect occurred but acknowledgement lost')

    api = API(messages=[{'content': None, 'tool_calls': [{'id': 'effect', 'type': 'function',
                      'function': {'name': 'notify', 'arguments': '{}'}}]}])
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='Tool execution'):
        engine.run('external', TASK, External, contract_version='v1')
    with pytest.raises(RuntimeError, match='Tool replay blocked'):
        engine.run('external', TASK, External, contract_version='v1')
    assert effects == ['notify']
    assert len(api.requests) == 1


def test_private_tool_result_never_reaches_next_model_request(tmp_path):
    import pytest

    class Unsafe(ArithmeticEnvironment):
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {'type': 'object'}}}]
        def call(self, name, args):
            return {'ok': False, 'private_check': 'PRIVATE_CHECK_SENTINEL'}

    api = API(messages=[{'content': None, 'tool_calls': [{'id': 'read', 'type': 'function',
                      'function': {'name': 'inspect', 'arguments': '{}'}}]}, {'content': '2'}])
    with pytest.raises(ValueError, match='Private evaluation'):
        core(tmp_path, api).run('unsafe-tool', TASK, Unsafe, contract_version='v1')
    assert len(api.requests) == 1
    assert 'PRIVATE_CHECK_SENTINEL' not in json.dumps(api.requests)


def test_diagnostic_byte_limit_includes_utf8_and_provenance(tmp_path):
    import pytest

    class Large(ArithmeticEnvironment):
        def validate(self, final):
            return {'passed': False, 'checks': ['界' * 100]}

    api = API()
    base = core(tmp_path, api)
    profiles, decision = example_config()
    engine = Core(base.client, profiles, decision,
                  diagnostic_contract=DiagnosticContract(max_bytes=250))
    with pytest.raises(ValueError, match='byte limit'):
        engine.run('large', TASK, Large, contract_version='v1')
    assert len(api.requests) == 1


def test_diagnostic_schema_source_and_version_are_frozen_for_resume(tmp_path):
    from dataclasses import replace
    import pytest

    api = API(first='2')
    engine = core(tmp_path, api)
    original = engine.run('frozen-diagnostic', TASK, ArithmeticEnvironment, contract_version='v1')
    repeated = engine.run('frozen-diagnostic', TASK, ArithmeticEnvironment, contract_version='v1')
    assert original == repeated
    initial = engine.diagnostic_contract
    for changes in ({'source': 'different-producer'}, {'version': 'v2'},
                    {'allowed_fields': ('checks', 'message')}, {'max_bytes': 8192}):
        engine.diagnostic_contract = replace(initial, **changes)
        with pytest.raises(ValueError, match='Resume evidence changed'):
            engine.run('frozen-diagnostic', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.requests) == 1


def test_unknown_and_hidden_diagnostic_fields_stop_before_downstream_requests(tmp_path):
    import pytest

    for field in ('undeclared', 'hidden_answer', 'other_arm_answers', 'private_check'):
        class Unsafe(ArithmeticEnvironment):
            def validate(self, final):
                return {'passed': False, field: 'DO_NOT_SEND'}
        api = API()
        with pytest.raises(ValueError):
            core(tmp_path / field, api).run('hidden', TASK, Unsafe, contract_version='v1')
        assert len(api.requests) == 1
        assert 'DO_NOT_SEND' not in json.dumps(api.requests)


def test_tool_content_error_is_public_and_does_not_execute(tmp_path):
    class NoExecution(ArithmeticEnvironment):
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {'type': 'object'}}}]
        def call(self, name, args):
            raise AssertionError('Malformed arguments must not execute')

    api = API(messages=[{'content': None, 'tool_calls': [{'id': 'read', 'type': 'function',
                      'function': {'name': 'inspect', 'arguments': 'invalid-json'}}]}, {'content': '2'}])
    result = core(tmp_path, api).run('content-error', TASK, NoExecution, contract_version='v1')
    assert result['status'] == 'passed'
    assert result['actions'][0]['result']['error']['category'] == 'content'
    assert result['actions'][0]['result']['error']['code'] == 'invalid_arguments'


def test_completed_external_effect_is_not_replayed_when_validator_interrupts(tmp_path):
    import pytest

    effects = []
    class External(ArithmeticEnvironment):
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'notify', 'parameters': {'type': 'object'}}}]
        def call(self, name, args):
            effects.append(name)
            return {'ok': True}
        def validate(self, final):
            raise RuntimeError('validator interrupted')

    api = API(messages=[{'content': None, 'tool_calls': [{'id': 'effect', 'type': 'function',
                      'function': {'name': 'notify', 'arguments': '{}'}}]}, {'content': 'done'}])
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='validator interrupted'):
        engine.run('completed-effect', TASK, External, contract_version='v1')
    with pytest.raises(RuntimeError, match='Tool replay blocked'):
        engine.run('completed-effect', TASK, External, contract_version='v1')
    assert effects == ['notify']
    assert len(api.requests) == 2


def test_replay_permission_cannot_be_broadened_on_resume(tmp_path):
    import pytest

    class Interrupted(ArithmeticEnvironment):
        def validate(self, final):
            raise RuntimeError('validator interrupted')
    class Broadened(Interrupted):
        replayable_tools = frozenset({'notify'})

    api = API(first='2')
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='validator interrupted'):
        engine.run('replay-policy', TASK, Interrupted, contract_version='v1')
    with pytest.raises(ValueError, match='Resume evidence changed'):
        engine.run('replay-policy', TASK, Broadened, contract_version='v1')
    assert len(api.requests) == 1


def test_control_plane_rejection_preserves_application_status(tmp_path):
    import pytest

    class BudgetStop(RuntimeError):
        control_plane_error = True
    class Blocked(ArithmeticEnvironment):
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {'type': 'object'}}}]
        def call(self, name, args):
            raise BudgetStop('public budget stop')

    api = API(messages=[{'content': None, 'tool_calls': [{'id': 'read', 'type': 'function',
                      'function': {'name': 'inspect', 'arguments': '{}'}}]}])
    with pytest.raises(BudgetStop, match='public budget stop'):
        core(tmp_path, api).run('budget-stop', TASK, Blocked, contract_version='v1')
    assert len(api.requests) == 1
