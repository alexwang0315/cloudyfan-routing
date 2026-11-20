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
              max_tokens=16000, max_input_bytes=16384, anthropic_workspace_id=None, rejection_policy_source=None):
    """難度校準：每題用同一個模型作答；所有層都設成這個模型，所以答錯時的修正也由它自己做。
    回傳每題是否首答通過、最終是否通過、作答次數、檢查碼與費用；報告不含標準答案。"""
    from .hard_tasks import HardTaskEnvironment
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
            result = runner.run('cal-' + task['id'], {'prompt': task['prompt']}, lambda t=task: HardTaskEnvironment(t),
                                contract_version='hard-tasks-calibration-v1', mode='open_source_first')
            calls = [call for attempt in result['attempts'] for call in attempt['calls']]
            last = result['attempts'][-1] if result['attempts'] else {}
            row.update(status=result['status'], first_pass=result['first_public_pass'],
                       final_pass=result['final_public_pass'], attempts=len(result['attempts']),
                       checks=list((last.get('public') or {}).get('checks') or []),
                       output_tokens=sum((c.get('usage') or {}).get('completion_tokens') or 0 for c in calls),
                       provider_rejected=any(c.get('provider_rejected') for c in calls),
                       cost_usd=result['costs']['api_usd'], wall_seconds=result.get('wall_seconds'))
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
    known = sum((Decimal(r['cost_usd']) for r in done if r.get('cost_usd') is not None), Decimal(0))
    report = {'stage': 'calibration', 'target': target['address'], 'cap_usd': str(cap_usd), 'rows': rows,
              'summary': {'tasks': len(done), 'first_pass': sum(r['first_pass'] is True for r in done),
                          'final_pass': sum(r['final_pass'] is True for r in done),
                          'not_completed': len(rows) - len(done), 'known_usd': format(known.normalize(), 'f')},
              'by_family': by_family}
    (root / 'calibration-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return rows


ARMS = ('cloudyfan', 'cloudyfan_plus', 'advisor_first')
ARM_CODES = {'cloudyfan': 'p1', 'cloudyfan_plus': 'p1plus', 'advisor_first': 'p2'}
# Jev 分類器（OpenRouter 的 decisions 端點）。沒有公開單價，上限檢查用保守的每次固定價，實際照回應記帳。
JEV = {'model': 'typesafe/jev-1.13', 'version': 'typesafe/jev-1.13-20260917', 'provider': 'typesafe',
       'provider_name': 'TypeSafe'}


def _role(target):
    return dict(target['profile'], open_source=True)


def _compare_runner(root, *, open_target, pool_targets, advisor_target, judge_target, decision,
                    decision_per_call_usd, openrouter_key, anthropic_key, ledger_exposure_usd, transports=None,
                    max_tokens=16000, max_input_bytes=24576, anthropic_workspace_id=None,
                    rejection_policy_source=None):
    """組出比較用的 BudgetedCore（同一個 Core、收據與預算庫）。預算庫的授權是「帳本暫扣上限」，
    只用來容納未核帳呼叫的保守暫扣，不是要花的錢；實際花費上限由 ``compare`` 另外檢查。"""
    from .advisor import AdvisorConfig
    from .cloudyfan_plus import CloudyfanPlusConfig
    root = Path(root)
    transports = transports or {}
    anthropic_prices = {t['vendor_model']: t['price'] for t in pool_targets.values() if t['vendor'] == 'anthropic'}
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
                                  valid_until=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
    client = Client(http, openrouter_key, root / 'receipts', rejection_fee_policy=policy)
    store = BudgetStore(root / 'budget.sqlite')
    if store.report()['authorization'] is None:
        store.authorize(Authorization('pilot-ledger-exposure-only', Decimal(str(ledger_exposure_usd)),
                                      (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()))
    profiles = {'open_source': dict(open_target['profile']),
                **{tier: dict(pool_targets[tier]['profile']) for tier in ('low', 'mid', 'high')}}
    advisor = AdvisorConfig(advisor=_role(advisor_target), judge=_role(judge_target),
                            score_snapshot='pilot-81-20261119')
    plus = CloudyfanPlusConfig(judge=_role(judge_target))
    prices = {t['address']: t['price'] for t in (open_target, advisor_target, judge_target, *pool_targets.values())}
    prices[decision['version']] = {'per_call_usd': str(decision_per_call_usd)}
    verifier = ListPriceBounds(prices, max_input_bytes=max_input_bytes, max_output_tokens=max_tokens)
    core = Core(client, profiles, dict(decision), system=CALIBRATION_SYSTEM,
                limits=Limits(max_turns=1, max_tokens=max_tokens, max_context_bytes=max_input_bytes),
                advisor_first=advisor, cloudyfan_plus=plus)
    return BudgetedCore(core, store, model_bounds=verifier.model_bounds(), tool_bounds={}, verify_bound=verifier)


def compare_worst_case_usd(*, max_tokens=16000, max_input_bytes=24576, **setup):
    """每組跑一題的最壞花費（牌價 × token 上限），不發出任何請求。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        runner = _compare_runner(tmp, **setup, openrouter_key='offline', anthropic_key='offline',
                                 ledger_exposure_usd='1', max_tokens=max_tokens, max_input_bytes=max_input_bytes)
        return {arm: runner._admit(arm) for arm in ARMS}


def compare(root, tasks, *, cap_usd, openrouter_key, anthropic_key, ledger_exposure_usd='100', arms=ARMS,
            order_seed=0, **setup):
    """#81 三組比較：每題依輪替的順序在選定的組別各跑一次。

    ``cap_usd`` 是**實際花費上限**：每跑一題之前檢查「帳本已知花費＋這題最壞花費」，超過就不送出（記為
    ``spend_cap``）。``ledger_exposure_usd`` 只是預算庫的暫扣上限。同一個資料夾裡已完成的題直接讀回，不重付、
    不受上限影響。回傳每題每組一列；報告 ``compare-report.json`` 依組別統計，不含題目與標準答案。"""
    from .hard_tasks import HardTaskEnvironment
    if not openrouter_key or not anthropic_key:
        raise ValueError('Both OpenRouter and Anthropic keys are required')
    unknown = set(arms) - set(ARMS)
    if unknown or not arms:
        raise ValueError('Unknown arms: ' + ', '.join(sorted(unknown)))
    root = Path(root)
    runner = _compare_runner(root, openrouter_key=openrouter_key, anthropic_key=anthropic_key,
                             ledger_exposure_usd=ledger_exposure_usd, **setup)
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
                    result = runner.run(run_id, {'prompt': task['prompt']}, lambda t=task: HardTaskEnvironment(t),
                                        contract_version='hard-tasks-compare-v1', mode=arm)
                row.update(_compare_row(result))
            except BudgetError as error:
                row.update(status='not_run', reason='budget: ' + str(error))
            except Exception as error:  # 照實記錄，不重試
                row.update(status='error', reason=type(error).__name__ + ': ' + str(error)[:300])
            rows.append(row)
    _write_compare_report(root, rows, arms, cap_usd, ledger_exposure_usd, runner.store.report())
    return rows


def _compare_row(result):
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
            'wall_seconds': result.get('wall_seconds')}


def _write_compare_report(root, rows, arms, cap_usd, ledger_exposure_usd, ledger):
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
                        'provider_rejected': sum(r['provider_rejected'] for r in done)}
    report = {'stage': 'compare', 'spend_cap_usd': str(cap_usd), 'ledger_exposure_usd': str(ledger_exposure_usd),
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
    cal.add_argument('--openrouter-model', required=True)
    cal.add_argument('--openrouter-provider', required=True)
    cal.add_argument('--openrouter-provider-name', required=True)
    cal.add_argument('--openrouter-input-price', required=True)
    cal.add_argument('--openrouter-output-price', required=True)
    cal.add_argument('--rejection-policy-source', type=Path)
    cal.add_argument('--confirm-paid', action='store_true')
    cmp = sub.add_parser('compare', help='三組比較：凍結的難題在各路徑上各跑一次（會發出真實付費請求）')
    cmp.add_argument('--root', type=Path, required=True)
    cmp.add_argument('--cap', required=True, help='這一階段的實際花費上限（美元）')
    cmp.add_argument('--ledger-exposure', default='100', help='預算庫的暫扣上限（容納未核帳呼叫的保守暫扣，不是要花的錢）')
    cmp.add_argument('--task', action='append', default=[], help='只跑指定的題目編號')
    cmp.add_argument('--limit', type=int, help='只跑前幾題（依題型輪流排序）')
    cmp.add_argument('--arm', action='append', choices=ARMS, default=[])
    cmp.add_argument('--max-tokens', type=int, default=16000)
    cmp.add_argument('--openrouter-env-file', type=Path)
    cmp.add_argument('--openrouter-key-file', type=Path)
    cmp.add_argument('--openrouter-env-name', default='OPENROUTER_API_KEY')
    cmp.add_argument('--anthropic-env-file', type=Path)
    cmp.add_argument('--anthropic-key-file', type=Path)
    cmp.add_argument('--anthropic-env-name', default='ANTHROPIC_API_KEY')
    cmp.add_argument('--anthropic-workspace-id')
    cmp.add_argument('--rejection-policy-source', type=Path)
    cmp.add_argument('--confirm-paid', action='store_true')
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
        rows = compare(args.root, frozen_tasks(args.limit, only=args.task or None), **pilot_setup(),
                       ledger_exposure_usd=args.ledger_exposure,
                       openrouter_key=key_from_args(args, 'openrouter'),
                       anthropic_key=key_from_args(args, 'anthropic'), cap_usd=args.cap,
                       arms=tuple(args.arm) or ARMS, max_tokens=args.max_tokens,
                       anthropic_workspace_id=args.anthropic_workspace_id,
                       rejection_policy_source=args.rejection_policy_source)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        return 0
    if args.command == 'calibrate':
        if not args.confirm_paid:
            parser.error('paid_requests_require_--confirm-paid')
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
