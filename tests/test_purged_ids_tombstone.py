"""test_purged_ids_tombstone.py — tombeaux de purge `free_discovery.purged_ids`.

La découverte auto est **add-only** à deux étages
(`config/discovery.py:apply_discovered_free_models` ajoute tout id annoncé en
amont ; `persist_free_mappings` fusionne `MODELS` dans `models:` sans jamais
retirer). Retirer à la main un id mort de `models:` ne tenait donc que jusqu'au
refresh suivant : la découverte le ré-ajoutait en mémoire, puis le persist le
ré-écrivait sur le disque — le même piège documenté dans
`docs/models-protocol.md` pour la purge du 2026-09-09 (`kimi-k2.7`, `hy3-free`…).

`free_discovery.purged_ids` est la liste de tombeaux, calquée sur
`go_only_ids` (`tests/test_go_only_routing.py`) : filtre avant le delta-check,
retrait de `models`/`FREE_MODELS`, garde au persist, rechargement à chaud.

Mécanique GÉNÉRIQUE uniquement : ids factices `acme-*`, aucun id live — seuls
les tests marqués « inventaire » lisent le `config.yaml` réel.
"""

import pytest
from test_hot_reload import isolated_yaml  # noqa: F401  (fixture partagée : yaml complet isolé)

import config.settings as st

_PURGED = "acme-dead-free"
_LIVE = "acme-live-free"


# ── Snapshot / restauration (pattern test_go_only_routing.py) ───────


def _snapshot():
    return {
        "purged": set(st.PURGED_IDS),
        "models": {k: dict(v) for k, v in st.MODELS.items()},
        "free": set(st.FREE_MODELS),
        "pool_obj": st.FREE_MODEL_POOL,
        "map": dict(st.FREE_MODEL_MAP),
        "state": dict(st._FREE_DISCOVERY_STATE),
    }


def _restore(snap):
    st.PURGED_IDS.clear()
    st.PURGED_IDS.update(snap["purged"])
    st.MODELS.clear()
    st.MODELS.update(snap["models"])
    st.FREE_MODELS.clear()
    st.FREE_MODELS.update(snap["free"])
    st.FREE_MODEL_POOL = snap["pool_obj"]
    st.FREE_MODEL_MAP.clear()
    st.FREE_MODEL_MAP.update(snap["map"])
    st._FREE_DISCOVERY_STATE.clear()
    st._FREE_DISCOVERY_STATE.update(snap["state"])


def _seed_purged(monkeypatch, purged=_PURGED):
    """PURGED_IDS isolé (config.yaml réel non lu) — restauré par monkeypatch."""
    monkeypatch.setattr(st, "PURGED_IDS", {purged})


# ── 1. Id découvert en amont : jamais entré dans le pool anonyme ────


def test_discovered_purged_id_never_enters_pool(monkeypatch):
    snap = _snapshot()
    try:
        _seed_purged(monkeypatch)
        added = st._apply_discovered_free_models({_PURGED, _LIVE}, source="test")
        assert isinstance(added, int)
        assert _PURGED not in st.FREE_MODELS
        assert _PURGED not in st.FREE_MODEL_POOL
        assert _PURGED not in st.MODELS
        assert _LIVE in st.FREE_MODELS
        assert _LIVE in st.FREE_MODEL_POOL
        assert st.MODELS[_LIVE]["endpoint"] == st.API_BASE_FREE
        # jamais mappé comme cible d'un homonyme payant non plus
        assert not any(v == _PURGED for v in st.FREE_MODEL_MAP.values())
    finally:
        _restore(snap)


def test_purged_id_added_when_tombstone_absent(monkeypatch):
    """Contre-épreuve : sans tombeau, l'id entre normalement (add-only)."""
    snap = _snapshot()
    try:
        monkeypatch.setattr(st, "PURGED_IDS", set())
        st._apply_discovered_free_models({_PURGED}, source="test")
        assert _PURGED in st.FREE_MODELS
        assert _PURGED in st.FREE_MODEL_POOL
    finally:
        _restore(snap)


# ── 2. Id déjà en mémoire (process démarré avant la purge) ──────────


