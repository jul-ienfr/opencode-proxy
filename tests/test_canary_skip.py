"""test_canary_skip.py — la sonde canary ne martèle pas quand tout est trial-mort."""

import pytest

import opencode as oc


@pytest.mark.asyncio
async def test_canary_skips_without_upstream_when_chain_dead(monkeypatch):
    calls = []

    async def _fake_probe(probe, path, model, timeout_s):
        calls.append((probe, path))
        return {"ok": True, "ms": 1.0, "detail": "", "status": 200}

    monkeypatch.setattr(oc, "_canary_probe_once", _fake_probe)
    monkeypatch.setattr(oc, "_resolve_free_model", lambda m: "mimo-v2.6-flash-free")
    monkeypatch.setattr(
        oc, "_free_candidate_chain", lambda paid, base: ["mimo-v2.6-flash-free", "deepseek-v4.1-flash-free"]
    )
    monkeypatch.setattr(oc, "_free_stations_exhausted", lambda m: True)

    class _P:
        enabled = True
        _stations = [object()]

    monkeypatch.setattr(oc, "_free_ip_pool", _P())
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)

    out = await oc._canary_cycle()
    assert calls == [], "aucun appel réseau quand la chaîne est épuisée"
    assert out["probes"]["free.chat"]["detail"].startswith("skip trial-dead")
    assert out["probes"]["free.messages"]["detail"].startswith("skip trial-dead")


@pytest.mark.asyncio
async def test_canary_probes_when_a_candidate_is_alive(monkeypatch):
    calls = []

    async def _fake_probe(probe, path, model, timeout_s):
        calls.append((probe, path))
        return {"ok": True, "ms": 1.0, "detail": "", "status": 200}

    monkeypatch.setattr(oc, "_canary_probe_once", _fake_probe)
    monkeypatch.setattr(oc, "_resolve_free_model", lambda m: "mimo-v2.6-flash-free")
    monkeypatch.setattr(
        oc, "_free_candidate_chain", lambda paid, base: ["mimo-v2.6-flash-free", "deepseek-v4.1-flash-free"]
    )
    monkeypatch.setattr(oc, "_free_stations_exhausted", lambda m: m != "deepseek-v4.1-flash-free")

    class _P:
        enabled = True
        _stations = [object()]

    monkeypatch.setattr(oc, "_free_ip_pool", _P())
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)

    out = await oc._canary_cycle()
    assert len(calls) == 2, "sonde réseau normale quand un bucket vit"
    assert out["model"] == "big-pickle"
