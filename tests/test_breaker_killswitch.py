"""test_breaker_killswitch.py — circuit_breaker.enabled: false = tout passe toujours."""

import time

import opencode as oc


def _patch_yaml(monkeypatch, enabled):
    real = oc.yaml_get

    def _fake(section, key=None, default=None):
        if section == "circuit_breaker" and key == "enabled":
            return enabled
        return real(section, key, default)

    monkeypatch.setattr(oc, "yaml_get", _fake)


def test_disabled_never_blocks_per_endpoint(monkeypatch):
    _patch_yaml(monkeypatch, False)
    cb = oc._get_cb("http://127.0.0.1:9/fake-killswitch")
    cb.state = "open"
    cb.opened_at = time.monotonic()
    assert oc._cb_should_allow("http://127.0.0.1:9/fake-killswitch") is True
    assert oc._cb_enabled() is False


def test_disabled_global_429_reports_zero(monkeypatch):
    _patch_yaml(monkeypatch, False)
    oc._g429.record()
    assert oc._global_429_remaining() == 0.0


def test_enabled_keeps_historic_behavior(monkeypatch):
    _patch_yaml(monkeypatch, True)
    assert oc._cb_enabled() is True
    cb = oc._get_cb("http://127.0.0.1:9/fake-killswitch-2")
    cb.state = "open"
    cb.opened_at = time.monotonic()
    assert oc._cb_should_allow("http://127.0.0.1:9/fake-killswitch-2") is False


def test_free_endpoint_failures_never_open_the_breaker(monkeypatch):
    """[2026-10-29] Un quota free n'est pas une panne d'endpoint.

    Les échecs free (429 quota, flux vide, stations épuisées) ouvraient le
    breaker global de `https://opencode.ai/inference/...` après 8 échecs ;
    toutes les requêtes free recevaient ensuite 503 « circuit open » alors
    que le quota pouvait revenir. Le breaker ne doit pas couvrir la jambe free.
    """
    monkeypatch.setattr(oc, "_circuit_breakers", {})
    _patch_yaml(monkeypatch, True)
    endpoint = "https://opencode.ai/inference/openai/v1/chat/completions"
    for _ in range(20):
        oc._cb_record_failure(endpoint)
    assert oc._cb_should_allow(endpoint) is True
    assert endpoint not in oc._circuit_breakers

    # Le breaker payant, lui, continue de fonctionner.
    paid = "https://opencode.ai/inference/go/openai/v1/chat/completions"
    for _ in range(20):
        oc._cb_record_failure(paid)
    assert oc._cb_should_allow(paid) is False