def test_purged_id_already_in_models_is_dropped(monkeypatch):
    snap = _snapshot()
    try:
        _seed_purged(monkeypatch)
        # état « process vivant d'avant la purge » : id encore résolu + pool
        st.MODELS[_PURGED] = {"endpoint": st.API_BASE_FREE, "protocol": "openai"}
        st.FREE_MODELS.add(_PURGED)
        st.FREE_MODEL_POOL = sorted(st.FREE_MODELS)
        st.FREE_MODEL_MAP[f"{_PURGED}-paid"] = _PURGED
        st._apply_discovered_free_models({_LIVE}, source="test")
        assert _PURGED not in st.MODELS
        assert _PURGED not in st.FREE_MODELS
        assert _PURGED not in st.FREE_MODEL_POOL
        assert _LIVE in st.FREE_MODELS
    finally:
        _restore(snap)


def test_purge_survives_no_delta_path(monkeypatch):
    """Le retrait doit aussi courir sur le chemin no-delta (early return) :
    sinon un refresh où rien d'autre ne change laisse l'id mort en place."""
    snap = _snapshot()
    try:
        _seed_purged(monkeypatch)
        st.MODELS[_PURGED] = {"endpoint": st.API_BASE_FREE, "protocol": "openai"}
        st.FREE_MODELS.clear()
        st.FREE_MODELS.update({_PURGED, _LIVE})
        st._FREE_DISCOVERY_STATE["detected"] = sorted(st.FREE_MODELS)
        # même ensemble après filtrage des purgés → early return (0, None)
        added = st._apply_discovered_free_models({_PURGED, _LIVE}, source="test")
        assert added == 0
        assert _PURGED not in st.MODELS
        assert _PURGED not in st.FREE_MODELS
        assert _LIVE in st.FREE_MODELS
    finally:
        _restore(snap)


def test_purged_id_not_resolvable_after_drop(monkeypatch):
    """Après retrait, get_model_config retombe sur le repli implicite :
    la purge ne change pas le comportement du routeur (id non routé).

    [PLAN N4] l'endpoint du repli est désormais celui de la SOURCE UNIQUE
    (`_resolve_model_endpoint`), plus choisi à la main : `acme-dead-free` est un
    id `-free`, il retombe donc sur la jambe **anonyme** — là où l'ancien repli
    codait `API_BASE_OPENAI`, la jambe payée, pour absolument tout id absent de
    `models:`. Le point verrouillé reste le même : l'id ne ressort pas avec la
    config qu'on vient de lui retirer (pas de `capabilities` résiduelle).
    """
    snap = _snapshot()
    try:
        _seed_purged(monkeypatch)
        st.MODELS[_PURGED] = {"endpoint": st.API_BASE_FREE, "protocol": "openai"}
        st._apply_discovered_free_models(set(), source="test")
        cfg = st.get_model_config(_PURGED)
        assert cfg["protocol"] == "openai"
        assert cfg["endpoint"] == st.API_BASE_FREE
        assert "capabilities" not in cfg
        # Le suffixe, et lui seul, décide de la jambe : sans `-free`, repli payé.
        assert st.get_model_config("acme-dead")["endpoint"] == st.API_BASE_OPENAI
    finally:
        _restore(snap)


# ── 2 bis. 3e voie d'ajout : fetch du catalogue upstream ────────────
# `apply_discovered_free_models` (pools anonymes) et `persist_free_mappings`
# (disque) portent le tombeau ; `fetch_upstream_models` — le thread lancé 0,5 s
# après l'import, qui ajoute tout id annoncé par `/models` — l'ignorait. C'est
# la voie qui faisait échouer `test_models_catalog` de façon INTERMITTENTE
# (vert ou rouge selon que le curl avait eu le temps de répondre).


def _fake_upstream_payload(monkeypatch, ids):
    import json as _json
    import types

    from config import discovery

    payload = {"data": [{"id": mid} for mid in ids]}
    monkeypatch.setattr(
        discovery.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=_json.dumps(payload), stderr=""
        ),
    )
    return discovery


