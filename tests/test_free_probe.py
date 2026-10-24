"""test_free_probe.py — sonde active de servabilité (PLAN_STRATEGIE_FREE_MODELS Lot A).

Hermetic matrix (no network, json dispatch stubs only):

  * probe_cfg — defaults (enabled False), clamps, markers parse fail-soft.
  * free_probe_shaping — /chat/completions body (max_tokens 64), muse →
    /responses (max_output_tokens), jev-* → non sondable (SystemOne).
  * free_probe_verdict — alive 2xx, dead 404/410 + marqueurs body,
    not_free 401/402, limited 429, unknown 5xx/403/400-sans-marqueur/None.
  * probe_recheck_plan — unknown périmés seulement.
  * probe_free_models — budget max_ids respecté (les f/jev non sondables
    consomment un slot sans dispatch), transport error → unknown.
  * settings hook — gate DI : promotion filtrée (ghost non appliqué), gate
    absente / levée / None → comportement historique announce-only.
  * gate runtime (opencode) — promotion + dead frozen dead_ttl_s + recheck
    unknown périmés + skip usage DB récent (source traffic) + budget max_ids.
"""

import json
import time
from pathlib import Path

import pytest

import config.discovery as _disc
import config.settings as _st

_ROOT = Path(__file__).resolve().parents[1]
API_BASE_FREE = "https://opencode.ai/inference/v1/chat/completions"


# ── probe_cfg — parse + clamps ──────────────────────────────────────


def test_probe_cfg_defaults_desactivé():
    cfg = _disc.probe_cfg(None)  # non-dict → défauts totaux
    assert cfg["enabled"] is False
    assert cfg["timeout"] == 8.0
    assert cfg["concurrency"] == 3
    assert cfg["max_ids"] == 10
    assert cfg["dead_status"] == {404, 410}
    assert cfg["not_free_status"] == {401, 402}
    assert "not found" in cfg["dead_markers"]


def test_probe_cfg_clamps_hors_bornes_et_valeurs_invalides():
    cfg = _disc.probe_cfg(
        {
            "enabled": True,
            "timeout": 1,          # clamp bas → 2
            "concurrency": 99,     # clamp haut → 10
            "max_ids": 0,          # clamp bas → 1
            "dead_ttl_s": 2,       # clamp bas → 60
            "recheck_interval_s": 10 ** 9,  # clamp haut → 86400
            "skip_recent_ok_s": 10 ** 9,    # clamp haut → 86400
            "dead_status": "bad",  # non-list → défaut
            "not_free_status": [401],
            "dead_markers": ["gone forever", 42, "  "],  # non-str filtrés
        }
    )
    assert cfg["enabled"] is True
    assert cfg["timeout"] == 2.0
    assert cfg["concurrency"] == 10
    assert cfg["max_ids"] == 1
    assert cfg["dead_ttl_s"] == 60.0
    assert cfg["recheck_interval_s"] == 86400.0
    assert cfg["skip_recent_ok_s"] == 86400.0
    assert cfg["dead_status"] == {404, 410}
    assert cfg["not_free_status"] == {401}
    assert cfg["dead_markers"] == ("gone forever",)


# ── shaping ─────────────────────────────────────────────────────────


def test_shaping_chat_free_id():
    url, body = _disc.free_probe_shaping("fledge-alpha-free", API_BASE_FREE)
    assert url == API_BASE_FREE
    assert body["model"] == "fledge-alpha-free"
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert body["max_tokens"] == 64
    assert body["stream"] is False


def test_shaping_muse_responses():
    url, body = _disc.free_probe_shaping("muse-spark-1.4-free", API_BASE_FREE)
    assert url == "https://opencode.ai/inference/v1/responses"
    assert body["input"] == "ping"
    assert body["max_output_tokens"] == 64


def test_shaping_jev_non_sondable():
    assert _disc.free_probe_shaping("jev-1.14-free", API_BASE_FREE) == (None, None)


