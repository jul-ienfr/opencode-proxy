"""Tests V4 100% : DataPolicyError retriable (403/400, case-insensitive)."""
import types
import opencode as oc

def _fake_resp(code, text):
    return types.SimpleNamespace(status_code=code, text=text, headers={}, content=text.encode())

class TestIsRetriable:
    def test_403_datapolicy(self):
        assert oc._is_retriable_datapolicy(_fake_resp(403, '{"reason":"DataPolicyError"}')) is True

    def test_400_datapolicy(self):
        assert oc._is_retriable_datapolicy(_fake_resp(400, 'DataPolicyError illegal invocation')) is True

    def test_403_illegal_invocation_lower(self):
        assert oc._is_retriable_datapolicy(_fake_resp(403, 'ILLEGAL INVOCATION by third party')) is True

    def test_403_datapolicy_case_insensitive(self):
        assert oc._is_retriable_datapolicy(_fake_resp(403, 'datapolicyerror')) is True

    def test_403_other_not_retriable(self):
        assert oc._is_retriable_datapolicy(_fake_resp(403, 'Region not allowed')) is False

    def test_429_not_retriable(self):
        assert oc._is_retriable_datapolicy(_fake_resp(429, 'DataPolicyError')) is False

    def test_200_not_retriable(self):
        assert oc._is_retriable_datapolicy(_fake_resp(200, 'DataPolicyError')) is False

    def test_empty_text_not_retriable(self):
        assert oc._is_retriable_datapolicy(_fake_resp(403, '')) is False