def test_fetch_upstream_models_skips_purged_id(monkeypatch):
    discovery = _fake_upstream_payload(monkeypatch, [_PURGED, _LIVE])
    models = {}
    discovery.fetch_upstream_models(
        models,
        "https://acme.test/v1/chat/completions",
        "https://acme.test/v1/messages",
        lambda mid: "openai",
        lambda: None,
        purged_ids_fn=lambda: {_PURGED},
    )
    assert _PURGED not in models
    assert _LIVE in models


def test_fetch_upstream_models_drops_purged_already_in_models(monkeypatch):
    """Process démarré AVANT la purge : l'id mort est déjà dans MODELS."""
    discovery = _fake_upstream_payload(monkeypatch, [_LIVE])
    models = {_PURGED: {"endpoint": "https://acme.test", "protocol": "openai"}}
    discovery.fetch_upstream_models(
        models,
        "https://acme.test/v1/chat/completions",
        "https://acme.test/v1/messages",
        lambda mid: "openai",
        lambda: None,
        purged_ids_fn=lambda: {_PURGED},
    )
    assert _PURGED not in models
    assert _LIVE in models


def test_fetch_upstream_models_without_tombstone_adds_everything(monkeypatch):
    """Contre-épreuve : sans `purged_ids_fn`, le comportement historique
    (add-only) est intact — le filtre est opt-in par l'hôte."""
    discovery = _fake_upstream_payload(monkeypatch, [_PURGED, _LIVE])
    models = {}
    added = discovery.fetch_upstream_models(
        models,
        "https://acme.test/v1/chat/completions",
        "https://acme.test/v1/messages",
        lambda mid: "openai",
        lambda: None,
    )
    assert _PURGED in models
    assert _LIVE in models
    assert added == 2


# ── 3. Persist : ni conserve, ni ré-ajoute (fichier isolé tmp_path) ──


def _isolated(tmp_path, monkeypatch, purged_in_yaml=True):
    from test_free_discovery import _isolated_settings

    yaml_data = {
        "models": {
            "mimo-v2.5": {"protocol": "openai"},
            _PURGED: {"protocol": "openai"},
        },
        "free_model_map": {"mimo-v2.5": "mimo-v2.5-free"},
        "free_discovery": (
            {"purged_ids": [_PURGED]} if purged_in_yaml else {"purged_ids": []}
        ),
    }
    # `_isolated_settings` mute le singleton `config.settings` PARTAGÉ et ne
    # snapshot pas PURGED_IDS : on le fait ici, sinon les tombeaux réels de
    # config.yaml restent vidés pour tout le process pytest (et §4, qui les
    # relit, « passerait » sur une liste vide — vert presque faux).
    real_purged = set(st.PURGED_IDS)
    st2, *old = _isolated_settings(tmp_path, monkeypatch, yaml_data)
    st2.PURGED_IDS.clear()
    st2.PURGED_IDS.update(
        x for x in (yaml_data["free_discovery"].get("purged_ids") or []) if x
    )
    return st2, old, real_purged


def _restore_isolated(st2, old, real_purged):
    from test_free_discovery import _restore_settings

    _restore_settings(st2, old)
    st2.PURGED_IDS.clear()
    st2.PURGED_IDS.update(real_purged)


def test_persist_drops_purged_id_from_config_models(tmp_path, monkeypatch):
    import yaml as _yaml

    st2, old, real_purged = _isolated(tmp_path, monkeypatch)
    try:
        st2._persist_free_mappings()
        on_disk = _yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert _PURGED not in on_disk.get("models", {})
        assert "mimo-v2.5" in on_disk["models"]
        assert on_disk["free_model_map"] == {"mimo-v2.5": "mimo-v2.5-free"}
    finally:
        _restore_isolated(st2, old, real_purged)


def test_persist_does_not_readd_purged_id_from_memory(tmp_path, monkeypatch):
    """Même si MODELS porte encore l'id (process pré-purge), le persist le
    saute au lieu de le ré-écrire dans `models:`."""
    import yaml as _yaml

    st2, old, real_purged = _isolated(tmp_path, monkeypatch)
    try:
        st2.MODELS[_PURGED] = {"endpoint": "https://x", "protocol": "openai"}
        st2._persist_free_mappings()
        on_disk = _yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert _PURGED not in on_disk.get("models", {})
    finally:
        _restore_isolated(st2, old, real_purged)


# ── 4. Inventaire réel : les 5 morts sont tombautés dans config.yaml ─