# ── verdict matrix ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (200, "", "alive"),
        (204, "", "alive"),
        (404, "", "dead"),
        (410, "", "dead"),
        (400, "model not found", "dead"),
        (403, "engiste no longer available", "dead"),  # marqueur corps → dead
        (429, "", "limited"),
        (401, "", "not_free"),
        (402, "payment required", "not_free"),
        (500, "", "unknown"),
        (503, "upstream", "unknown"),
        (403, "", "unknown"),  # JAMAIS dead — geo/policy dépend de l'IP
        (400, "", "unknown"),  # param shape — pas un verdict mortel
        (400, "max_tokens too small", "unknown"),
        (None, "", "unknown"),
    ],
)
def test_verdict_matrix(status, body, expected):
    cfg = _disc.probe_cfg({"enabled": True})
    verdict, _reason = _disc.free_probe_verdict(status, body, cfg)
    assert verdict == expected


# ── recheck plan ────────────────────────────────────────────────────


def test_recheck_plan_unknown_perimes_seulement():
    cfg = _disc.probe_cfg({"enabled": True})
    now = 1000.0
    results = {
        "a-free": {"verdict": "unknown", "checked_epoch": now - 2 * cfg["recheck_interval_s"]},
        "b-free": {"verdict": "unknown", "checked_epoch": now},
        "c-free": {"verdict": "dead", "checked_epoch": now - 200000},  # gate exclut via dead_ttl
        "d-free": {"verdict": "alive", "checked_epoch": now - 200000},  # frais, pas de recheck
    }
    assert _disc.probe_recheck_plan(results, cfg, now) == ["a-free"]
    assert _disc.probe_recheck_plan({}, cfg, now) == []


# ── probe_free_models (exécutant borné) ──────────────────────────────


def test_probe_free_models_budget_et_verdicts():
    cfg = _disc.probe_cfg({"enabled": True, "concurrency": 1, "max_ids": 2})
    dispatched = []

    def dispatch(url, headers, payload):
        mid = json.loads(payload)["model"]
        dispatched.append(mid)
        return {"bb-free": 200, "aa-free": 429, "cc-free": 404}[mid], ""

    rows = _disc.probe_free_models(
        ["jev-5-free", "bb-free", "aa-free", "cc-free", "zz-free"],
        api_base_free=API_BASE_FREE,
        headers_fn=None,
        dispatch_fn=dispatch,
        cfg=cfg,
    )
    # jev (non sondable) consomme un slot sans dispatch → reste 1 sonde (bb)
    assert dispatched == ["bb-free"]
    assert rows["jev-5-free"]["verdict"] == "not_probeable"
    assert rows["bb-free"]["verdict"] == "alive"
    assert "cc-free" not in rows  # hors budget — pas de verdict cette passe


def test_probe_free_models_erreur_transport_inconnu():
    cfg = _disc.probe_cfg({"enabled": True, "concurrency": 1})

    def raiser(url, headers, payload):
        raise TimeoutError("boom")

    rows = _disc.probe_free_models(
        ["aa-free"],
        api_base_free=API_BASE_FREE,
        headers_fn=None,
        dispatch_fn=raiser,
        cfg=cfg,
    )
    assert rows["aa-free"]["verdict"] == "unknown"
    assert rows["aa-free"]["status"] is None
    assert "transport" in rows["aa-free"]["reason"]


# ── settings hook (`_ensure_free_models_sync` × gate DI) ───────────────


def _probe_doc(probe: dict | None):
    return {
        "free_discovery": {"enabled": True, "probe": probe or {"enabled": False}},
        "models": {},
        "free_model_map": {},
    }


def _isolated_st(monkeypatch, doc):
    monkeypatch.setattr(_st, "_yaml_data", dict(doc))
    monkeypatch.setattr(_st, "MODELS", dict(doc.get("models") or {}))
    monkeypatch.setattr(
        _st, "FREE_MODEL_MAP", dict(doc.get("free_model_map") or {})
    )
    monkeypatch.setattr(_st, "FREE_MODELS", set())
    monkeypatch.setattr(_st, "FREE_MODEL_POOL", [])
    monkeypatch.setattr(_st, "FREE_DISCOVERY_ENABLED", True)
    monkeypatch.setattr(_st, "FREE_DISCOVERY_AUTO_PERSIST", False)
    monkeypatch.setattr(
        _st,
        "_FREE_DISCOVERY_STATE",
        {
            "last_refresh": None,
            "next_refresh": None,
            "source": "none",
            "consecutive_failures": 0,
            "removed": [],
            "detected": [],
        },
    )
    return _st


