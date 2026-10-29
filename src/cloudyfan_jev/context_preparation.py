"""明示選用的參考區塊與確定性擷取式準備（#50）。

只做整塊保留或省略：沒有 LLM 摘要、embedding、改寫、工具或來源請求。原 prompt
永遠受保護；可省略標記只在伺服器政策也允許時有效。準備在付費前完成並凍結，
必要內容超限或必要的 token 保障沒有可信方法時，直接拒絕，不靜默截斷。
"""
import hashlib
import json
import re
import time


FORMAT = 'reference-blocks-v1'
VERSIONS = ('extractive-v1',)
MEASURE = 'utf8_bytes_of_user_content'
POLICY_FIELDS = {'version', 'max_input_bytes', 'max_blocks', 'max_block_bytes', 'max_prepared_bytes',
                 'framing_reserve_bytes', 'output_reserve_bytes', 'allow_omit', 'token_guarantee',
                 'max_prepare_ms', 'qualification'}
QUALIFICATION_FIELDS = {'content_preparation_version', 'scope', 'synthetic', 'evidence_sha256'}
BLOCK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
_now = time.perf_counter  # 準備耗時用的時鐘；主機測試可替換以驗證逾時與回退


class ContextError(ValueError):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.code, self.status = code, status


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _bytes(text):
    return len(text.encode())


def _positive(value):
    return type(value) is int and value > 0


