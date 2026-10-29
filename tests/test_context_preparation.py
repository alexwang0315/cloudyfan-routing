"""#50：確定性擷取式準備；只整塊保留或省略，原 prompt 與必要區塊 bytes 及順序不變。"""
import copy

import pytest

from cloudyfan_jev.context_preparation import ContextError, _render, prepare, task_prompt


POLICY = {'version': 'extractive-v1', 'max_input_bytes': 4096, 'max_blocks': 8, 'max_block_bytes': 1024,
          'max_prepared_bytes': 400, 'framing_reserve_bytes': 50, 'output_reserve_bytes': 50,
          'allow_omit': True, 'token_guarantee': 'not_required', 'max_prepare_ms': 1000,
          'qualification': {'content_preparation_version': 'extractive-v1', 'scope': 'synthetic-only',
                            'synthetic': True, 'evidence_sha256': 'a' * 64}}


def _context(*blocks):
    return {'format': 'reference-blocks-v1', 'compression': 'extractive-v1',
            'blocks': [{'id': key, 'text': text, 'omittable': omit, 'priority': priority}
                       for key, text, omit, priority in blocks]}


def _policy(**changes):
    return {**copy.deepcopy(POLICY), **changes}


def test_required_content_keeps_exact_bytes_and_order_including_chinese_and_code():
    code = 'def add(a, b):\n    return a + b  # 1.50 "引文"\n'
    prepared = prepare('請回答：1+1？', _context(('zh', '中文參考：數字 3.14159 不可改。', False, 5),
                                              ('code', code, False, 1)), _policy(max_prepared_bytes=4000))
    assert prepared['content'] == ('請回答：1+1？' + '\n\n[參考區塊 zh]\n中文參考：數字 3.14159 不可改。'
                                   + '\n\n[參考區塊 code]\n' + code)
    assert [row['decision'] for row in prepared['blocks']] == ['kept', 'kept']
    assert {row['reason'] for row in prepared['blocks']} == {'required_by_request'}


def test_priority_then_arrival_order_and_a_large_block_is_skipped_for_later_blocks_that_fit():
    big = 'B' * 290
    context = _context(('a', 'a' * 40, True, 2), ('big', big, True, 0), ('c', 'c' * 40, True, 2),
                       ('d', 'd' * 40, True, 1))
    prepared = prepare('Q', context, _policy())
    decisions = {row['id']: (row['decision'], row['reason']) for row in prepared['blocks']}
    # 預算 299 bytes（400−50−50−prompt）：big 渲染後 311 bytes 放不下而略過；d（1）、a、c（2，依到達順序）都放得下。
    assert decisions['big'] == ('omitted', 'remaining_budget_insufficient')
    assert decisions['d'][0] == decisions['a'][0] == decisions['c'][0] == 'kept'
    kept = [row['id'] for row in prepared['blocks'] if row['decision'] == 'kept']
    assert prepared['content'] == 'Q' + ''.join(_render(b) for b in context['blocks'] if b['id'] in kept)
    assert kept == [b['id'] for b in context['blocks'] if b['id'] in kept], 'Final order must stay original'
    assert prepared['prepared_bytes'] <= prepared['budget_bytes']
    assert prepare('Q', context, _policy()) == prepared, 'Preparation must be deterministic'


def test_omittable_request_without_server_permission_stays_required():
    prepared = prepare('Q', _context(('a', 'x' * 30, True, 0)), _policy(allow_omit=False))
    assert prepared['blocks'][0]['decision'] == 'kept'
    assert prepared['blocks'][0]['reason'] == 'server_policy_disallows_omission'