def _fake_fetch(monkeypatch, announced):
    def fake(*args, **kwargs):
        return set(announced), "fake", []

    monkeypatch.setattr(_disc, "fetch_free_models_sync", fake)


class _FakeGate:
    def __init__(self, accepted=None, state=None, raises=False):
        self.calls = []
        self.accepted = accepted
        self.state = state
        self.raises = raises

    def __call__(self, announced, known, api_base_free):
        self.calls.append((set(announced), set(known), api_base_free))
        if self.raises:
            raise RuntimeError("gate boom")
        return self.accepted, self.state


def test_hook_gate_filtre_avant_apply(monkeypatch):
    _isolated_st(monkeypatch, _probe_doc({"enabled": True}))
    _fake_fetch(monkeypatch, ["alive-free", "ghost-free"])
    gate = _FakeGate(accepted={"alive-free"}, state={"run_at": "t1"})
    monkeypatch.setattr(_st, "_FREE_PROBE_GATE", gate)
    added = _st._ensure_free_models_sync()
    assert added == 1
    assert "alive-free" in _st.FREE_MODELS
    assert "ghost-free" not in _st.FREE_MODELS
    assert "ghost-free" not in _st.MODELS  # jamais ajouté, jamais persisté
    assert gate.calls[0][0] == {"alive-free", "ghost-free"}
    assert gate.calls[0][2] == API_BASE_FREE
    assert _st._FREE_DISCOVERY_STATE["probe"] == {"run_at": "t1"}


def test_hook_sans_gate_comportement_historique(monkeypatch):
    _isolated_st(monkeypatch, _probe_doc(None))
    _fake_fetch(monkeypatch, ["a-free", "b-free"])
    monkeypatch.setattr(_st, "_FREE_PROBE_GATE", None)
    assert _st._ensure_free_models_sync() == 2
    # [CORRECTION 2026-10-20] La decouverte ne possede plus FREE_MODELS seule :
    # les ids verifies en direct (`_EXPOSED_VERIFIED_IDS`, ex. big-pickle) sont
    # re-ajoutes apres chaque passe, sinon la passe suivante les effacait et le
    # pipeline les renvoyait sur la voie payante. On compare donc la PART
    # decouverte ; l'overlay a son propre test (test_free_models_exposed.py).
    assert _st.FREE_MODELS - _st._EXPOSED_VERIFIED_IDS == {"a-free", "b-free"}
    assert "probe" not in _st._FREE_DISCOVERY_STATE


def test_hook_gate_non_un_set_ne_fait_rien(monkeypatch):
    _isolated_st(monkeypatch, _probe_doc(None))
    _fake_fetch(monkeypatch, ["a-free", "b-free"])
    monkeypatch.setattr(_st, "_FREE_PROBE_GATE", _FakeGate(accepted=None, state=None))
    # accepted None → l'apply garde l'annonce complète (comportement historique)
    assert _st._ensure_free_models_sync() == 2
    # [CORRECTION 2026-10-20] La decouverte ne possede plus FREE_MODELS seule :
    # les ids verifies en direct (`_EXPOSED_VERIFIED_IDS`, ex. big-pickle) sont
    # re-ajoutes apres chaque passe, sinon la passe suivante les effacait et le
    # pipeline les renvoyait sur la voie payante. On compare donc la PART
    # decouverte ; l'overlay a son propre test (test_free_models_exposed.py).
    assert _st.FREE_MODELS - _st._EXPOSED_VERIFIED_IDS == {"a-free", "b-free"}


