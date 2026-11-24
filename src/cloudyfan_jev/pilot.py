"""#81 真實試跑的組裝與「階段 0 連線驗證」。

這個模組會發出**真實、要付費**的請求，只在使用者當次授權後執行（指令列要加 ``--confirm-paid``）。
金鑰只從使用者指定的單行檔案讀取，交給連線層使用，不顯示、不寫入任何紀錄。

- ``list_models``：用 OpenRouter 金鑰讀模型目錄與價格（目錄查詢不收費）。
- ``connect``：每個模型各跑一題極短的算術題，走完整的堆疊（廠商路由、收據、身分核對、預算預留、
  牌價上界），回報狀態、token 用量與費用。用來確認轉接器的請求格式與費用計算對真實廠商成立。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import httpx

from .budget import Authorization, BudgetedCore, BudgetError, BudgetStore
from .client import Client
from .demo import ArithmeticEnvironment, example_config
from .engine import Core, Limits
from .pilot_bounds import ListPriceBounds
from .vendors import AnthropicVendor, VendorTransport

OPENROUTER_MODELS = 'https://openrouter.ai/api/v1/models'
TASK = {'prompt': 'Return only the integer sum of 1 and 1.'}
# 每百萬 token 牌價（2026-11-03 的資料）；開跑前請對照廠商公告，可用參數覆寫。
ANTHROPIC_PRICES = {
    'claude-haiku-5-5': {'input_per_mtok': '0.10', 'output_per_mtok': '0.50', 'long_context_tokens': 100000,
                         'long_input_per_mtok': '0.50', 'long_output_per_mtok': '2.50'},
    'claude-sonnet-5-5': {'input_per_mtok': '2', 'output_per_mtok': '10'},
    'claude-fable-5-2': {'input_per_mtok': '8', 'output_per_mtok': '40'},
    # 2026-11-23 加入：前沿模型直接作答的對照組（#81）。價格取自 claude-api 技能的模型表（2026-11-03 快取）。
    'claude-opus-5-5': {'input_per_mtok': '4', 'output_per_mtok': '20'},
}


def read_key(path):
    """讀使用者指定的金鑰檔：必須是一般檔案、剛好一行。內容不顯示、不落地。"""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Key file must be an existing regular file')
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError('Key file must contain exactly one line')
    return lines[0]


def read_env_key(path, name):
    """從使用者指定的 ``.env`` 檔只取出一個指定變數；其他變數不保留、不回傳，錯誤訊息不帶檔案內容。"""
    import re
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        raise ValueError('Invalid variable name')
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Env file must be an existing regular file')
    found = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        key, separator, value = line.partition('=')
        if not separator or key.strip() != name:
            continue
        value = value.strip()
        if value[:1] in ('"', "'"):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split(' #', 1)[0].strip()
        found.append(value)
    if not found:
        raise ValueError(name + ' not found in env file')
    if len(found) > 1:
        raise ValueError(name + ' appears more than once in env file')
    if not found[0]:
        raise ValueError(name + ' is empty in env file')
    return found[0]


def _per_mtok(value):
    return format((Decimal(str(value)) * 1000000).normalize(), 'f')


def list_models(openrouter_key, *, match=(), transport=None):
    """OpenRouter 模型目錄（只讀、不收費）；``match`` 是模型代號要包含的字串（任一符合即列出）。"""
    with httpx.Client(transport=transport, timeout=30, follow_redirects=False, trust_env=False) as http:
        response = http.get(OPENROUTER_MODELS, headers={'Authorization': 'Bearer ' + openrouter_key})
    response.raise_for_status()
    rows = []
    for item in response.json().get('data', []):
        pricing = item.get('pricing') or {}
        if match and not any(part in item.get('id', '') for part in match):
            continue
        if 'prompt' not in pricing or 'completion' not in pricing:
            continue
        rows.append({'id': item['id'], 'context_length': item.get('context_length'),
                     'input_per_mtok': _per_mtok(pricing['prompt']),
                     'output_per_mtok': _per_mtok(pricing['completion'])})
    return sorted(rows, key=lambda row: row['id'])


def list_endpoints(openrouter_key, model, *, transport=None):
    """某個模型在 OpenRouter 上的各家供應商、價格與量化（只讀、不收費）。"""
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.\-]+/[A-Za-z0-9_.:\-]+', model):
        raise ValueError('Invalid OpenRouter model id')
    with httpx.Client(transport=transport, timeout=30, follow_redirects=False, trust_env=False) as http:
        response = http.get(OPENROUTER_MODELS + '/' + model + '/endpoints',
                            headers={'Authorization': 'Bearer ' + openrouter_key})
    response.raise_for_status()
    rows = []
    for item in (response.json().get('data') or {}).get('endpoints', []):
        pricing, parameters = item.get('pricing') or {}, item.get('supported_parameters') or []
        rows.append({'provider_name': item.get('provider_name'), 'tag': item.get('tag'),
                     'quantization': item.get('quantization'), 'context_length': item.get('context_length'),
                     'max_completion_tokens': item.get('max_completion_tokens'),
                     'input_per_mtok': _per_mtok(pricing['prompt']) if 'prompt' in pricing else None,
                     'output_per_mtok': _per_mtok(pricing['completion']) if 'completion' in pricing else None,
                     'tools': 'tools' in parameters, 'reasoning': 'reasoning' in parameters,
                     'uptime_last_30m': item.get('uptime_last_30m'), 'status': item.get('status')})
    return rows


def anthropic_target(model, *, effort='low', prices=None):
    price = (prices or ANTHROPIC_PRICES).get(model)
    if price is None:
        raise ValueError('No frozen Anthropic price for ' + model)
    address = 'anthropic:' + model
    return {'vendor': 'anthropic', 'address': address, 'price': dict(price), 'vendor_model': model,
            'profile': {'model': address, 'model_version': address, 'accepted_models': [address],
                        'provider': 'anthropic', 'provider_name': 'Anthropic', 'effort': effort,
                        'capability_card': {}, 'external_score': None}}


def openrouter_target(model, *, provider, provider_name, input_per_mtok, output_per_mtok, effort='low'):
    # 不帶前綴就是 OpenRouter（ADR 0037）；既有的 429 零費政策以不帶前綴的名稱比對。
    address = model
    return {'vendor': 'openrouter', 'address': address, 'vendor_model': model,
            'price': {'input_per_mtok': str(input_per_mtok), 'output_per_mtok': str(output_per_mtok)},
            'profile': {'model': address, 'model_version': address, 'accepted_models': [model],
                        'provider': provider, 'provider_name': provider_name, 'effort': effort,
                        'capability_card': {}, 'external_score': None}}


def _row(target, result):
    calls = [call for attempt in result['attempts'] for call in attempt['calls']]
    usage = calls[-1].get('usage') or {} if calls else {}
    return {'address': target['address'], 'status': result['status'], 'calls': len(calls),
            'model': calls[-1].get('model') if calls else None,
            'provider': calls[-1].get('provider') if calls else None,
            'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
            'provider_rejected': any(call.get('provider_rejected') for call in calls),
            'cost_usd': result['costs']['api_usd'],
            # 費用來源：Anthropic 是計算值；OpenRouter 是回應金額、核帳金額或 429 政策零費。
            'cost_basis': usage.get('cost_basis') or (calls[-1].get('cost_source') if calls else None),
            'elapsed_ms': calls[-1].get('elapsed_ms') if calls else None}


def connect(root, targets, *, openrouter_key, anthropic_key, cap_usd, transports=None, max_tokens=2048,
            max_input_bytes=8192, anthropic_workspace_id=None, rejection_policy_source=None):
    """階段 0：每個目標各跑一題。任何一題費用未知時，預算庫會擋下後面的題（照既有規則）。"""
    root = Path(root)
    if any(t['vendor'] == 'openrouter' for t in targets) and not openrouter_key:
        raise ValueError('OpenRouter key required for openrouter targets')
    if any(t['vendor'] == 'anthropic' for t in targets) and not anthropic_key:
        raise ValueError('Anthropic key required for anthropic targets')
    transports = transports or {}
    vendors = {}
    if anthropic_key:
        prices = {t['vendor_model']: t['price'] for t in targets if t['vendor'] == 'anthropic'}
        vendors['anthropic'] = AnthropicVendor(anthropic_key, prices, transport=transports.get('anthropic'),
                                               workspace_id=anthropic_workspace_id)
    http = httpx.Client(transport=VendorTransport(transports.get('openrouter') or httpx.HTTPTransport(), vendors),
                        follow_redirects=False, trust_env=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 沒有 OpenRouter 目標時，Client 仍需要一個非空字串；它不會被送到任何地方。
    policy = None
    if rejection_policy_source is not None and any(t['vendor'] == 'openrouter' for t in targets):
        # 上游 429 且沒有答案時，依 OpenRouter 官方定價政策（存檔為證）記為 0 元；其他未知費用照舊停止。
        import hashlib
        from .rejection_fees import Text429FeePolicy
        source = Path(rejection_policy_source)
        policy = Text429FeePolicy(
            source, hashlib.sha256(source.read_bytes()).hexdigest(),
            [{'model': t['vendor_model'], 'provider': t['profile']['provider'],
              'provider_name': t['profile']['provider_name']} for t in targets if t['vendor'] == 'openrouter'],
            valid_until=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    client = Client(http, openrouter_key or 'no-openrouter-key-configured', root / 'receipts',
                    rejection_fee_policy=policy)
    store = BudgetStore(root / 'budget.sqlite')
    if store.report()['authorization'] is None:
        store.authorize(Authorization('pilot-stage-0-connect', Decimal(str(cap_usd)),
                                      (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    limits = Limits(max_turns=1, max_tokens=max_tokens, max_context_bytes=max_input_bytes)
    _, decision = example_config()
    rows = []
    for index, target in enumerate(targets):
        profiles = {tier: dict(target['profile']) for tier in ('open_source', 'low', 'mid', 'high')}
        verifier = ListPriceBounds({target['address']: target['price']}, max_input_bytes=max_input_bytes,
                                   max_output_tokens=max_tokens)
        runner = BudgetedCore(Core(client, profiles, decision, limits=limits), store,
                              model_bounds=verifier.model_bounds(), tool_bounds={}, verify_bound=verifier)
        try:
            result = runner.run(f'connect-{index:02d}', TASK, ArithmeticEnvironment,
                                contract_version='pilot-connect-v1', mode='fixed_high')
            rows.append(_row(target, result))
        except BudgetError as error:
            rows.append({'address': target['address'], 'status': 'not_run', 'reason': 'budget: ' + str(error)})
        except Exception as error:  # 照實回報，不重試
            rows.append({'address': target['address'], 'status': 'error',
                         'reason': type(error).__name__ + ': ' + str(error)})
    ledger = store.report()
    known = sum((Decimal(r['cost_usd']) for r in rows if r.get('cost_usd') is not None), Decimal(0))
    report = {'stage': 'connect', 'cap_usd': str(cap_usd), 'rows': rows,
              'total_known_usd': format(known.normalize(), 'f'),
              'unknown_events': sum(1 for event in ledger['events'] if event['cost'] is None)}
    (root / 'connect-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return rows


CALIBRATION_SYSTEM = ('You are solving a self-contained reasoning task. Everything you need is in the task. '
                      'Work it out carefully and follow the requested answer format exactly.')


def calibrate(root, tasks, target, *, openrouter_key=None, anthropic_key=None, cap_usd, transports=None,
              max_tokens=16000, max_input_bytes=16384, anthropic_workspace_id=None, rejection_policy_source=None,
              blind=False):
    """難度校準：每題用同一個模型作答；所有層都設成這個模型，所以答錯時的修正也由它自己做。
    回傳每題是否首答通過、最終是否通過、作答次數、檢查碼與費用；報告不含標準答案。"""
    from .hard_tasks import HardTaskEnvironment
    environment, contract_version = HardTaskEnvironment, 'hard-tasks-calibration-v1'
    if blind:
        # 盲測題的難度校準：只看公開範例作答與修正，事後以隱藏測試量「誤放行」比例。
        from .code_sandbox import sandbox_available
        from .code_tasks import BlindCodeEnvironment
        if not sandbox_available():
            raise RuntimeError('Blind code tasks require sandbox-exec; refusing before any paid request')
        environment, contract_version = BlindCodeEnvironment, 'code-tasks-blind-calibration-v1'
    root = Path(root)
    key = openrouter_key if target['vendor'] == 'openrouter' else anthropic_key
    if not key:
        raise ValueError(('OpenRouter' if target['vendor'] == 'openrouter' else 'Anthropic') + ' key required')
    transports = transports or {}
    vendors = {}
    if anthropic_key:
        vendors['anthropic'] = AnthropicVendor(anthropic_key, {target['vendor_model']: target['price']}
                                               if target['vendor'] == 'anthropic' else {},
                                               transport=transports.get('anthropic'),
                                               workspace_id=anthropic_workspace_id)
    http = httpx.Client(transport=VendorTransport(transports.get('openrouter') or httpx.HTTPTransport(), vendors),
                        follow_redirects=False, trust_env=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy = None
    if rejection_policy_source is not None and target['vendor'] == 'openrouter':
        import hashlib
        from .rejection_fees import Text429FeePolicy
        source = Path(rejection_policy_source)
        policy = Text429FeePolicy(source, hashlib.sha256(source.read_bytes()).hexdigest(),
                                  [{'model': target['vendor_model'], 'provider': target['profile']['provider'],
                                    'provider_name': target['profile']['provider_name']}],
                                  valid_until=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
    client = Client(http, openrouter_key or 'no-openrouter-key-configured', root / 'receipts',
                    rejection_fee_policy=policy)
    store = BudgetStore(root / 'budget.sqlite')
    if store.report()['authorization'] is None:
        store.authorize(Authorization('pilot-calibration', Decimal(str(cap_usd)),
                                      (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()))
    profiles = {tier: dict(target['profile']) for tier in ('open_source', 'low', 'mid', 'high')}
    _, decision = example_config()
    verifier = ListPriceBounds({target['address']: target['price']}, max_input_bytes=max_input_bytes,
                               max_output_tokens=max_tokens)
    core = Core(client, profiles, decision, system=CALIBRATION_SYSTEM,
                limits=Limits(max_turns=1, max_tokens=max_tokens, max_context_bytes=max_input_bytes))
    runner = BudgetedCore(core, store, model_bounds=verifier.model_bounds(), tool_bounds={}, verify_bound=verifier)
    rows = []
    for task in tasks:
        row = {'id': task['id'], 'family': task['family'], 'level': task['level']}
        try:
            result = runner.run('cal-' + task['id'], {'prompt': task['prompt']}, lambda t=task: environment(t),
                                contract_version=contract_version, mode='open_source_first')
            calls = [call for attempt in result['attempts'] for call in attempt['calls']]
            last = result['attempts'][-1] if result['attempts'] else {}
            row.update(status=result['status'], first_pass=result['first_public_pass'],
                       final_pass=result['final_public_pass'], attempts=len(result['attempts']),
                       checks=list((last.get('public') or {}).get('checks') or []),
                       output_tokens=sum((c.get('usage') or {}).get('completion_tokens') or 0 for c in calls),
                       provider_rejected=any(c.get('provider_rejected') for c in calls),
                       cost_usd=result['costs']['api_usd'], wall_seconds=result.get('wall_seconds'))
            if blind:
                row.update({k: v for k, v in _hidden_fields(result, task).items()
                            if k in ('hidden_by_attempt', 'public_by_attempt', 'hidden_first_pass',
                                     'hidden_final_pass', 'false_accept', 'hidden_counts')})
                row['first_false_accept'] = row['first_pass'] is True and row['hidden_first_pass'] is not True
        except BudgetError as error:
            row.update(status='not_run', reason='budget: ' + str(error))
        except Exception as error:  # 照實記錄，不重試
            row.update(status='error', reason=type(error).__name__ + ': ' + str(error)[:300])
        rows.append(row)
    done = [r for r in rows if 'first_pass' in r]
    by_family = {}
    for r in done:
        entry = by_family.setdefault(r['family'], {'tasks': 0, 'first_pass': 0, 'final_pass': 0})
        entry['tasks'] += 1
        entry['first_pass'] += r['first_pass'] is True
        entry['final_pass'] += r['final_pass'] is True
        if blind:
            for key in ('hidden_first_pass', 'first_false_accept'):
                entry[key] = entry.get(key, 0) + (r[key] is True)
    known = sum((Decimal(r['cost_usd']) for r in done if r.get('cost_usd') is not None), Decimal(0))
    report = {'stage': 'calibration', 'target': target['address'], 'cap_usd': str(cap_usd), 'rows': rows,
              'summary': {'tasks': len(done), 'first_pass': sum(r['first_pass'] is True for r in done),
                          **({'hidden_first_pass': sum(r['hidden_first_pass'] is True for r in done),
                              'first_false_accept': sum(r['first_false_accept'] is True for r in done)}
                             if blind else {}),
                          'final_pass': sum(r['final_pass'] is True for r in done),
                          'not_completed': len(rows) - len(done), 'known_usd': format(known.normalize(), 'f')},
              'by_family': by_family}
    (root / 'calibration-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return rows


class _ApproveEnvironment:
    """審查重播用：評審只回一次，不論內容都放行，避免格式不合時觸發修正（多花錢）。"""

    def tools_schema(self):
        return []

    def call(self, name, arguments):
        raise ValueError('Review replay has no tools')

    def validate(self, final_text):
        return {'passed': True, 'checks': []}


def _single_call_runner(root, target, system, label, *, anthropic_key, cap_usd, transports=None,
                        max_tokens=16000, max_input_bytes=24576, anthropic_workspace_id=None):
    """重播用：每題只送一次的 Anthropic 呼叫，照樣經過收據、預算庫與牌價上界。"""
    vendors = {'anthropic': AnthropicVendor(anthropic_key, {target['vendor_model']: target['price']},
                                            transport=(transports or {}).get('anthropic'),
                                            workspace_id=anthropic_workspace_id)}
    http = httpx.Client(transport=VendorTransport(httpx.HTTPTransport(), vendors),
                        follow_redirects=False, trust_env=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = Client(http, 'no-openrouter-key-configured', root / 'receipts')
    store = BudgetStore(root / 'budget.sqlite')
    if store.report()['authorization'] is None:
        store.authorize(Authorization(label, Decimal(str(cap_usd)),
                                      (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()))
    profiles = {tier: dict(target['profile']) for tier in ('open_source', 'low', 'mid', 'high')}
    _, decision = example_config()
    verifier = ListPriceBounds({target['address']: target['price']}, max_input_bytes=max_input_bytes,
                               max_output_tokens=max_tokens)
    core = Core(client, profiles, decision, system=system,
                limits=Limits(max_turns=1, max_tokens=max_tokens, max_context_bytes=max_input_bytes))
    return BudgetedCore(core, store, model_bounds=verifier.model_bounds(), tool_bounds={}, verify_bound=verifier)


def _single_call(runner, run_id, prompt, contract_version):
    """送一次；回傳回覆文字（供應商拒絕時為 None）與費用欄位。預算或其他錯誤照實寫進列裡，不重試。"""
    try:
        result = runner.run(run_id, {'prompt': prompt}, _ApproveEnvironment,
                            contract_version=contract_version, mode='fixed_high')
    except BudgetError as error:
        return None, {'status': 'not_run', 'outcome': 'not_run', 'reason': 'budget: ' + str(error)}
    except Exception as error:  # 照實記錄，不重試
        return None, {'status': 'error', 'outcome': 'error',
                      'reason': type(error).__name__ + ': ' + str(error)[:300]}
    calls = [c for a in result['attempts'] for c in a['calls']]
    rejected = any(c.get('provider_rejected') for c in calls)
    fields = {'status': result['status'], 'cost_usd': result['costs']['api_usd'],
              'output_tokens': sum((c.get('usage') or {}).get('completion_tokens') or 0 for c in calls)}
    if rejected:
        fields['outcome'] = 'rejected'
    return None if rejected else result['attempts'][-1]['final'], fields


def _calibration_rows(source_root):
    return {r['id']: r for r in json.loads((Path(source_root) / 'calibration-report.json').read_text())['rows']}


def review_replay(root, source_root, tasks, target, *, anthropic_key, cap_usd, **options):
    """離線量評審：把一次盲測校準的首答（已過公開驗收的）原樣交給路徑一加強版的評審，
    評審提示與訊息格式和 ``cloudyfan_plus.reviewed_attempt`` 相同。對照校準時量到的隱藏測試結果，
    算抓到幾題錯、誤報幾題對。不重跑首答。"""
    from .cloudyfan_plus import REVIEW_SYSTEM, parse_review
    from .engine import _encoded
    root, source_root = Path(root), Path(source_root)
    source = _calibration_rows(source_root)
    runner = _single_call_runner(root, target, REVIEW_SYSTEM, 'pilot-review-replay', anthropic_key=anthropic_key,
                                 cap_usd=cap_usd, **options)
    rows = []
    for task in tasks:
        first = json.loads((source_root / 'receipts' / 'runs' / ('cal-' + task['id']) / 'result.json')
                           .read_text())['attempts'][0]
        row = {'id': task['id'], 'family': task['family'], 'level': task['level'],
               'hidden_first_pass': source[task['id']]['hidden_first_pass']}
        if first['public'].get('passed') is not True:
            row.update(status='not_reviewed', outcome='not_reviewed')
            rows.append(row)
            continue
        prompt = _encoded({'task': task['prompt'], 'candidate': first['final']}).decode()
        text, fields = _single_call(runner, 'review-' + task['id'], prompt, 'cloudyfan-plus-review-replay-v1')
        row.update(fields)
        if 'outcome' not in row:
            verdict = parse_review(text)
            row['outcome'] = 'review_invalid' if verdict is None else 'approved' if verdict['ok'] else 'flagged'
            row['issue'] = (verdict or {}).get('issue', '')
        rows.append(row)
    reviewed = [r for r in rows if r['outcome'] in ('approved', 'flagged')]
    wrong = [r for r in reviewed if r['hidden_first_pass'] is not True]
    right = [r for r in reviewed if r['hidden_first_pass'] is True]
    known = sum((Decimal(r['cost_usd']) for r in rows if r.get('cost_usd') is not None), Decimal(0))
    report = {'stage': 'review_replay', 'source': str(source_root), 'target': target['address'],
              'effort': target['profile']['effort'], 'review_system': REVIEW_SYSTEM, 'cap_usd': str(cap_usd),
              'rows': rows,
              'summary': {'tasks': len(rows), 'reviewed': len(reviewed),
                          'wrong': len(wrong), 'caught': sum(r['outcome'] == 'flagged' for r in wrong),
                          'right': len(right), 'false_flags': sum(r['outcome'] == 'flagged' for r in right),
                          'not_completed': len(rows) - len(reviewed), 'known_usd': format(known.normalize(), 'f')}}
    (root / 'review-replay-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return rows


def replay_advisor_pool():
    """顧問重播的候選：盲測顧問池最前面加上校準用的最便宜首答 Haiku 5.5 low，其餘照 ``blind_setup``。"""
    haiku = anthropic_target('claude-haiku-5-5', effort='low')
    return {'haiku-low': {**haiku['profile'], 'tier': 'low', 'price': _price_note(haiku)},
            **blind_setup()['advisor_pool']}


def advisor_replay(root, answers, tasks, target, *, anthropic_key, cap_usd, pool=None, **options):
    """離線量顧問分派：每題把公開題目與候選清單交給只分派的顧問（提示與訊息格式同 ``advisor_flow``），
    記下它的第一順位。``answers`` 是 {設定: [校準資料夾...]}；第一順位有現成答案時，
    用該答案的隱藏測試結果與花費推算路徑二的結果，沒有時記為未量。"""
    from .advisor_flow import ADVISOR_SYSTEM_DELEGATE_ONLY, advisor_messages, parse_decision
    root = Path(root)
    pool = pool or replay_advisor_pool()
    known_answers = {setting: {k: v for d in dirs for k, v in _calibration_rows(d).items()
                               if 'hidden_first_pass' in v}
                     for setting, dirs in answers.items()}
    runner = _single_call_runner(root, target, ADVISOR_SYSTEM_DELEGATE_ONLY, 'pilot-advisor-replay',
                                 anthropic_key=anthropic_key, cap_usd=cap_usd, **options)
    rows = []
    for task in tasks:
        messages = advisor_messages({'prompt': task['prompt']}, pool, delegate_only=True)
        text, fields = _single_call(runner, 'advise-' + task['id'], messages[1]['content'],
                                    'advisor-first-replay-v1')
        row = {'id': task['id'], 'family': task['family'], 'level': task['level'], **fields}
        if 'outcome' not in row:
            decision = parse_decision(text, pool, delegate_only=True)
            row['outcome'] = decision['outcome']
            row['choices'] = [r['setting'] for r in decision['recommendations']]
            row['reasons'] = [r['reason'] for r in decision['recommendations']]
            first = row['choices'][0] if row['choices'] else None
            measured = known_answers.get(first, {}).get(task['id'])
            row['first'] = first
            row['answer_hidden_pass'] = None if measured is None else measured['hidden_first_pass']
            row['answer_cost_usd'] = None if measured is None else measured.get('cost_usd')
        rows.append(row)
    done = [r for r in rows if r['outcome'] == 'delegate']
    by_first = {}
    for r in done:
        by_first.setdefault(r['first'], {'tasks': 0, 'measured_right': 0, 'measured_wrong': 0, 'unmeasured': 0})
        entry = by_first[r['first']]
        entry['tasks'] += 1
        key = ('unmeasured' if r['answer_hidden_pass'] is None else
               'measured_right' if r['answer_hidden_pass'] else 'measured_wrong')
        entry[key] += 1
    known = sum((Decimal(r['cost_usd']) for r in rows if r.get('cost_usd') is not None), Decimal(0))
    report = {'stage': 'advisor_replay', 'target': target['address'], 'effort': target['profile']['effort'],
              'advisor_system': ADVISOR_SYSTEM_DELEGATE_ONLY, 'pool': pool,
              'answers': {k: [str(d) for d in v] for k, v in answers.items()}, 'cap_usd': str(cap_usd),
              'rows': rows,
              'summary': {'tasks': len(rows), 'delegated': len(done), 'by_first': by_first,
                          'not_completed': len(rows) - len(done), 'known_usd': format(known.normalize(), 'f')}}
    (root / 'advisor-replay-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                                 sort_keys=True))
    return rows


ARMS = ('cloudyfan', 'cloudyfan_plus', 'advisor_first')
# 對照組：每題直接交給高池（不換模型、一路用前沿模型）；答錯時同一高池再修一次。只在指定 --arm 時跑。
ALL_ARMS = ARMS + ('fixed_high',)
ARM_CODES = {'cloudyfan': 'p1', 'cloudyfan_plus': 'p1plus', 'advisor_first': 'p2', 'fixed_high': 'fh'}
# Jev 分類器（OpenRouter 的 decisions 端點）。沒有公開單價，上限檢查用保守的每次固定價，實際照回應記帳。
JEV = {'model': 'typesafe/jev-1.13', 'version': 'typesafe/jev-1.13-20260917', 'provider': 'typesafe',
       'provider_name': 'TypeSafe'}


def _policy_valid_until(root):
    """拒絕費政策的有效期限在資料夾第一次建立時寫下，之後沿用；
    否則程式重新啟動後設定描述不同，讀不回同一資料夾已完成的題（#81 compare-10）。"""
    marker = Path(root) / 'rejection-policy-valid-until.txt'
    if marker.exists():
        return marker.read_text().strip()
    value = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    marker.write_text(value + '\n')
    return value