def validate_policy(policy):
    """伺服器端政策；欄位、上限與資格範圍都必須明確。"""
    if (not isinstance(policy, dict) or set(policy) - {'fallback'} != POLICY_FIELDS or policy['version'] not in VERSIONS
            or policy.get('fallback', 'reject') not in ('reject', 'original_if_fits')
            or not all(_positive(policy[key]) for key in ('max_input_bytes', 'max_blocks', 'max_block_bytes',
                                                          'max_prepared_bytes', 'max_prepare_ms'))
            or not all(type(policy[key]) is int and policy[key] >= 0
                       for key in ('framing_reserve_bytes', 'output_reserve_bytes'))
            or type(policy['allow_omit']) is not bool
            or policy['token_guarantee'] not in ('not_required', 'required')):
        raise ValueError('Explicit bounded context preparation policy required')
    qualification = policy['qualification']
    if (not isinstance(qualification, dict) or set(qualification) != QUALIFICATION_FIELDS
            or not isinstance(qualification['scope'], str) or not qualification['scope'].strip()
            or type(qualification['synthetic']) is not bool
            or not isinstance(qualification['evidence_sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', qualification['evidence_sha256'])):
        raise ValueError('Context preparation qualification must be explicit')
    return policy


def _render(block):
    # 固定框架：區塊識別獨立一行，內容原 bytes 不改；不在區塊內截斷。
    return '\n\n[參考區塊 ' + block['id'] + ']\n' + block['text']


def _validate_context(prompt, context, policy):
    if not isinstance(context, dict) or set(context) != {'format', 'compression', 'blocks'}:
        raise ContextError('unsupported_context_fields')
    if context['format'] != FORMAT:
        raise ContextError('unsupported_context_format')
    if context['compression'] != policy['version']:
        raise ContextError('context_preparation_version_not_registered', 409)
    blocks = context['blocks']
    if not isinstance(blocks, list) or not blocks or len(blocks) > policy['max_blocks']:
        raise ContextError('context_block_count_invalid')
    seen, total = set(), _bytes(prompt)
    for block in blocks:
        if (not isinstance(block, dict) or set(block) != {'id', 'text', 'omittable', 'priority'}
                or not isinstance(block['id'], str) or BLOCK_ID.fullmatch(block['id']) is None
                or not isinstance(block['text'], str) or not block['text'].strip()
                or type(block['omittable']) is not bool
                or type(block['priority']) is not int or not 0 <= block['priority'] <= 1000):
            raise ContextError('invalid_context_block')
        if block['id'] in seen:
            raise ContextError('duplicate_context_block_id')
        seen.add(block['id'])
        if _bytes(block['text']) > policy['max_block_bytes']:
            raise ContextError('context_block_too_large', 413)
        total += _bytes(block['text'])
    if total > policy['max_input_bytes']:
        raise ContextError('context_input_too_large', 413)
    return blocks


def prepare(prompt, context, policy, *, clock=None):
    """回傳凍結用的準備結果；任何必要條件不成立就在付費前拒絕。"""
    validate_policy(policy)
    clock = clock or _now
    started = clock()
    if not isinstance(prompt, str) or not prompt.strip():
        raise ContextError('invalid_text_prompt')
    if policy['qualification']['content_preparation_version'] != policy['version']:
        # 未壓縮的資格、耗時或校準不能自動證明這個內容準備版本。
        raise ContextError('context_preparation_not_qualified', 409)
    if policy['token_guarantee'] == 'required':
        # 第一版沒有綁定 tokenizer 與精確模型的可信計數方法；不用字元平均冒充硬限制。
        raise ContextError('token_method_untrusted', 409)
    blocks = _validate_context(prompt, context, policy)
    budget = policy['max_prepared_bytes'] - policy['framing_reserve_bytes'] - policy['output_reserve_bytes']
    if budget <= 0:
        # 框架與輸出預留已經吃掉整個上下文；與「必要內容超限」分開。
        raise ContextError('output_reserve_exceeds_context', 413)
    rows = []
    for index, block in enumerate(blocks):
        omittable = block['omittable'] and policy['allow_omit']
        rows.append({'index': index, 'id': block['id'], 'text_sha256': _sha(block['text']),
                     'bytes': _bytes(_render(block)), 'priority': block['priority'],
                     'requested_omittable': block['omittable'], 'omittable': omittable,
                     'decision': None, 'reason': None})
    required = _bytes(prompt) + sum(row['bytes'] for row in rows if not row['omittable'])
    if required > budget:
        raise ContextError('required_context_over_limit', 413)
    for row in rows:
        if not row['omittable']:
            row.update(decision='kept', reason='required_by_request' if not row['requested_omittable']
                       else 'server_policy_disallows_omission')
    remaining = budget - required
    # 固定優先序（數字小者先），同優先序依到達順序；放不下就略過，繼續看後面能容納的。
    for row in sorted((r for r in rows if r['omittable']), key=lambda r: (r['priority'], r['index'])):
        if row['bytes'] <= remaining:
            row.update(decision='kept', reason='fits_in_priority_order')
            remaining -= row['bytes']
        else:
            row.update(decision='omitted', reason='remaining_budget_insufficient')
    content = prompt + ''.join(_render(block) for block, row in zip(blocks, rows) if row['decision'] == 'kept')
    original_content = prompt + ''.join(_render(block) for block in blocks)
    original = _bytes(original_content)
    outcome, outcome_reason = 'prepared', 'whole_blocks_selected'
    if all(row['decision'] == 'kept' for row in rows):
        outcome, outcome_reason = 'no_reduction', 'all_blocks_fit'
    elapsed = (clock() - started) * 1000
    if elapsed > policy['max_prepare_ms']:
        # 準備逾時：只有政策明示允許、原完整輸入也放得下時，才在付費前回退原文。
        if policy.get('fallback', 'reject') != 'original_if_fits':
            raise ContextError('context_preparation_timeout', 503)
        if original > budget:
            raise ContextError('preparation_unavailable_original_over_limit', 413)
        content, outcome, outcome_reason = original_content, 'fallback_original', 'preparation_timeout'
        for row in rows:
            row.update(decision='kept', reason='fallback_original')
    return {'schema_version': 'context-preparation-v1', 'version': policy['version'], 'format': FORMAT,
            'measure': MEASURE, 'budget_bytes': budget, 'original_bytes': original,
            'prepared_bytes': _bytes(content), 'prompt_sha256': _sha(prompt), 'content_sha256': _sha(content),
            'outcome': outcome, 'outcome_reason': outcome_reason, 'sent_bytes': _bytes(content),
            # 第一版沒有綁定精確 tokenizer 的本機計數；保持未知，不用字元平均估值。
            'token_estimate': None, 'token_method': 'unavailable',
            'fallback_policy': policy.get('fallback', 'reject'),
            'blocks': rows, 'qualification': dict(policy['qualification']),
            'policy_sha256': hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
            'content': content}


def task_prompt(job):
    """實際派送的使用者內容：有凍結準備結果就用它，否則是原 prompt。"""
    prepared = job.get('prepared')
    return prepared['content'] if isinstance(prepared, dict) else job['request']['prompt']
