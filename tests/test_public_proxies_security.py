"""Security guards for untrusted public proxy egress."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from free.public_proxies import (
    PUBLIC_AUTH,
    SECRET_ENV_KEYS,
    SECRET_HEADERS,
    UntrustedProxySecretError,
    assert_secure_endpoint,
    assert_untrusted_proxy_safe,
    is_untrusted_proxy,
    redact_proxy_url,
    sanitize_free_headers,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch):
    for key in SECRET_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL", raising=False)


class _ManagedStation:
    managed = True
    provider = "warp"
    socks5_url = "socks5://127.0.0.1:1080"


class _StaticStation:
    managed = False
    provider = "static"
    socks5_url = "https://1.2.3.4:443"


class _DockerStation:
    _docker_container = "opencode-vpn"
    socks5_url = "socks5://127.0.0.1:1080"


class _HttpsManagedLike:
    managed = True
    scheme = "https"
    socks5_url = "https://1.2.3.4:443"


class _ManagedUnknown:
    managed = True
    socks5_url = "socks5://127.0.0.1:1080"


def test_untrusted_proxy_is_detected():
    assert is_untrusted_proxy(None, "https://1.2.3.4:443") is True
    assert is_untrusted_proxy(_StaticStation(), "https://1.2.3.4:443") is True
    assert is_untrusted_proxy(_ManagedStation(), "socks5://127.0.0.1:1080") is False
    assert is_untrusted_proxy(_ManagedStation(), "https://1.2.3.4:443") is True
    assert is_untrusted_proxy(_ManagedStation(), None) is False
    assert is_untrusted_proxy(_DockerStation(), "socks5://127.0.0.1:1080") is False
    assert is_untrusted_proxy(_DockerStation(), "https://1.2.3.4:443") is True
    assert is_untrusted_proxy(_HttpsManagedLike(), "https://1.2.3.4:443") is True
    assert is_untrusted_proxy(_ManagedUnknown(), "socks5://127.0.0.1:1080") is True


def test_sanitize_strips_every_secret_header():
    headers = {
        "Authorization": "Bearer sk-live-secret",
        "Proxy-Authorization": "Basic c2VjcmV0",
        "Cookie": "session=secret",
        "x-api-key": "sk-live-secret",
        "api-key": "sk-live-secret",
        "x-opencode-org-id": "org_secret",
        "openai-organization": "org_secret",
        "anthropic-api-key": "sk-ant-secret",
        "x-goog-api-key": "aiza-secret",
        "x-auth-token": "secret",
        "x-access-token": "secret",
        "x-session-token": "secret",
        "x-csrf-token": "secret",
        "x-custom-api-token": "secret",
        "x-my-secret": "secret",
        "Content-Type": "application/json",
        "x-opencode-session": "ses_abc",
    }
    out = sanitize_free_headers(headers, untrusted_proxy=True)
    assert out["Authorization"] == PUBLIC_AUTH
    assert out["Content-Type"] == "application/json"
    assert out["x-opencode-session"] == "ses_abc"
    lowered = {name.lower() for name in out}
    for name in SECRET_HEADERS:
        if name == "authorization":
            continue
        assert name not in lowered
    assert "x-custom-api-token" not in lowered
    assert "x-my-secret" not in lowered


def test_managed_path_forces_public_without_console_opt_in():
    out = sanitize_free_headers(
        {"Authorization": "Bearer sk-accidental-secret", "Cookie": "c=1", "x-api-key": "sk"},
        untrusted_proxy=False,
    )
    assert out["Authorization"] == PUBLIC_AUTH
    assert "Cookie" not in out
    assert "x-api-key" not in out


def test_managed_path_keeps_existing_authorization_with_console_opt_in(monkeypatch):
    monkeypatch.setenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL", "1")
    monkeypatch.setenv("OPENCODE_CONSOLE_TOKEN", "secret-console-token")
    out = sanitize_free_headers(
        {
            "Authorization": "Bearer secret-console-token",
            "Cookie": "c=1",
            "x-opencode-org-id": "org_123",
        },
        untrusted_proxy=False,
    )
    assert out["Authorization"] == "Bearer secret-console-token"
    assert out["x-opencode-org-id"] == "org_123"
    assert "Cookie" not in out


def test_console_credential_fails_closed_on_untrusted(monkeypatch):
    monkeypatch.setenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL", "1")
    monkeypatch.setenv("OPENCODE_CONSOLE_TOKEN", "secret-console-token")
    with pytest.raises(UntrustedProxySecretError):
        assert_untrusted_proxy_safe({"Authorization": "Bearer public"})
    with pytest.raises(UntrustedProxySecretError):
        sanitize_free_headers({"Authorization": "Bearer public"}, untrusted_proxy=True)


def test_console_credential_allowed_on_managed(monkeypatch):
    monkeypatch.setenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL", "1")
    monkeypatch.setenv("OPENCODE_CONSOLE_TOKEN", "secret-console-token")
    out = sanitize_free_headers(
        {"Authorization": "Bearer secret-console-token"},
        untrusted_proxy=False,
    )
    assert out["Authorization"] == "Bearer secret-console-token"


def test_environment_secret_in_custom_header_fails_closed(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test-very-secret-value")
    with pytest.raises(UntrustedProxySecretError):
        sanitize_free_headers(
            {"x-custom": "prefix sk-test-very-secret-value suffix"},
            untrusted_proxy=True,
        )


def test_untrusted_proxy_requires_https_endpoint():
    assert_secure_endpoint("https://opencode.ai/inference/v1/chat/completions", untrusted_proxy=True)
    with pytest.raises(UntrustedProxySecretError):
        assert_secure_endpoint("http://opencode.ai/inference/v1/chat/completions", untrusted_proxy=True)
    with pytest.raises(UntrustedProxySecretError):
        assert_secure_endpoint("https://evil.example/inference", untrusted_proxy=True)
    assert_secure_endpoint("http://localhost/dev", untrusted_proxy=False)


def test_redact_proxy_url_masks_credentials():
    assert redact_proxy_url("https://user:pass@1.2.3.4:443") == "https://***:***@1.2.3.4:443"
    assert redact_proxy_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert redact_proxy_url("https://[2001:db8::1]:8443") == "https://[2001:db8::1]:8443"
    assert redact_proxy_url(None) == ""


def test_no_origin_tls_disable_in_sources():
    """Le TLS d'origine ne doit jamais être désactivé, même par accident."""
    sources = [
        ROOT / "opencode.py",
        ROOT / "free" / "public_proxies.py",
        *sorted((ROOT / "free" / "pool").glob("*.py")),
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = str(path.relative_to(ROOT))
        assert not re.search(r"\bverify\s*=\s*False", text), relative
        assert "CurlOpt.SSL_VERIFYPEER" not in text, relative


def test_opencode_free_guard_forces_public_on_static_station():
    import opencode as oc

    class _Station:
        managed = False
        provider = "static"
        socks5_url = "https://1.2.3.4:443"

    out = oc._guard_free_wire_headers(
        {"Authorization": "Bearer sk-live-secret", "Cookie": "session=secret"},
        station=_Station(),
        proxy_url="https://1.2.3.4:443",
    )
    assert out["Authorization"] == "Bearer public"
    assert "Cookie" not in out


@pytest.mark.asyncio
async def test_paid_geo_path_refuses_untrusted_proxy(monkeypatch):
    import opencode as oc

    class _Station:
        managed = False
        provider = "static"
        socks5_url = "https://1.2.3.4:443"

    class _Pool:
        def _best_station(self, forced_pool=None):
            return _Station()

    monkeypatch.setattr(oc, "_free_ip_pool", _Pool(), raising=False)
    monkeypatch.setattr(oc, "_vpn_manager", None, raising=False)
    with pytest.raises(oc.UpstreamError):
        async with oc._open_via_pool(
            "https://opencode.ai/inference/v1/responses",
            {},
            {"Authorization": "Bearer paid-key"},
        ):
            pass
