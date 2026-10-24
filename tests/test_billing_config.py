"""Billing configuration is explicit, read-only and never shell-evaluated."""
import pytest

from cloudyfan_monitor.billing_config import BillingConfigurationError, load_billing_credentials


def test_explicit_file_uses_billing_key_priority_and_ignores_other_services(tmp_path):
    path = tmp_path / "keys.env"
    sentinel = tmp_path / "should-never-run"
    path.write_text(f'''OTHER_SERVICE_KEY=$(touch {sentinel})
export OPENROUTER_API_KEY = "ordinary-key"
OPENROUTER_BILLING_API_KEY='billing-key' # explicit priority
OPENROUTER_MANAGEMENT_API_KEY=manage-key
''')
    result = load_billing_credentials(key_file=path, environ={"OPENROUTER_BILLING_API_KEY": "env-key"})
    assert result.api_key == "billing-key"
    assert result.management_key == "manage-key"
    assert not sentinel.exists()
    assert "billing-key" not in repr(result) and "manage-key" not in repr(result)


def test_explicit_file_never_falls_back_to_environment_or_expands_values(tmp_path):
    path = tmp_path / "keys.env"
    path.write_text("OTHER_SERVICE_KEY='an unrelated unterminated quote\n")
    with pytest.raises(BillingConfigurationError, match="^billing_key_missing$"):
        load_billing_credentials(key_file=path, environ={"OPENROUTER_API_KEY": "env-secret"})
    path.write_text("OPENROUTER_API_KEY='${NOT_EXPANDED}'\n")
    result = load_billing_credentials(key_file=path, environ={"NOT_EXPANDED": "env-secret",
                                     "OPENROUTER_MANAGEMENT_API_KEY": "env-management-secret"})
    assert result.api_key == "${NOT_EXPANDED}" and result.management_key is None


def test_environment_reads_only_three_names_and_prefers_billing_key():
    class SelectedEnvironment(dict):
        def get(self, name):
            assert name in ("OPENROUTER_BILLING_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_API_KEY")
            return super().get(name)
    environment = SelectedEnvironment(OPENROUTER_BILLING_API_KEY="billing-secret",
                                      OPENROUTER_API_KEY="ordinary-secret",
                                      OPENROUTER_MANAGEMENT_API_KEY="management-secret",
                                      OTHER_SERVICE_KEY="unrelated-secret")
    result = load_billing_credentials(environ=environment)
    assert result.api_key == "billing-secret" and result.management_key == "management-secret"
    del environment["OPENROUTER_BILLING_API_KEY"]
    assert load_billing_credentials(environ=environment).api_key == "ordinary-secret"


@pytest.mark.parametrize("body,code", [
    ("OPENROUTER_API_KEY=secret-one\nexport OPENROUTER_API_KEY=secret-two\n", "billing_key_duplicate"),
    ("OPENROUTER_API_KEY='secret-one\n", "billing_key_file_invalid"),
    ("OPENROUTER_API_KEY=secret-one secret-two\n", "billing_key_file_invalid"),
    ("OPENROUTER_API_KEY='secret one'\n", "billing_key_invalid"),
])
def test_invalid_selected_file_reports_only_safe_codes(tmp_path, body, code):
    path = tmp_path / "keys.env"
    path.write_text(body)
    with pytest.raises(BillingConfigurationError) as caught:
        load_billing_credentials(key_file=path)
    assert str(caught.value) == code and caught.value.code == code