def _role(target):
    return dict(target['profile'], open_source=True)


def _compare_runner(root, *, open_target, pool_targets, advisor_target, judge_target, decision,
                    decision_per_call_usd, openrouter_key, anthropic_key, ledger_exposure_usd, transports=None,
                    max_tokens=16000, max_input_bytes=24576, anthropic_workspace_id=None,
                    rejection_policy_source=None, max_turns=1, loop_guard_mode='shadow', tools=False,
                    advisor_pool=None):
    """組出比較用的 BudgetedCore（同一個 Core、收據與預算庫）。預算庫的授權是「帳本暫扣上限」，
    只用來容納未核帳呼叫的保守暫扣，不是要花的錢；實際花費上限由 ``compare`` 另外檢查。"""
    from .advisor import AdvisorConfig
    from .cloudyfan_plus import CloudyfanPlusConfig
    root = Path(root)
    transports = transports or {}
    # 首答層、顧問與裁判也可能走 Anthropic（盲測），價格要一併交給轉接層。
    anthropic_prices = {t['vendor_model']: t['price'] for t in (open_target, advisor_target, judge_target,
                                                                  *pool_targets.values()) if t['vendor'] == 'anthropic'}
    vendors = {'anthropic': AnthropicVendor(anthropic_key, anthropic_prices, transport=transports.get('anthropic'),
                                            workspace_id=anthropic_workspace_id)}
    http = httpx.Client(transport=VendorTransport(transports.get('openrouter') or httpx.HTTPTransport(), vendors),
                        follow_redirects=False, trust_env=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    openrouter_targets = [t for t in (open_target, advisor_target, judge_target, *pool_targets.values())
                          if t['vendor'] == 'openrouter']
    policy = None
    if rejection_policy_source is not None:
        import hashlib
        from .rejection_fees import Text429FeePolicy
        source = Path(rejection_policy_source)
        policy = Text429FeePolicy(source, hashlib.sha256(source.read_bytes()).hexdigest(),
                                  [{'model': t['vendor_model'], 'provider': t['profile']['provider'],
                                    'provider_name': t['profile']['provider_name']} for t in openrouter_targets],
                                  valid_until=_policy_valid_until(root))
    # 盲測的 xhigh 長答可能超過預設 180 秒；Anthropic 轉接層本身等 600 秒。
    client = Client(http, openrouter_key, root / 'receipts', rejection_fee_policy=policy,
                    chat_timeout_s=600 if advisor_pool is not None else 180)
    store = BudgetStore(root / 'budget.sqlite')
    if store.report()['authorization'] is None:
        store.authorize(Authorization('pilot-ledger-exposure-only', Decimal(str(ledger_exposure_usd)),
                                      (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()))
    profiles = {'open_source': dict(open_target['profile']),
                **{tier: dict(pool_targets[tier]['profile']) for tier in ('low', 'mid', 'high')}}
    from .loop_guard import LoopGuardConfig
    if advisor_pool is None:
        advisor = AdvisorConfig(advisor=_role(advisor_target), judge=_role(judge_target),
                                score_snapshot='pilot-81-20261119')
        plus = CloudyfanPlusConfig(judge=_role(judge_target), loop_guard=LoopGuardConfig(mode=loop_guard_mode))
    else:
        # 盲測（2026-11-24）：顧問只分派、可派 Sonnet，不用裁判與迴圈守衛；路徑一加強版只開交卷前評審。
        advisor = AdvisorConfig(advisor=_role(advisor_target), judge=_role(judge_target),
                                score_snapshot='blind-20261124', pool=advisor_pool, delegate_only=True,
                                judge_calls_per_attempt=0)
        plus = CloudyfanPlusConfig(judge=_role(judge_target), review=True)
    prices = {t['address']: t['price'] for t in (open_target, advisor_target, judge_target, *pool_targets.values())}
    prices[decision['version']] = {'per_call_usd': str(decision_per_call_usd)}
    verifier = ListPriceBounds(prices, max_input_bytes=max_input_bytes, max_output_tokens=max_tokens,
                               local_tools=('check_answer',) if tools else ())
    core = Core(client, profiles, dict(decision), system=CALIBRATION_SYSTEM,
                limits=Limits(max_turns=max_turns, max_tokens=max_tokens, max_context_bytes=max_input_bytes),
                advisor_first=advisor, cloudyfan_plus=plus)
    from .budget import EnforcedBound, ToolBound
    tool_bounds = ({'check_answer': ToolBound(EnforcedBound('0', 'local deterministic answer check'), external=False)}
                   if tools else {})
    return BudgetedCore(core, store, model_bounds=verifier.model_bounds(), tool_bounds=tool_bounds,
                        verify_bound=verifier)


def compare_worst_case_usd(*, max_tokens=16000, max_input_bytes=24576, max_turns=1, **setup):
    """每組跑一題的最壞花費（牌價 × token 上限），不發出任何請求。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        runner = _compare_runner(tmp, **setup, openrouter_key='offline', anthropic_key='offline',
                                 ledger_exposure_usd='1', max_tokens=max_tokens, max_input_bytes=max_input_bytes,
                                 max_turns=max_turns)
        return {arm: runner._admit(arm) for arm in ALL_ARMS}


def compare(root, tasks, *, cap_usd, openrouter_key, anthropic_key, ledger_exposure_usd='100', arms=ARMS,
            order_seed=0, tools=False, blind=False, **setup):
    """#81 三組比較：每題依輪替的順序在選定的組別各跑一次。

    ``cap_usd`` 是**實際花費上限**：每跑一題之前檢查「帳本已知花費＋這題最壞花費」，超過就不送出（記為
    ``spend_cap``）。``ledger_exposure_usd`` 只是預算庫的暫扣上限。同一個資料夾裡已完成的題直接讀回，不重付、
    不受上限影響。回傳每題每組一列；報告 ``compare-report.json`` 依組別統計，不含題目與標準答案。"""
    from .hard_tasks import HardTaskEnvironment, HardToolTaskEnvironment, tool_prompt
    environment = HardToolTaskEnvironment if tools else HardTaskEnvironment
    contract_version = 'hard-tasks-tools-compare-v1' if tools else 'hard-tasks-compare-v1'
    if blind:
        if tools:
            raise ValueError('Blind code tasks do not use the check tool')
        from .code_tasks import BlindCodeEnvironment
        from .code_sandbox import sandbox_available
        if not sandbox_available():
            # 沒有隔離就無法驗收，每題都會被當成失敗而升級、白花錢；在任何請求前拒跑。
            raise RuntimeError('Blind code tasks require sandbox-exec; refusing before any paid request')
        environment, contract_version = BlindCodeEnvironment, 'code-tasks-blind-v1'
    if not openrouter_key or not anthropic_key:
        raise ValueError('Both OpenRouter and Anthropic keys are required')
    unknown = set(arms) - set(ALL_ARMS)
    if unknown or not arms:
        raise ValueError('Unknown arms: ' + ', '.join(sorted(unknown)))
    root = Path(root)
    runner = _compare_runner(root, openrouter_key=openrouter_key, anthropic_key=anthropic_key,
                             ledger_exposure_usd=ledger_exposure_usd, tools=tools, **setup)
    spend_cap = Decimal(str(cap_usd))
    rows = []
    for index, task in enumerate(tasks):
        shift = (index + order_seed) % len(arms)
        for arm in arms[shift:] + arms[:shift]:
            run_id = f'{ARM_CODES[arm]}-{task["id"]}'
            row = {'task': task['id'], 'family': task['family'], 'level': task['level'], 'arm': arm}
            try:
                try:
                    result = runner.core.read_completed(run_id)
                except ValueError:
                    known = Decimal(runner.store.report()['known_usd'])
                    if known + runner._admit(arm) > spend_cap:
                        row.update(status='not_run', reason='spend_cap')
                        rows.append(row)
                        continue
                    prompt = tool_prompt(task) if tools else task['prompt']
                    result = runner.run(run_id, {'prompt': prompt}, lambda t=task: environment(t),
                                        contract_version=contract_version, mode=arm)
                row.update(_compare_row(result, task, blind))
            except BudgetError as error:
                row.update(status='not_run', reason='budget: ' + str(error))
            except Exception as error:  # 照實記錄，不重試
                row.update(status='error', reason=type(error).__name__ + ': ' + str(error)[:300])
            rows.append(row)
    _write_compare_report(root, rows, arms, cap_usd, ledger_exposure_usd, runner.store.report(), blind=blind)
    return rows


ROLE_TIERS = ('advisor', 'judge')
SONNET = 'claude-sonnet-5-5'


def _all_calls(result):
    """一題的全部呼叫（作答、Jev、顧問、迴圈守衛、評審），供三種成本重算。"""
    calls = [c for a in result['attempts'] for c in a['calls']]
    calls += [c for a in result['attempts'] for c in (a.get('loop_guard') or {}).get('calls', [])]
    calls += [c for a in result['attempts'] for c in (a.get('review') or {}).get('calls', [])]
    calls += (result.get('selection') or {}).get('calls', [])
    calls += (result.get('advisor') or {}).get('calls', [])
    return calls


def cost_views(result):
    """真實成本；假設一：顧問與裁判算 0；假設二：再加 Sonnet 算 0（不論首答或被分派）。
    有任何一筆金額未知時三者都回 None。"""
    real = result['costs'].get('api_usd')
    if real is None:
        return {'real_usd': None, 'roles_zero_usd': None, 'roles_sonnet_zero_usd': None}
    roles = sonnet = Decimal(0)
    for call in _all_calls(result):
        amount = Decimal(str(call.get('cost_usd') or 0))
        if call.get('tier') in ROLE_TIERS:
            roles += amount
        elif SONNET in str(call.get('model') or ''):
            sonnet += amount
    real = Decimal(real)
    return {'real_usd': str(real), 'roles_zero_usd': str(real - roles),
            'roles_sonnet_zero_usd': str(real - roles - sonnet)}


def _hidden_fields(result, task):
    """盲測事後計分：每次作答的程式碼跑隱藏測試；作答過程從未讀到這些結果。"""
    from .code_tasks import grade_hidden
    graded = [grade_hidden(task, a.get('final') or '') for a in result['attempts']]
    public = [a['public']['passed'] for a in result['attempts']]
    final_public = public[-1] if public else None
    hidden = [g['passed'] for g in graded]
    return {'hidden_by_attempt': hidden, 'public_by_attempt': public,
            'hidden_first_pass': hidden[0] if hidden else False,
            'hidden_final_pass': hidden[-1] if hidden else False,
            # 誤放行：最後交出的答案公開驗收（或評審）放行，隱藏測試沒過。
            'false_accept': final_public is True and not (hidden[-1] if hidden else False),
            # 錯殺：已經是對的答案卻沒被放行，又往上升級。
            'needless_escalations': sum(1 for i in range(len(hidden) - 1) if hidden[i] and public[i] is not True),
            'review_flags': sum(1 for a in result['attempts'] if (a.get('review') or {}).get('outcome') == 'flagged'),
            'hidden_counts': [[g['hidden_passed'], g['hidden_total']] for g in graded]}


def _compare_row(result, task=None, blind=False):
    calls = [c for a in result['attempts'] for c in a['calls']]
    advisor = (result.get('advisor') or {}).get('decision') or {}
    last = result['attempts'][-1] if result['attempts'] else {}
    return {'status': result['status'], 'first_pass': result['first_public_pass'],
            'final_pass': result['final_public_pass'],
            'tiers': [a['tier'] for a in result['attempts']],
            'classifier_calls': (result.get('selection') or {}).get('classifier_calls', 0),
            'advisor_outcome': advisor.get('outcome'), 'end_reason': result.get('end_reason'),
            'checks': list((last.get('public') or {}).get('checks') or []),
            'provider_rejected': any(c.get('provider_rejected') for c in calls),
            'research_usd': result['costs']['api_usd'],
            'formal_usd': result['costs']['hypothetical_open_source_zero_usd'],
            'wall_seconds': result.get('wall_seconds'),
            'stops': [a['stop'] for a in result['attempts']],
            'tool_calls': last.get('cumulative_actions', 0),
            **cost_views(result),
            **({'advisor_settings': [a.get('setting') for a in result['attempts']]}
               if result.get('advisor') else {}),
            **(_hidden_fields(result, task) if blind else {})}


def _write_compare_report(root, rows, arms, cap_usd, ledger_exposure_usd, ledger, blind=False):
    def total(items, key):
        return format(sum((Decimal(r[key]) for r in items if r.get(key) is not None), Decimal(0)).normalize(), 'f')
    summary = {}
    for arm in arms:
        done = [r for r in rows if r['arm'] == arm and 'final_pass' in r]
        tiers = {}
        for r in done:
            for tier in r['tiers']:
                tiers[tier] = tiers.get(tier, 0) + 1
        summary[arm] = {'tasks': len(done), 'not_completed': sum(1 for r in rows if r['arm'] == arm) - len(done),
                        'first_pass': sum(r['first_pass'] is True for r in done),
                        'final_pass': sum(r['final_pass'] is True for r in done),
                        'research_usd': total(done, 'research_usd'), 'formal_usd': total(done, 'formal_usd'),
                        'tier_counts': tiers, 'classifier_calls': sum(r['classifier_calls'] for r in done),
                        'advisor_answer': sum(r['advisor_outcome'] == 'answer' for r in done),
                        'advisor_delegate': sum(r['advisor_outcome'] == 'delegate' for r in done),
                        'advisor_incomplete': sum(r['advisor_outcome'] == 'incomplete' for r in done),
                        'provider_rejected': sum(r['provider_rejected'] for r in done),
                        'real_usd': total(done, 'real_usd'), 'roles_zero_usd': total(done, 'roles_zero_usd'),
                        'roles_sonnet_zero_usd': total(done, 'roles_sonnet_zero_usd')}
        if blind:
            summary[arm].update({
                'hidden_first_pass': sum(r['hidden_first_pass'] is True for r in done),
                'hidden_final_pass': sum(r['hidden_final_pass'] is True for r in done),
                'false_accept': sum(r['false_accept'] is True for r in done),
                'needless_escalations': sum(r['needless_escalations'] for r in done),
                'review_flags': sum(r['review_flags'] for r in done)})
    report = {'stage': 'blind-compare' if blind else 'compare', 'spend_cap_usd': str(cap_usd), 'ledger_exposure_usd': str(ledger_exposure_usd),
              'ledger': {k: ledger[k] for k in ('known_usd', 'verified_usd', 'provisional_hold_usd', 'unknown_events')},
              'arms': summary, 'rows': rows}
    (root / 'compare-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


FROZEN_TASKS = Path(__file__).resolve().parents[2] / 'docs/evidence/hard-tasks-50-20261119.json'


def pilot_setup():
    """2026-11-19 使用者核定的試跑組合（ADR 0037、#81）。價格為當日目錄與牌價，開跑前可覆寫。"""
    # 2026-11-20 使用者決定：路徑一的開源層與路徑二的顧問用同一個模型與供應商，比較才公平。
    # 第一次 50 題比較（compare-02～04）的開源層是 openai/gpt-oss-120b＠Crusoe（0.05／0.25）。
    # 顧問原用 qwen/qwen3.5-397b-a17b＠Alibaba；階段 1 試跑時被上游 429 拒絕，使用者決定換模型。
    # 2026-11-20 起 SiliconFlow 在約 3,200 token 截斷推理、不輸出答案（探測見 .scratch/pilot81/probe-20261120），
    # 使用者同意改用同模型的 Relace（fp4）；第一次 50 題的顧問結果是 SiliconFlow（fp8）11-19 的條件。
    shared = openrouter_target('deepseek/deepseek-v4-pro', provider='relace/fp4', provider_name='Relace',
                               input_per_mtok='0.9', output_per_mtok='1.5')
    return {'open_target': shared,
            'advisor_target': shared,
            'judge_target': openrouter_target('openai/gpt-oss-20b', provider='parasail/fp4', provider_name='Parasail',
                                              input_per_mtok='0.03', output_per_mtok='0.15'),
            'pool_targets': {'low': anthropic_target('claude-haiku-5-5', effort='medium'),
                             'mid': anthropic_target('claude-sonnet-5-5', effort='medium'),
                             'high': anthropic_target('claude-fable-5-2', effort='high')},
            'decision': JEV, 'decision_per_call_usd': '0.01'}


BLIND_ARMS = ('fixed_high', 'cloudyfan', 'cloudyfan_plus', 'advisor_first')


def _price_note(target):
    price = target['price']
    return f"US${price['input_per_mtok']} input / US${price['output_per_mtok']} output per million tokens"


def blind_setup():
    """2026-11-24 使用者指定的盲測角色：除 Jev 外全走 Anthropic。

    首答層 Sonnet 5.5 xhigh；低池 Opus 5.5 low、中池 Opus 5.5 xhigh、高池 Fable 5.2 xhigh；
    顧問與評審 Haiku 5.5 xhigh。顧問只分派，可派到 Sonnet 與三個池，候選清單附價格。"""
    sonnet = anthropic_target(SONNET, effort='xhigh')
    haiku = anthropic_target('claude-haiku-5-5', effort='xhigh')
    pools = {'low': anthropic_target('claude-opus-5-5', effort='low'),
             'mid': anthropic_target('claude-opus-5-5', effort='xhigh'),
             'high': anthropic_target('claude-fable-5-2', effort='xhigh')}
    labelled = (('sonnet-xhigh', 'low', sonnet), ('opus-low', 'low', pools['low']),
                ('opus-xhigh', 'mid', pools['mid']), ('fable-xhigh', 'high', pools['high']))
    advisor_pool = {key: {**target['profile'], 'tier': tier, 'price': _price_note(target)}
                    for key, tier, target in labelled}
    return {'open_target': sonnet, 'advisor_target': haiku, 'judge_target': haiku, 'pool_targets': pools,
            'advisor_pool': advisor_pool, 'decision': JEV, 'decision_per_call_usd': '0.01'}


def frozen_tasks(limit=None, *, interleave=True, only=None, path=FROZEN_TASKS):
    """讀凍結的 50 題並重建題目。``interleave`` 時依題型輪流排序，只跑前幾題也能涵蓋各題型。"""
    from .hard_tasks import FAMILIES, load_selection
    tasks = load_selection(json.loads(Path(path).read_text()))
    if interleave:
        queues = {f: [t for t in tasks if t['family'] == f] for f in FAMILIES}
        ordered = []
        while any(queues.values()):
            for family in FAMILIES:
                if queues[family]:
                    ordered.append(queues[family].pop(0))
        tasks = ordered
    if only:
        tasks = [t for t in tasks if t['id'] in set(only)]
        if len(tasks) != len(set(only)):
            raise ValueError('Unknown task id in --task')
    return tasks[:limit] if limit else tasks


def key_from_args(args, vendor):
    """指令列的金鑰來源：單行金鑰檔，或 .env 檔中指定的一個變數；都沒給時回傳 None。"""
    key_file = getattr(args, vendor + '_key_file')
    env_file = getattr(args, vendor + '_env_file')
    if key_file is not None:
        return read_key(key_file)
    if env_file is not None:
        return read_env_key(env_file, getattr(args, vendor + '_env_name'))
    return None


CODE_TASK_SETS = ('v1', 'v2', 'v3', 'v4')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    models = sub.add_parser('models', help='列出 OpenRouter 模型與價格（不收費）')
    models.add_argument('--openrouter-key-file', type=Path)
    models.add_argument('--openrouter-env-file', type=Path)
    models.add_argument('--openrouter-env-name', default='OPENROUTER_API_KEY')
    models.add_argument('--match', action='append', default=[])
    endpoints = sub.add_parser('endpoints', help='列出某個 OpenRouter 模型的供應商與價格（不收費）')
    endpoints.add_argument('model')
    endpoints.add_argument('--openrouter-key-file', type=Path)
    endpoints.add_argument('--openrouter-env-file', type=Path)
    endpoints.add_argument('--openrouter-env-name', default='OPENROUTER_API_KEY')
    cal = sub.add_parser('calibrate', help='用一個開源模型跑候選難題做難度校準（會發出真實付費請求）')
    cal.add_argument('--root', type=Path, required=True)
    cal.add_argument('--cap', default='2')
    cal.add_argument('--seeds-per-level', type=int, default=2)
    cal.add_argument('--family', action='append', default=[])
    cal.add_argument('--max-tokens', type=int, default=16000)
    cal.add_argument('--openrouter-key-file', type=Path)
    cal.add_argument('--openrouter-env-file', type=Path)
    cal.add_argument('--openrouter-env-name', default='OPENROUTER_API_KEY')
    cal.add_argument('--openrouter-model')
    cal.add_argument('--openrouter-provider')
    cal.add_argument('--openrouter-provider-name')
    cal.add_argument('--openrouter-input-price')
    cal.add_argument('--openrouter-output-price')
    cal.add_argument('--blind', action='store_true', help='改程式題（盲測題集）校準；用 --anthropic-model 指定模型')
    cal.add_argument('--anthropic-model', help='盲測校準用的 Anthropic 模型，例如 claude-sonnet-5-5')
    cal.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), default='xhigh')
    cal.add_argument('--task', action='append', default=[], help='盲測校準只跑指定題目編號')
    cal.add_argument('--task-set', choices=CODE_TASK_SETS, default='v3',
                     help='盲測題集版本：v1 規格逐條寫明、v2 演算法陷阱、v3 工單式（預設）')
    cal.add_argument('--anthropic-env-file', type=Path)
    cal.add_argument('--anthropic-key-file', type=Path)
    cal.add_argument('--anthropic-env-name', default='ANTHROPIC_API_KEY')
    cal.add_argument('--anthropic-workspace-id')
    cal.add_argument('--rejection-policy-source', type=Path)
    cal.add_argument('--confirm-paid', action='store_true')
    cmp = sub.add_parser('compare', help='三組比較：凍結的難題在各路徑上各跑一次（會發出真實付費請求）')
    cmp.add_argument('--root', type=Path, required=True)
    cmp.add_argument('--cap', required=True, help='這一階段的實際花費上限（美元）')
    cmp.add_argument('--ledger-exposure', default='100', help='預算庫的暫扣上限（容納未核帳呼叫的保守暫扣，不是要花的錢）')
    cmp.add_argument('--task', action='append', default=[], help='只跑指定的題目編號')
    cmp.add_argument('--limit', type=int, help='只跑前幾題（依題型輪流排序）')
    cmp.add_argument('--arm', action='append', choices=ALL_ARMS, default=[])
    cmp.add_argument('--high-model', help='把高池換成這個 Anthropic 模型（例如 claude-opus-5-5）')
    cmp.add_argument('--high-effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), help='高池的推理強度')
    cmp.add_argument('--max-tokens', type=int, default=16000)
    cmp.add_argument('--tools', action='store_true', help='工具題：每題多一個 check_answer 工具（多回合）')
    cmp.add_argument('--max-turns', type=int, help='每次作答的回合上限；工具題預設 6，否則 1')
    cmp.add_argument('--max-input-bytes', type=int, help='每次請求的輸入上限；工具題預設 131072，否則 24576')
    cmp.add_argument('--loop-guard', choices=('shadow', 'active'), default='shadow', help='路徑一加強版的迴圈守衛模式')
    cmp.add_argument('--task-set', choices=CODE_TASK_SETS, default='v3',
                     help='盲測題集版本：v1 規格逐條寫明、v2 演算法陷阱、v3 工單式（預設）')
    cmp.add_argument('--blind', action='store_true',
                     help='盲測：改程式題、只看公開測試，事後用隱藏測試計分；角色依 blind_setup')
    cmp.add_argument('--openrouter-env-file', type=Path)
    cmp.add_argument('--openrouter-key-file', type=Path)
    cmp.add_argument('--openrouter-env-name', default='OPENROUTER_API_KEY')
    cmp.add_argument('--anthropic-env-file', type=Path)
    cmp.add_argument('--anthropic-key-file', type=Path)
    cmp.add_argument('--anthropic-env-name', default='ANTHROPIC_API_KEY')
    cmp.add_argument('--anthropic-workspace-id')
    cmp.add_argument('--rejection-policy-source', type=Path)
    cmp.add_argument('--confirm-paid', action='store_true')
    rev = sub.add_parser('review-replay', help='把盲測校準的首答交給評審重審，量抓錯與誤報（會發出真實付費請求）')
    rev.add_argument('--root', type=Path, required=True, help='這次重審的新資料夾')
    rev.add_argument('--source', type=Path, required=True, help='盲測校準的資料夾（含 calibration-report.json）')
    rev.add_argument('--cap', default='1')
    rev.add_argument('--task-set', choices=CODE_TASK_SETS, default='v4')
    rev.add_argument('--task', action='append', default=[])
    rev.add_argument('--anthropic-model', default='claude-haiku-5-5')
    rev.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), default='xhigh')
    rev.add_argument('--anthropic-env-file', type=Path)
    rev.add_argument('--anthropic-key-file', type=Path)
    rev.add_argument('--anthropic-env-name', default='ANTHROPIC_API_KEY')
    rev.add_argument('--anthropic-workspace-id')
    rev.add_argument('--confirm-paid', action='store_true')
    adv = sub.add_parser('advisor-replay', help='把盲測題交給只分派的顧問，記下分派並對照現成答案（會發出真實付費請求）')
    adv.add_argument('--root', type=Path, required=True)
    adv.add_argument('--answers', action='append', default=[], help='設定=校準資料夾，可重複，例如 haiku-low=DIR')
    adv.add_argument('--cap', default='1')
    adv.add_argument('--task-set', choices=CODE_TASK_SETS, default='v4')
    adv.add_argument('--task', action='append', default=[])
    adv.add_argument('--anthropic-model', default='claude-haiku-5-5')
    adv.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), default='xhigh')
    adv.add_argument('--anthropic-env-file', type=Path)
    adv.add_argument('--anthropic-key-file', type=Path)
    adv.add_argument('--anthropic-env-name', default='ANTHROPIC_API_KEY')
    adv.add_argument('--anthropic-workspace-id')
    adv.add_argument('--confirm-paid', action='store_true')
    run = sub.add_parser('connect', help='階段 0 連線驗證（會發出真實付費請求）')
    run.add_argument('--root', type=Path, required=True, help='這次試跑的新資料夾（收據、帳本、報告）')
    run.add_argument('--cap', default='1', help='這一階段的美元上限')
    run.add_argument('--anthropic-key-file', type=Path)
    run.add_argument('--anthropic-env-file', type=Path, help='改從 .env 檔只取 --anthropic-env-name 這一個變數')
    run.add_argument('--anthropic-env-name', default='ANTHROPIC_API_KEY')
    run.add_argument('--anthropic-model', action='append', default=[])
    run.add_argument('--anthropic-workspace-id', help='金鑰沒有綁定工作區時要帶的工作區 ID')
    run.add_argument('--openrouter-key-file', type=Path)
    run.add_argument('--openrouter-env-file', type=Path)
    run.add_argument('--openrouter-env-name', default='OPENROUTER_API_KEY')
    run.add_argument('--openrouter-model')
    run.add_argument('--openrouter-provider')
    run.add_argument('--openrouter-provider-name')
    run.add_argument('--openrouter-input-price')
    run.add_argument('--openrouter-output-price')
    run.add_argument('--rejection-policy-source', type=Path,
                     help='OpenRouter 定價政策存檔；有給時，上游 429 且無答案記為政策 0 元')
    run.add_argument('--confirm-paid', action='store_true', help='確認要發出真實付費請求')
    args = parser.parse_args(argv)
    if args.command == 'models':
        key = key_from_args(args, 'openrouter')
        if key is None:
            parser.error('openrouter_key_required')
        for row in list_models(key, match=args.match):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.command == 'endpoints':
        key = key_from_args(args, 'openrouter')
        if key is None:
            parser.error('openrouter_key_required')
        for row in list_endpoints(key, args.model):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.command == 'compare':
        if not args.confirm_paid:
            parser.error('paid_requests_require_--confirm-paid')
        if args.blind and (args.tools or args.high_model or args.high_effort):
            parser.error('blind_uses_fixed_roles_without_tools')
        setup_ = blind_setup() if args.blind else pilot_setup()
        if args.high_model or args.high_effort:
            current = setup_['pool_targets']['high']
            setup_['pool_targets']['high'] = anthropic_target(args.high_model or current['vendor_model'],
                                                              effort=args.high_effort or current['profile']['effort'])
        if args.blind:
            from .code_tasks import FROZEN_CODE_TASK_SETS, frozen_code_tasks
            tasks = frozen_code_tasks(path=FROZEN_CODE_TASK_SETS[args.task_set], limit=args.limit)
            if args.task:
                tasks = [t for t in tasks if t['id'] in set(args.task)]
        else:
            tasks = frozen_tasks(args.limit, only=args.task or None)
        rows = compare(args.root, tasks, **setup_, blind=args.blind,
                       ledger_exposure_usd=args.ledger_exposure,
                       openrouter_key=key_from_args(args, 'openrouter'),
                       anthropic_key=key_from_args(args, 'anthropic'), cap_usd=args.cap,
                       arms=tuple(args.arm) or (BLIND_ARMS if args.blind else ARMS), max_tokens=args.max_tokens,
                       anthropic_workspace_id=args.anthropic_workspace_id,
                       rejection_policy_source=args.rejection_policy_source, tools=args.tools,
                       max_turns=args.max_turns or (6 if args.tools else 1),
                       max_input_bytes=args.max_input_bytes or (131072 if args.tools else 24576),
                       loop_guard_mode=args.loop_guard)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        return 0
    if args.command == 'advisor-replay':
        if not args.confirm_paid:
            parser.error('paid_requests_require_--confirm-paid')
        answers = {}
        for item in args.answers:
            setting, _, folder = item.partition('=')
            if setting not in replay_advisor_pool() or not folder:
                parser.error('answers_must_be_SETTING=DIR_with_a_pool_setting')
            answers.setdefault(setting, []).append(Path(folder))
        from .code_tasks import FROZEN_CODE_TASK_SETS, frozen_code_tasks
        tasks = [t for t in frozen_code_tasks(path=FROZEN_CODE_TASK_SETS[args.task_set])
                 if not args.task or t['id'] in set(args.task)]
        rows = advisor_replay(args.root, answers, tasks, anthropic_target(args.anthropic_model, effort=args.effort),
                              anthropic_key=key_from_args(args, 'anthropic'), cap_usd=args.cap,
                              anthropic_workspace_id=args.anthropic_workspace_id)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        return 0
    if args.command == 'review-replay':
        if not args.confirm_paid:
            parser.error('paid_requests_require_--confirm-paid')
        from .code_tasks import FROZEN_CODE_TASK_SETS, frozen_code_tasks
        done = {r['id'] for r in json.loads((args.source / 'calibration-report.json').read_text())['rows']
                if 'hidden_first_pass' in r}
        tasks = [t for t in frozen_code_tasks(path=FROZEN_CODE_TASK_SETS[args.task_set])
                 if t['id'] in done and (not args.task or t['id'] in set(args.task))]
        rows = review_replay(args.root, args.source, tasks, anthropic_target(args.anthropic_model, effort=args.effort),
                             anthropic_key=key_from_args(args, 'anthropic'), cap_usd=args.cap,
                             anthropic_workspace_id=args.anthropic_workspace_id)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        return 0
    if args.command == 'calibrate':
        if not args.confirm_paid:
            parser.error('paid_requests_require_--confirm-paid')
        if args.blind:
            if not args.anthropic_model:
                parser.error('blind_calibration_requires_--anthropic-model')
            from .code_tasks import FROZEN_CODE_TASK_SETS, frozen_code_tasks
            tasks = [t for t in frozen_code_tasks(path=FROZEN_CODE_TASK_SETS[args.task_set])
                     if (not args.task or t['id'] in set(args.task))
                     and (not args.family or t['family'] in args.family)]
            rows = calibrate(args.root, tasks, anthropic_target(args.anthropic_model, effort=args.effort),
                             anthropic_key=key_from_args(args, 'anthropic'), cap_usd=args.cap,
                             max_tokens=args.max_tokens, max_input_bytes=24576,
                             anthropic_workspace_id=args.anthropic_workspace_id, blind=True)
            for row in rows:
                print(json.dumps(row, ensure_ascii=False), flush=True)
            return 0
        if not all((args.openrouter_model, args.openrouter_provider, args.openrouter_provider_name,
                    args.openrouter_input_price, args.openrouter_output_price)):
            parser.error('openrouter_calibration_requires_model_provider_and_prices')
        from . import hard_tasks
        tasks = [t for t in hard_tasks.candidates(args.seeds_per_level)
                 if not args.family or t['family'] in args.family]
        target = openrouter_target(args.openrouter_model, provider=args.openrouter_provider,
                                   provider_name=args.openrouter_provider_name,
                                   input_per_mtok=args.openrouter_input_price,
                                   output_per_mtok=args.openrouter_output_price)
        rows = calibrate(args.root, tasks, target, openrouter_key=key_from_args(args, 'openrouter'),
                         cap_usd=args.cap, max_tokens=args.max_tokens,
                         rejection_policy_source=args.rejection_policy_source)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        return 0
    if not args.confirm_paid:
        parser.error('paid_requests_require_--confirm-paid')
    targets = [anthropic_target(model) for model in args.anthropic_model]
    if args.openrouter_model:
        required = (args.openrouter_provider, args.openrouter_provider_name,
                    args.openrouter_input_price, args.openrouter_output_price)
        if any(value is None for value in required):
            parser.error('openrouter_model_requires_provider_name_and_prices')
        targets.append(openrouter_target(args.openrouter_model, provider=args.openrouter_provider,
                                         provider_name=args.openrouter_provider_name,
                                         input_per_mtok=args.openrouter_input_price,
                                         output_per_mtok=args.openrouter_output_price))
    if not targets:
        parser.error('no_targets')
    rows = connect(args.root, targets,
                   openrouter_key=key_from_args(args, 'openrouter'),
                   anthropic_key=key_from_args(args, 'anthropic'),
                   cap_usd=args.cap, anthropic_workspace_id=args.anthropic_workspace_id,
                   rejection_policy_source=args.rejection_policy_source)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    return 0 if all(row['status'] == 'passed' for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
