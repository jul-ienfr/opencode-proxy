"""[P3 2026-11-21 — T307] `free/console_auth.py` : opt-in strict.

Le défaut DOIT rester `Bearer public` : la jambe free ne dépend jamais d'un
compte personnel (principe X).
"""

from __future__ import annotations

from free import console_auth


def _clear(monkeypatch):
    for name in (
        "OPENCODE_CONSOLE_TOKEN",
        "OPENCODE_CONSOLE_ORG",
        "OPENCODE_CONSOLE_UA",
        "OPENCODE_ALLOW_CONSOLE_CREDENTIAL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_disabled_by_default_keeps_the_anonymous_headers(monkeypatch):
    _clear(monkeypatch)
    headers = {"Authorization": "Bearer public"}
    assert console_auth.apply_console_auth(headers) is headers
    assert headers == {"Authorization": "Bearer public"}


def test_enabled_without_a_token_keeps_the_anonymous_headers(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL", "1")
    headers = {"Authorization": "Bearer public"}
    console_auth.apply_console_auth(headers)
    assert headers["Authorization"] == "Bearer public"


def test_enabled_with_a_token_overwrites_auth_org_and_ua(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL", "yes")
    monkeypatch.setenv("OPENCODE_CONSOLE_TOKEN", "secret-token")
    monkeypatch.setenv("OPENCODE_CONSOLE_ORG", "org-1")
    monkeypatch.setenv("OPENCODE_CONSOLE_UA", "custom-ua")
    headers = {"Authorization": "Bearer public", "User-Agent": "old"}
    console_auth.apply_console_auth(headers)
    assert headers == {
        "Authorization": "Bearer secret-token",
        "x-opencode-org-id": "org-1",
        "User-Agent": "custom-ua",
    }


def test_console_ua_defaults_when_absent(monkeypatch):
    _clear(monkeypatch)
    assert console_auth.console_ua() == console_auth.DEFAULT_CONSOLE_UA
    monkeypatch.setenv("OPENCODE_CONSOLE_UA", "  custom  ")
    assert console_auth.console_ua() == "custom"