def test_hook_gate_levee_fallback_announce_only(monkeypatch):
    _isolated_st(monkeypatch, _probe_doc({"enabled": True}))
    _fake_fetch(monkeypatch, ["a-free", "b-free"])
    monkeypatch.setattr(_st, "_FREE_PROBE_GATE", _FakeGate(raises=True))
    assert _st._ensure_free_models_sync() == 2
    # [CORRECTION 2026-10-20] La decouverte ne possede plus FREE_MODELS seule :
    # les ids verifies en direct (`_EXPOSED_VERIFIED_IDS`, ex. big-pickle) sont
    # re-ajoutes apres chaque passe, sinon la passe suivante les effacait et le
    # pipeline les renvoyait sur la voie payante. On compare donc la PART
    # decouverte ; l'overlay a son propre test (test_free_models_exposed.py).
    assert _st.FREE_MODELS - _st._EXPOSED_VERIFIED_IDS == {"a-free", "b-free"}
    assert "probe" not in _st._FREE_DISCOVERY_STATE


def test_config_yaml_probe_enabled():
    """La config réelle du dépôt sonde activée (déploiement)."""
    import yaml as _yaml

    raw = _yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    probe = (raw.get("free_discovery") or {}).get("probe")
    assert isinstance(probe, dict) and probe.get("enabled") is True
    assert float(probe.get("timeout", 8)) >= 2.0
    assert int(probe.get("concurrency", 3)) >= 1
    assert int(probe.get("max_ids", 10)) >= 1


# ── gate runtime (opencode) — orchestration ──────────────────────────


def _gate_env(monkeypatch, probe_over: dict | None = None):
    import opencode as oc

    base = {
        "enabled": True,
        "timeout": 4.0,
        "concurrency": 2,
        "max_ids": 8,
        "dead_ttl_s": 3600.0,
        "recheck_interval_s": 60.0,
        "skip_recent_ok_s": 0.0,  # OFF par défaut dans l'env de test
        "dead_status": [404, 410],
    }
    if probe_over:
        base.update(probe_over)
    _doc = {"free_discovery": {"probe": dict(base)}}

    def _fake_yaml_get(section, key=None, default=None):
        sub = _doc.get(section)
        if key is None:
            return sub if sub is not None else default
        val = (sub or {}).get(key, default) if isinstance(sub, dict) else default
        return val if val else default

    monkeypatch.setattr(oc, "yaml_get", _fake_yaml_get)
    monkeypatch.setattr(
        oc._cfg_settings,
        "_FREE_DISCOVERY_STATE",
        {"last_refresh": None, "source": "fake", "consecutive_failures": 0},
    )
    monkeypatch.setattr(oc._cfg_settings, "GO_ONLY_IDS", set())
    monkeypatch.setattr(oc._cfg_settings, "PURGED_IDS", set())
    return oc


def _fake_dispatch(monkeypatch, oc, mapping):
    seen = []
    calls_state = {"seen": seen}

    def dispatch(url, headers, payload):
        mid = json.loads(payload)["model"]
        seen.append({"mid": mid, "url": url, "headers": dict(headers)})
        return mapping[mid], ""

    monkeypatch.setattr(oc, "_free_probe_dispatch", dispatch)
    return calls_state


def _probe_state_write(oc):
    """Mimic du hook settings : la gate rend le state, l'hôte le remet."""

    def _writer(accepted, state):
        if isinstance(state, dict):
            oc._cfg_settings._FREE_DISCOVERY_STATE["probe"] = state
        return True

    return _writer