@pytest.mark.parametrize('context,policy,code', [
    (_context(('a', 'x' * 500, False, 0)), {}, 'required_context_over_limit'),
    (_context(('a', 'x', True, 0)), {'token_guarantee': 'required'}, 'token_method_untrusted'),
    (_context(('a', 'x', True, 0)), {'qualification': {**POLICY['qualification'],
     'content_preparation_version': 'extractive-v0'}}, 'context_preparation_not_qualified'),
    ({**_context(('a', 'x', True, 0)), 'extra': 1}, {}, 'unsupported_context_fields'),
    ({**_context(('a', 'x', True, 0)), 'format': 'markdown'}, {}, 'unsupported_context_format'),
    ({**_context(('a', 'x', True, 0)), 'compression': 'llm-summary-v1'}, {}, 'context_preparation_version_not_registered'),
    (_context(('a', 'x', True, 0), ('a', 'y', True, 0)), {}, 'duplicate_context_block_id'),
    (_context(('../a', 'x', True, 0)), {}, 'invalid_context_block'),
    (_context(('a', 'x', True, 1001)), {}, 'invalid_context_block'),
    (_context(('a', 'x' * 2000, True, 0)), {}, 'context_block_too_large'),
    (_context(*[(f'b{i}', 'x', True, 0) for i in range(9)]), {}, 'context_block_count_invalid'),
    ({'format': 'reference-blocks-v1', 'compression': 'extractive-v1', 'blocks': [
        {'id': 'a', 'text': 'x', 'omittable': True, 'priority': 0, 'role': 'system'}]}, {}, 'invalid_context_block'),
], ids=['required-over-limit', 'token-untrusted', 'not-qualified', 'unknown-field', 'bad-format',
        'unregistered-version', 'duplicate-id', 'bad-id', 'bad-priority', 'block-too-large', 'too-many-blocks',
        'forged-role'])
def test_invalid_or_unsafe_input_is_rejected_before_any_dispatch(context, policy, code):
    with pytest.raises(ContextError) as error:
        prepare('Q', context, _policy(**policy))
    assert error.value.code == code


def test_task_prompt_uses_frozen_preparation_and_old_jobs_keep_their_prompt():
    assert task_prompt({'request': {'prompt': 'old'}}) == 'old'
    prepared = prepare('new', _context(('a', 'x', False, 0)), _policy())
    assert task_prompt({'request': {'prompt': 'new'}, 'prepared': prepared}) == prepared['content']


class _Clock:
    """可控時鐘：第一次呼叫為 0，之後跳到指定毫秒，模擬準備逾時。"""
    def __init__(self, elapsed_ms):
        self.values = [0.0, elapsed_ms / 1000]

    def __call__(self):
        return self.values.pop(0) if self.values else 0.0


def test_no_reduction_is_recorded_when_every_block_fits():
    prepared = prepare('Q', _context(('a', 'x' * 10, True, 0)), _policy())
    assert (prepared['outcome'], prepared['outcome_reason']) == ('no_reduction', 'all_blocks_fit')
    assert prepared['sent_bytes'] == prepared['original_bytes'] == prepared['prepared_bytes']
    assert prepared['token_estimate'] is None and prepared['token_method'] == 'unavailable'


def test_timeout_falls_back_to_the_original_only_when_allowed_and_it_fits():
    context = _context(('a', 'a' * 40, True, 0), ('b', 'b' * 40, True, 1))
    prepared = prepare('Q', context, _policy(fallback='original_if_fits', max_prepare_ms=5), clock=_Clock(10))
    assert (prepared['outcome'], prepared['outcome_reason']) == ('fallback_original', 'preparation_timeout')
    assert prepared['content'] == 'Q' + ''.join(_render(b) for b in context['blocks'])
    assert {row['reason'] for row in prepared['blocks']} == {'fallback_original'}


@pytest.mark.parametrize('policy,code', [
    ({'max_prepare_ms': 5}, 'context_preparation_timeout'),
    ({'max_prepare_ms': 5, 'fallback': 'original_if_fits', 'max_prepared_bytes': 260},
     'preparation_unavailable_original_over_limit'),
    ({'framing_reserve_bytes': 300, 'output_reserve_bytes': 100}, 'output_reserve_exceeds_context'),
    ({'fallback': 'always'}, None),
], ids=['timeout-no-fallback', 'timeout-original-too-big', 'reserve-exceeds-context', 'unknown-fallback'])
def test_timeout_and_reserve_failures_stop_before_dispatch_with_distinct_reasons(policy, code):
    context = _context(('a', 'a' * 100, True, 0), ('b', 'b' * 100, True, 1))
    if code is None:
        with pytest.raises(ValueError, match='policy'):
            prepare('Q', context, _policy(**policy), clock=_Clock(10))
        return
    with pytest.raises(ContextError) as error:
        prepare('Q', context, _policy(**policy), clock=_Clock(10))
    assert error.value.code == code
