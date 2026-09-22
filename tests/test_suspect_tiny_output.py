"""Tests suspect_tiny_output (P0) — détection « tour agent vide ».

Contrat :
- in >= input_min (défaut 40000) + out < output_max (défaut 100) + aucun
  tool call → suspect (tour inutilisable pour un harness agentique).
- Un tour avec tool calls n'est JAMAIS suspect (cas normal d'un tour agent).
- _save_and_log_request bascule un suspect en success=False + error
  "upstream_tiny_output" (vérité DB) ; les chemins normaux inchangés.
- _cb_record_stream_success ignore le CB success sur suspect (neutre).
- custom_routes / mapping NON touché par ce module (détection seule).
"""

import pytest

import opencode as oc

# ── Helper pur ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inp,out,tools,expected",
    [
        (597258, 26, [], True),  # cas réel 27f16308 (597k → 26, 0 outil)
        (107709, 21, [], True),  # cas réel du 18/09 (107k → 21)
        (46295, 30, [], True),  # seuil bas (46k → 30)
        (40000, 99, [], True),  # pile sur les seuils par défaut
        (39999, 10, [], False),  # sous le seuil input
        (50000, 100, [], False),  # output au seuil → pas suspect
        (50000, 500, [], False),  # vraie réponse courte → ok
        (203000, 87, ["bash"], False),  # micro-sortie AVEC tool = tour normal
        (80000, 63, ["read"], False),  # idem
        (80000, 63, None, True),  # None == pas d'outils
        (None, None, [], False),  # valeurs manquantes → jamais suspect
        (0, 0, [], False),
    ],
)
def test_is_suspect_tiny_output_matrix(inp, out, tools, expected):
    assert oc._is_suspect_tiny_output(inp, out, tools) is expected


def test_is_suspect_tiny_output_never_raises():
    assert oc._is_suspect_tiny_output(object(), object(), object()) is False


def test_thresholds_overridable_via_yaml(monkeypatch):
    fake = {"input_min": 200000, "output_max": 50}
    monkeypatch.setattr(oc, "yaml_get", lambda *a, **k: fake.get(a[1], k.get("default")))
    assert oc._is_suspect_tiny_output(100000, 20, []) is False
    assert oc._is_suspect_tiny_output(250000, 20, []) is True


# ── _save_and_log_request : vérité DB ─────────────────────────────────


@pytest.mark.asyncio
async def test_save_and_log_request_marks_suspect_failure(monkeypatch):
    saved = {}

    async def _fake_save(*a, **kw):
        saved["args"] = a
        saved["kw"] = kw

    monkeypatch.setattr(oc, "_save_request", _fake_save)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)

    await oc._save_and_log_request(
        "req-suspect",
        "muse-spark-1.3-contributor",
        "muse-spark-1.3-contributor",
        0.0,
        107709,
        21,
        0,
        "openai",
        True,
        "none",
        "high",
        "127.0.0.1",
        "alias",
        ["bash"],
        "",
        tools_used=[],
    )
    kw = saved["kw"]
    assert kw["success"] is False
    assert kw["error"] == "upstream_tiny_output"


@pytest.mark.asyncio
async def test_save_and_log_request_keeps_normal_success(monkeypatch):
    saved = {}

    async def _fake_save(*a, **kw):
        saved["args"] = a
        saved["kw"] = kw

    monkeypatch.setattr(oc, "_save_request", _fake_save)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)

    # tour normal avec tool call → succès inchangé, error=None
    await oc._save_and_log_request(
        "req-ok",
        "muse-spark-1.3-contributor",
        "muse-spark-1.3-contributor",
        0.0,
        46334,
        131,
        0,
        "openai",
        True,
        "none",
        "high",
        "127.0.0.1",
        "alias",
        ["bash"],
        "",
        tools_used=["bash"],
    )
    kw = saved["kw"]
    assert kw["success"] is True
    assert kw.get("error") is None


# ── CB : neutre sur suspect ───────────────────────────────────────────


def test_cb_stream_success_skipped_on_suspect(monkeypatch):
    calls = []
    monkeypatch.setattr(oc, "_cb_record_success", lambda ep: calls.append(("ok", ep)))
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    assert oc._cb_record_stream_success("ep", 107709, 21, []) is False
    assert calls == []


def test_cb_stream_success_kept_normally(monkeypatch):
    calls = []
    monkeypatch.setattr(oc, "_cb_record_success", lambda ep: calls.append(("ok", ep)))
    assert oc._cb_record_stream_success("ep", 46334, 131, ["bash"]) is True
    assert calls == [("ok", "ep")]


# ── Preview : post-mortem du tour vide ───────────────────────────────


async def _call_save(monkeypatch, **kw):
    saved = {}

    async def _fake_save(*a, **k):
        saved["kw"] = k

    monkeypatch.setattr(oc, "_save_request", _fake_save)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    base = {
        "req_id": "req-pv",
        "model_id": "muse-spark-1.3-contributor",
        "original_model": "muse-spark-1.3-contributor",
        "start_time": 0.0,
        "inp": 107709,
        "out": 21,
        "cache": 0,
        "protocol": "openai",
        "is_stream": True,
        "thinking_type": "none",
        "effort": "high",
        "client_ip": "127.0.0.1",
        "account_alias": "alias",
        "tools": ["bash"],
        "log_tag": "",
        "tools_used": [],
    }
    base.update(kw)
    await oc._save_and_log_request(**base)
    return saved["kw"]


@pytest.mark.asyncio
async def test_preview_stored_only_on_suspect(monkeypatch):
    kw = await _call_save(monkeypatch, response_preview="du texte vide de sens")
    assert kw["success"] is False
    assert kw["response_body"] == "[stream-preview] du texte vide de sens"


@pytest.mark.asyncio
async def test_preview_dropped_on_normal_turn(monkeypatch):
    kw = await _call_save(monkeypatch, out=131, tools_used=["bash"], response_preview="du texte")
    assert kw["success"] is True
    assert kw.get("response_body") is None


@pytest.mark.asyncio
async def test_suspect_without_preview_keeps_null_body(monkeypatch):
    kw = await _call_save(monkeypatch)
    assert kw["success"] is False
    assert kw.get("response_body") is None


# ── Retry vide pré-terminal (P4) ──────────────────────────────────────


@pytest.mark.parametrize(
    "yielded,out,expected",
    [
        (False, 0, True),  # EOF nu : retry sûr
        (False, None, True),  # compteurs absents : retry sûr
        (False, 62, False),  # quelque chose est parti : pas de retry
        (True, 0, False),  # flag yielded : pas de retry (concat interdite)
        (True, 10, False),  # stream partiel : jamais de retry (concat)
    ],
)
def test_should_retry_empty_stream(yielded, out, expected):
    assert oc._should_retry_empty_stream(yielded, out) is expected


def test_should_retry_empty_stream_never_raises():
    assert oc._should_retry_empty_stream(object(), object()) is False