def test_gate_promue_alive_limited_et_fige_dead(monkeypatch):
    oc = _gate_env(monkeypatch)
    mapping = {
        "good-free": 200,
        "angry-free": 429,
        "bad404-free": 404,
        "muse-spark-1.4-free": 200,
    }
    seen_holder = _fake_dispatch(monkeypatch, oc, mapping)
    announced = {"good-free", "angry-free", "bad404-free", "muse-spark-1.4-free"}
    acc, state = oc._free_probe_gate(announced, set(), API_BASE_FREE)
    assert acc == {"good-free", "angry-free", "muse-spark-1.4-free"}
    assert "bad404-free" not in acc
    assert state["summary"]["dead"] == 1
    assert state["summary"].get("alive", 0) == 2
    assert state["summary"].get("limited", 0) == 1
    # face officielle transmise à la sonde (chat + responses selon id)
    chat_seen = next(
        s for s in seen_holder["seen"] if s["mid"] == "good-free"
    )
    assert chat_seen["headers"]["Authorization"] == "Bearer public"
    assert "provider-utils/4.0.23" in chat_seen["headers"]["User-Agent"]
    resp_seen = next(
        s for s in seen_holder["seen"] if s["mid"] == "muse-spark-1.4-free"
    )
    assert "4.0.40" in resp_seen["headers"]["User-Agent"]
    assert "responses" in resp_seen["url"]
    # ── cycle suivant : le dead est gelé (pas re-sondé), les vivants connus ──
    state_writer = _probe_state_write(oc)
    state_writer(acc, state)
    seen_h2 = _fake_dispatch(monkeypatch, oc, mapping)
    acc2, state2 = oc._free_probe_gate(announced, acc, API_BASE_FREE)
    assert "bad404-free" not in acc2
    assert acc2 >= {"good-free", "angry-free", "muse-spark-1.4-free"}
    assert set(state2["dead_frozen"]) == {"bad404-free"}
    assert state2["probed"] == []


def test_gate_recheck_unknown_perime(monkeypatch):
    oc = _gate_env(monkeypatch)
    stale_free = {"stale-free": {"verdict": "unknown", "checked_epoch": time.time() - 10000.0}}
    oc._cfg_settings._FREE_DISCOVERY_STATE["probe"] = {"results": stale_free}
    seen_holder = _fake_dispatch(monkeypatch, oc, {"stale-free": 200})
    acc, state = oc._free_probe_gate({"stale-free"}, set(), API_BASE_FREE)
    assert acc == {"stale-free"}
    assert [s["mid"] for s in seen_holder["seen"]] == ["stale-free"]


def test_gate_skip_usage_recent_traffic_source(monkeypatch):
    oc = _gate_env(monkeypatch)
    monkeypatch.setattr(
        oc, "_free_probe_recently_ok", lambda ids, seconds: {"usage-free"}
    )
    seen_holder = _fake_dispatch(monkeypatch, oc, {})
    acc, state = oc._free_probe_gate({"usage-free"}, set(), API_BASE_FREE)
    assert acc == {"usage-free"}  # trafic réel = signal vivant
    assert seen_holder["seen"] == []  # PAS sondé
    r = state["results"]["usage-free"]
    assert r["verdict"] == "alive" and r["source"] == "traffic"


def test_gate_budget_max_ids(monkeypatch):
    oc = _gate_env(monkeypatch, probe_over={"max_ids": 2})
    mapping = {"aa-free": 200, "bb-free": 200, "cc-free": 200}
    seen_holder = _fake_dispatch(monkeypatch, oc, mapping)
    acc, state = oc._free_probe_gate(
        {"aa-free", "bb-free", "cc-free"}, set(), API_BASE_FREE
    )
    assert acc == {"aa-free", "bb-free"}  # cc hors budget → pas promu ce cycle
    assert state["probed"] and set(state["probed"]) == {"aa-free", "bb-free"}


def test_gate_pas_unknow_exclu(monkeypatch):
    oc = _gate_env(monkeypatch)
    _fake_dispatch(monkeypatch, oc, {"gone-free": 401, "shaky-free": 503})
    acc, state = oc._free_probe_gate({"gone-free", "shaky-free"}, set(), API_BASE_FREE)
    assert acc == set()  # 401 refused ; 503 unknown → pas promu ce cycle
    assert state["summary"].get("not_free", 0) == 1
    assert state["summary"].get("unknown", 0) == 1


def test_gate_sonde_desactivee_retour_none(monkeypatch):
    oc = _gate_env(monkeypatch, probe_over={"enabled": False})
    acc, state = oc._free_probe_gate({"aa-free"}, set(), API_BASE_FREE)
    assert acc is None and state is None  # comportement historique


def test_gate_di_push_effectif():
    import opencode as oc

    assert oc._cfg_settings._FREE_PROBE_GATE is oc._free_probe_gate
