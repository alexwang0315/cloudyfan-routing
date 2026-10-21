"""Cached real proposal metadata connects to mock-only exact-setting exams."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.scores.sql_multi_runner import (
    MultiCandidateQualificationRunner, prepare_multi_candidate_table,
)

ROOT = Path(__file__).resolve().parents[1]


def test_cached_review_compatible_settings_connect_without_changing_old_qualification(tmp_path):
    path = ROOT / 'examples/sql-backup-candidate-review-v1.json'
    original = path.read_bytes()
    review = json.loads(original)
    compatible = [c for c in review['configurations']
                  if c['exam_compatibility']['status'] == 'exact_response_contract_compatible_only']
    assert len(compatible) == 3
    entries = [{'config_id': c['config_id'], 'profile': c['profile']} for c in compatible]
    table = prepare_multi_candidate_table(entries + [deepcopy(entries[0])])
    assert len(table['configurations']) == 3
    assert {c['configuration_sha256'] for c in table['configurations']} == {
        c['configuration_sha256'] for c in compatible}
    posts = []

    def upstream(request):
        assert request.method == 'POST'
        payload = json.loads(request.content)
        posts.append(payload)
        profile = next(c['entry']['profile'] for c in table['configurations']
                       if c['entry']['profile']['model_version'] == payload['model'])
        if profile.get('reasoning_parameter') == 'omit':
            assert 'reasoning' not in payload
        return httpx.Response(200, json={'id': f'gen-proposal-mock-{len(posts)}',
            'model': payload['model'], 'provider': profile['provider_name'],
            'choices': [{'message': {'content': 'OK'}}], 'usage': {'cost': '0.001'}})

    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('synthetic-only', Decimal('10'),
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    bounds = {c['configuration_sha256']: EnforcedBound('0.01', 'mock-only') for c in table['configurations']}
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        runner = MultiCandidateQualificationRunner(Client(http, 'mock-credential', tmp_path / 'client'),
            store, table, model_bounds=bounds, verify_bound=lambda *args: True, synthetic=True)
        result = runner.capture('metadata-bridge', max_new_requests=3)
        assert result['status'] == 'paused' and result['calls'] == 3
        assert result['formal_qualification'] == 'pending' and result['pool_activated'] is False
        assert runner.capture('metadata-bridge', max_new_requests=0)['calls'] == 3
        assert len(posts) == 3
    assert path.read_bytes() == original


def test_archived_alias_profiles_are_not_silently_narrowed_into_new_exam():
    review = json.loads((ROOT / 'examples/sql-backup-candidate-review-v1.json').read_text())
    aliases = [c for c in review['configurations']
               if c['exam_compatibility']['status'] == 'requires_new_explicit_exact_response_contract']
    assert len(aliases) == 2
    for candidate in aliases:
        profile = deepcopy(candidate['profile'])
        with pytest.raises(ValueError, match='identity'):
            prepare_multi_candidate_table([{'profile': profile}])
        assert profile == candidate['profile']
