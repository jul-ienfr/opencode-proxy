"""Phase 0 refonte — harnais de caractérisation (contrats gelés, ADR-006).

Ces tests figent le COMPORTEMENT OBSERVABLE actuel. Toute PR de refonte
(déplacement pur) doit les garder verts. Ils ne testent aucune logique
métier — seulement les contrats d'interface qui ne doivent JAMAIS changer :

  * routes HTTP exposées (proxy + dashboard + static + health)
  * façade d'imports publics (config, dashboard, opencode shims)
  * schéma SQLite (colonnes requests + free_model_usage, INSERT 32 cols)
  * clés config.yaml / CONFIG_KEYS exposables
  * entrypoint CLI (--no-gui/--gui/--port) et lock mono-instance

Pattern établi : import module-level (test_proxy.py, test_invariant_a0.py),
jamais de boot réseau, jamais de touch sur logs/requests.db live.
"""

import inspect
import sqlite3

import pytest

import opencode as oc

# ── 1. Routes proxy (opencode.py) ─────────────────────────────────────
# Chemins POST/GET définis DIRECTEMENT sur app dans opencode.py.
# Le dashboard enregistre ses propres routes via register_dashboard
# (testées §2) — ici on fige le socle proxy.
PROXY_ROUTES = {
    ("POST", "/v1/messages"),
    ("POST", "/anthropic/v1/messages"),
    ("POST", "/v1/messages/count_tokens"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/models"),
    ("GET", "/health"),
    ("GET", "/metrics"),
    ("GET", "/api/circuit-breakers"),
    ("POST", "/api/circuit-breakers/reset"),
    ("GET", "/api/key-pauses"),
    ("POST", "/api/key-pauses/reset"),
    ("GET", "/api/free-models"),
    ("POST", "/api/free-discovery/refresh"),
}

# Routes dashboard enregistrées par register_dashboard (dashboard/api.py).
# Échantillon représentatif par famille — la liste exhaustive vit dans
# dashboard/api.py ; ce qui compte ici : le préfixe /api/* et la racine /.
DASHBOARD_ROUTES_SAMPLE = {
    ("GET", "/"),
    ("GET", "/api/config"),
    ("GET", "/api/stats"),
    ("GET", "/api/logs"),
    ("GET", "/api/history"),
    ("GET", "/api/vpn-status"),
    ("GET", "/api/quotas"),
    ("GET", "/api/costs"),
    ("GET", "/api/events"),
}


def _route_set(app) -> set:
    found = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        for m in methods:
            if m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                found.add((m, path))
    return found


def test_proxy_routes_present():
    routes = _route_set(oc.app)
    missing = PROXY_ROUTES - routes
    assert not missing, f"routes proxy manquantes (régression): {sorted(missing)}"


def test_dashboard_routes_registered_on_fresh_app():
    from fastapi import FastAPI

    from dashboard import register_dashboard

    sub = FastAPI()
    sig = inspect.signature(register_dashboard)
    assert "app" in sig.parameters, "register_dashboard(app, ...) : param 'app' renommé ?"
    assert "static_dir" in sig.parameters
    # Pas de boot DB : conn=None — on ne fait que monter les routes.
    import os

    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    register_dashboard(sub, static_dir, None)
    routes = _route_set(sub)
    missing = DASHBOARD_ROUTES_SAMPLE - routes
    assert not missing, f"routes dashboard manquantes (régression): {sorted(missing)}"
    # Le dashboard ne doit JAMAIS capturer les chemins proxy cœur.
    for method, path in PROXY_ROUTES:
        if path.startswith("/v1/"):
            assert (method, path) not in routes, f"collision dashboard↔proxy sur {(method, path)}"


def test_static_mount_present():
    # /static servi (mount StaticFiles) — le dashboard GUI en dépend.
    names = [getattr(r, "name", "") for r in oc.app.routes]
    paths = [getattr(r, "path", "") for r in oc.app.routes]
    assert "static" in names or any(str(p).startswith("/static") for p in paths), (
        "mount /static absent — dashboard GUI cassé"
    )


# ── 2. Façades d'imports ──────────────────────────────────────────────
CONFIG_PUBLIC = [
    "API_BASE_OPENAI",
    "API_BASE_ANTHROPIC",
    "API_BASE_FREE",
    "API_KEY",
    "PROXY",
    "HOST",
    "PORT",
    "MODELS",
    "ROUTES",
    "CUSTOM_ROUTES",
    "API_KEYS",
    "FREE_MODEL_MAP",
    "FREE_MODELS",
    "IP_ROTATION",
    "DEBUG",
    "DISABLE_MAPPING",
    "CONFIG_KEYS",
    "get_model_config",
    "maybe_reload_custom_routes",
    "yaml_get",
    "yaml_set",
    "save_yaml_config",
]

DASHBOARD_PUBLIC = ["register_dashboard", "log", "build_display"]


def test_config_facade_stable():
    import config

    for name in CONFIG_PUBLIC:
        assert hasattr(config, name), f"config.{name} a disparu (façade cassée)"
    assert set(CONFIG_PUBLIC) <= set(config.__all__), "config.__all__ ne couvre plus la façade"


def test_dashboard_facade_stable():
    import dashboard

    for name in DASHBOARD_PUBLIC:
        assert hasattr(dashboard, name), f"dashboard.{name} a disparu (façade cassée)"


def test_opencode_entrypoints_stable():
    # Symboles que les tests et lifespan consomment depuis opencode.
    for name in ["app", "lifespan", "_acquire_instance_lock", "get_model_config", "_route_for"]:
        assert hasattr(oc, name), f"opencode.{name} a disparu"


def test_opencode_cli_flags_stable():
    src = inspect.getsource(oc)
    assert '"--no-gui"' in src and '"--gui"' in src and '"--port"' in src, (
        "flags CLI --no-gui/--gui/--port modifiés — contrats README/lanceurs .bat cassés"
    )


# ── 3. Schéma SQLite (app/db, source unique) ──────────────────────────
EXPECTED_REQUEST_COLUMNS = {
    "id",
    "timestamp",
    "model",
    "original_model",
    "duration_ms",
    "tokens_input",
    "tokens_output",
    "tokens_cache",
    "success",
    "error",
    "protocol",
    "is_stream",
    "thinking",
    "effort",
    "client_ip",
    "account_alias",
    "tools",
    "tools_used",
    "request_body",
    "response_body",
    "client_user_agent",
    "free_model_ip",
    "identity",
    "geo_country",
    "geo_blocked",
    "hedged",
    "winner_station",
    "geo_direct_country",
    "geo_direct_ip",
    "geo_via_vpn",
    "geo_allowed",
    "station",
    "free_status",
    "paid_status",
}  # 34 colonnes : 14 de base + 20 migrations + station

EXPECTED_FREE_USAGE_COLUMNS = {
    "id",
    "timestamp",
    "paid_model",
    "free_model",
    "api_key",
    "workspace_id",
    "status",
    "tokens_input",
    "tokens_output",
    "duration_ms",
    "ip",
}


def test_db_schema_columns_frozen():
    """Le schéma est un contrat (dashboard, requêtes SQL, migrations)."""
    from app.db import init_free_usage_schema, init_requests_schema

    conn = sqlite3.connect(":memory:")
    n_naive = init_requests_schema(conn, busy_timeout=5000, cache_size=1000, mmap_size=1024)
    assert isinstance(n_naive, int)
    init_free_usage_schema(conn)
    req_cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
    missing = EXPECTED_REQUEST_COLUMNS - req_cols
    assert not missing, f"colonnes requests manquantes: {sorted(missing)}"
    usage_cols = {r[1] for r in conn.execute("PRAGMA table_info(free_model_usage)")}
    missing_u = EXPECTED_FREE_USAGE_COLUMNS - usage_cols
    assert not missing_u, f"colonnes free_model_usage manquantes: {sorted(missing_u)}"
    conn.close()


def test_db_insert_sql_placeholder_count():
    """L'INSERT requests DOIT avoir 32 placeholders (tuple 32 colonnes)."""
    from app.db import _INSERT_REQUESTS_SQL

    assert _INSERT_REQUESTS_SQL.count("?") == 32, (
        f"INSERT requests: {_INSERT_REQUESTS_SQL.count('?')} placeholders ≠ 32"
    )


# ── 4. Config gelée ───────────────────────────────────────────────────
EXPECTED_CONFIG_KEYS = {
    "OPENCODE_PROXY",
    "OPENCODE_HOST",
    "OPENCODE_PORT",
    "OPUS_MAP_MODEL",
    "SONNET_MAP_MODEL",
    "HAIKU_MAP_MODEL",
    "DISABLE_MAPPING",
}


def test_config_keys_exposable_frozen():
    import config.settings as st

    assert EXPECTED_CONFIG_KEYS <= set(st.CONFIG_KEYS), (
        f"CONFIG_KEYS amputées: {sorted(EXPECTED_CONFIG_KEYS - set(st.CONFIG_KEYS))}"
    )


def test_config_yaml_top_sections_present():
    import yaml

    from config.settings import CONFIG_PATH

    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for section in ("server", "upstream", "routing", "models", "ip_rotation"):
        assert section in data, f"config.yaml: section '{section}' disparue"


def test_mono_instance_lock_message_frozen():
    src = inspect.getsource(oc._acquire_instance_lock)
    assert "another opencode-proxy instance is already running" in src
    assert "opencode-" in inspect.getsource(oc)  # lock suffixé par port (incident 25/08)


# ── 5. Modules satellites importables (non-régression découpage futur) ─
@pytest.mark.parametrize(
    "mod",
    [
        "vpn_manager",
        "protocol_mapping",
        "free_ip_pool",
        "shared_rotation",
        "latency_rotation",
        "traffic_capture",
        "trust",
        "station_supervisor",
        "docker_events",
        "free_discovery",
        "config.settings",
        "dashboard.api",
        "gui",
    ],
)
def test_satellite_module_importable(mod):
    __import__(mod)
