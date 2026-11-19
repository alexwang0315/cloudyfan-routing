"""任務建議認得路徑二與路徑一加強版；不呼叫任何模型。"""
import pytest

from cloudyfan_jev import task_recommendations as advice


def frozen_context(mode):
    return {'schema_version': 'creation-routing-context-v1', 'contract_id': 'demo-contract',
            'registered_sha256': 'a' * 64, 'category': 'arithmetic', 'mode': mode,
            'calibration_status': 'missing', 'source_kind': 'not_provided'}


@pytest.mark.parametrize('mode', ['cloudyfan', 'fixed_high', 'low_first', 'open_source_first',
                                  'cloudyfan_plus', 'advisor_first'])
def test_context_accepts_every_registered_routing_mode(mode):
    assert advice.context(frozen_context(mode))['mode'] == mode


def test_context_still_rejects_unknown_mode():
    with pytest.raises(ValueError):
        advice.context(frozen_context('made_up_mode'))


def test_only_modes_with_jev_classification_get_calibration_advice():
    assert advice.has_classification('cloudyfan') and advice.has_classification('cloudyfan_plus')
    # 路徑二沒有 Jev 分類：不該被要求分類校準。
    assert not advice.has_classification('advisor_first')
    assert not any(advice.has_classification(m) for m in ('fixed_high', 'low_first', 'open_source_first'))