def test_real_config_tombstones_the_dead_ids():
    """Ce qui rend `tests/test_models_catalog.py::test_ids_purges_absents_de_models`
    hermétique : sans la liste, la découverte add-only ré-ajoute les ids morts."""
    from test_models_catalog import PURGES

    assert PURGES, "inventaire vide"
    missing = [m for m in PURGES if m not in st.PURGED_IDS]
    assert not missing, f"ids morts absents de free_discovery.purged_ids: {missing}"


def test_dead_ids_absent_after_discovery_roundtrip():
    """Régression exacte du vert-presque-faux : unite la liste de tombeaux au
    filtre de découverte (apply) — les ids morts ne reviennent plus."""
    from test_models_catalog import PURGES

    dry = {m: {"protocol": "openai"} for m in PURGES}
    dry["mimo-v2.5-free"] = {"protocol": "openai"}
    snapped = dict(st.MODELS)
    try:
        st.MODELS.clear()
        st.MODELS.update(dry)
        added = st._apply_discovered_free_models(set(PURGES) | {"mimo-v2.5-free"}, source="test")
        for model_id in PURGES:
            assert model_id not in st.MODELS, f"{model_id} est revenu dans models:"
            assert model_id not in st.FREE_MODELS
            assert model_id not in st.FREE_MODEL_POOL
        assert "mimo-v2.5-free" in st.FREE_MODELS
        assert isinstance(added, int)
    finally:
        st.MODELS.clear()
        st.MODELS.update(snapped)


# ── 5. Rechargement à chaud ─────────────────────────────────────────


# `isolated_yaml` est importé AU NIVEAU MODULE (plus haut) et utilisé ici comme
# nom de paramètre : c'est ce qui rend la fixture — définie dans
# `tests/test_hot_reload.py`, pas dans un conftest — visible depuis CE module.
# Le rétrécissement est donc voulu, pas une omission : pas de noqa masqué, la
# raison est écrite là où on la cherche.
def test_hot_reload_rebuilds_purged_ids(isolated_yaml):  # noqa: F811
    """Un `purged_ids` édité dans config.yaml doit prendre effet sans restart,
    comme `go_only_ids` (config/settings.py, branche cfg_changed)."""
    import yaml as _yaml
    from test_hot_reload import _cfg as cfg

    before = set(cfg.PURGED_IDS)
    sentinel = "acme-tombstone-hot"
    assert sentinel not in before
    try:
        with open(isolated_yaml, encoding="utf-8") as f:
            doc = _yaml.safe_load(f) or {}
        fd = doc.get("free_discovery")
        if not isinstance(fd, dict):
            fd = {}
            doc["free_discovery"] = fd
        fd["purged_ids"] = sorted(before) + [sentinel]
        with open(isolated_yaml, "w", encoding="utf-8") as f:
            _yaml.safe_dump(doc, f)
        cfg._config_yaml_mtime -= 1  # ≠ mtime réel → force le re-read du YAML
        cfg._custom_routes_last_check = 0.0  # désarme le rate-limit
        cfg.maybe_reload_custom_routes()
        assert sentinel in cfg.PURGED_IDS
        assert before <= cfg.PURGED_IDS
    finally:
        cfg.PURGED_IDS.clear()
        cfg.PURGED_IDS.update(before)


# Une clé absente, mal typée, ou pleine de blancs : rien ne doit entrer.
_BAD_TOMBSTONES = [{}, "acme-x-free", 42, None, ["  ", ""]]


@pytest.mark.parametrize("bad", _BAD_TOMBSTONES)
def test_purged_ids_normalization(bad):
    """Une clé absente/mal typée ne doit ni planter ni créer de faux tombiveau
    (`None` → "none" barrerait un jour un id réel) : rien n'entre."""
    from config.discovery import _normalize_ids

    assert _normalize_ids(bad) == set()


def test_purged_ids_normalization_keeps_real_ids():
    """Les vrais ids sont normalisés (strip + minuscules, dédoublonnés)."""
    from config.discovery import _normalize_ids

    assert _normalize_ids(["  Acme-X-FREE ", "acme-x-free", 42, None, ""]) == {"acme-x-free"}
