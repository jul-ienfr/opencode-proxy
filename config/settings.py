import os
import json
import logging
import time
import threading

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
ENV_PATH = os.path.join(ROOT, ".env")
CUSTOM_ROUTES_PATH = os.path.join(ROOT, "custom_routes.json")
API_KEYS_PATH = os.path.join(ROOT, "api_keys.json")
TOOL_CAPABILITIES_PATH = os.path.join(ROOT, "tool_capabilities.json")

# Config keys safe to expose via API (not secrets)
CONFIG_KEYS = ["OPENCODE_PROXY", "OPENCODE_HOST", "OPENCODE_PORT", "OPENCODE_WEB_PORT",
               "OPUS_MAP_MODEL", "SONNET_MAP_MODEL", "HAIKU_MAP_MODEL", "DISABLE_MAPPING"]


# ── YAML Config Loader ───────────────────────────────────────────────

_yaml_data = {}


def load_yaml_config() -> dict:
    """Load config.yaml as the primary configuration source."""
    global _yaml_data
    try:
        import yaml
    except ImportError:
        logger.warning("[config] pyyaml not installed — falling back to .env only")
        _yaml_data = {}
        return {}

    if not os.path.exists(CONFIG_PATH):
        logger.info("[config] config.yaml not found at %s — using .env defaults", CONFIG_PATH)
        _yaml_data = {}
        return {}

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _yaml_data = yaml.safe_load(f) or {}
        logger.info("[config] loaded config.yaml (%d top-level keys)", len(_yaml_data))
        return _yaml_data
    except Exception as e:
        logger.error("[config] failed to load config.yaml: %s", e)
        _yaml_data = {}
        return {}


def save_yaml_config():
    """Write current config back to config.yaml."""
    global _yaml_data
    try:
        import yaml
    except ImportError:
        return
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(_yaml_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    logger.debug("[config] saved config.yaml")


def yaml_get(section: str, key: str = None, default=None):
    """Read a value from the YAML config. Returns default if missing."""
    section_data = _yaml_data.get(section, {})
    if key is None:
        return section_data if isinstance(section_data, dict) else (section_data or default)
    if isinstance(section_data, dict):
        return section_data.get(key, default)
    return default


def yaml_set(section: str, key: str, value):
    """Write a value to the YAML config and persist."""
    if section not in _yaml_data or not isinstance(_yaml_data.get(section), dict):
        _yaml_data[section] = {}
    _yaml_data[section][key] = value
    save_yaml_config()


# ── .env Loader (override YAML defaults) ─────────────────────────────

def load_env_file():
    """Load environment variables from .env file if it exists."""
    if not os.path.exists(ENV_PATH):
        return
    count = 0
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key not in os.environ:
                    os.environ[key] = value
                    count += 1
    logger.debug("[config] load_env_file: loaded %d new vars from .env", count)


def _env(key: str, default=None):
    """Read env var, falling back to default."""
    val = os.getenv(key)
    if val is not None:
        return val
    return default


def _env_bool(key: str, default=False):
    val = os.getenv(key)
    if val is not None:
        return val.lower() in ("1", "true", "yes")
    return default


def _env_int(key: str, default=0):
    val = os.getenv(key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            logging.warning("Invalid integer for %s=%r, using default %d", key, val, default)
    return default


# ── Initialize config ────────────────────────────────────────────────

load_env_file()
load_yaml_config()

# ── Secrets (YAML → .env override) ──────────────────────────────────
PROXY = _env("OPENCODE_PROXY", yaml_get("upstream", "proxy", ""))
API_KEY = _env("OPENCODE_API_KEY", "")
OPENCODE_GO_WORKSPACE_ID = _env("OPENCODE_GO_WORKSPACE_ID", "")
OPENCODE_GO_AUTH_COOKIE = _env("OPENCODE_GO_AUTH_COOKIE", "")
OPENCODE_GO_USE_BALANCE = _env_bool("OPENCODE_GO_USE_BALANCE", True)
API_KEY_ROUTING = _env("API_KEY_ROUTING", yaml_get("routing", "key_routing", "round-robin"))
CACHE_MIN_PROMPT_SIZE = _env_int("CACHE_MIN_PROMPT_SIZE", yaml_get("cache", "min_prompt_size", 2000))
DEBUG = _env_bool("OPENCODE_DEBUG", yaml_get("server", "debug", False))

# ── Upstream endpoints ──────────────────────────────────────────────
API_BASE_OPENAI = yaml_get("upstream", "openai_base", "https://opencode.ai/zen/go/v1/chat/completions")
API_BASE_ANTHROPIC = yaml_get("upstream", "anthropic_base", "https://opencode.ai/zen/go/v1/messages")
API_BASE_FREE = yaml_get("upstream", "free_base", "https://opencode.ai/zen/v1/chat/completions")
API_BASE_RESPONSES = yaml_get("upstream", "responses_base", "https://opencode.ai/zen/go/v1/responses")
API_BASE_RESPONSES_FREE = yaml_get("upstream", "responses_free_base", "https://opencode.ai/zen/v1/responses")

# ── Free model mapping (paid → free equivalent) ────────────────────
FREE_MODEL_MAP = yaml_get("free_model_map", default={})

# ── IP rotation (OpenVPN for free model quota) ────────────────────
IP_ROTATION = yaml_get("ip_rotation", default={})

# ── Server ──────────────────────────────────────────────────────────
HOST = _env("OPENCODE_HOST", yaml_get("server", "host", "0.0.0.0"))
PORT = _env_int("OPENCODE_PORT", yaml_get("server", "port", 4000))
WEB_PORT = _env_int("OPENCODE_WEB_PORT", yaml_get("server", "web_port", 8082))

# ── Model family prefix → protocol mapping ─────────────────────────
# Used by _fetch_upstream_models() to assign the correct protocol
# to auto-discovered models from the upstream API.
# When opencode.ai adds a new model family, add its prefix here.
KNOWN_PROTOCOLS = {
    # OpenAI protocol models
    "glm":      "openai",
    "kimi":     "openai",
    "deepseek": "openai",
    "mimo":     "openai",
    "muse":     "openai",
    "spark":    "openai",
    # Anthropic protocol models
    "minimax":  "anthropic",
    "qwen":     "anthropic",
}


def _resolve_protocol(model_id: str) -> str:
    """Resolve the protocol for a model ID using KNOWN_PROTOCOLS.

    Extracts the family prefix (first token before '-' or '.', then strips
    trailing digits) and looks it up in KNOWN_PROTOCOLS.
    Falls back to "openai" if unknown.

    Examples:
        "kimi-k2.7"    -> "kimi"  -> "openai"
        "qwen3.7-plus" -> "qwen"  -> "anthropic"
        "glm-5.2"      -> "glm"   -> "openai"
    """
    import re
    prefix = model_id.split("-")[0].split(".")[0].lower()
    prefix = re.sub(r"\d+$", "", prefix)  # "qwen3" -> "qwen"
    return KNOWN_PROTOCOLS.get(prefix, "openai")


# ── Models ──────────────────────────────────────────────────────────
_models_cfg = yaml_get("models", default={})
MODELS = {}
for _model_id, _model_data in _models_cfg.items():
    if isinstance(_model_data, dict):
        _proto = _model_data.get("protocol", "openai")
        _endpoint = API_BASE_OPENAI if _proto == "openai" else API_BASE_ANTHROPIC
        MODELS[_model_id] = {"endpoint": _endpoint, "protocol": _proto}

def _fetch_upstream_models():
    """Fetch available models from upstream API and add them to MODELS."""
    import subprocess
    try:
        url = f"{API_BASE_OPENAI.rsplit('/chat/completions', 1)[0]}/models"
        result = subprocess.run(
            ["curl", "-s", "--max-time", "10", url],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            raise Exception(f"curl failed: {result.stderr}")
        data = json.loads(result.stdout)
        models = data.get("data", [])
        added = 0
        for m in models:
            model_id = m.get("id", "")
            if model_id and model_id not in MODELS:
                proto = _resolve_protocol(model_id)
                endpoint = API_BASE_OPENAI if proto == "openai" else API_BASE_ANTHROPIC
                MODELS[model_id] = {"endpoint": endpoint, "protocol": proto}
                added += 1
        logger.info("[config] upstream models: fetched %d, added %d new", len(models), added)
    except Exception as e:
        logger.warning("[config] upstream models fetch failed: %s", e)

_fetch_upstream_models()

# ── Routing ─────────────────────────────────────────────────────────
DISABLE_MAPPING = _env_bool("DISABLE_MAPPING", yaml_get("routing", "disable_mapping", False))
ALIASES = yaml_get("routing", "aliases", {"nimo": "mimo"})


def load_routes():
    """Load ROUTES from YAML config or use default.

    Custom routes take priority over auto-generated routes: if a custom route
    matches the same model name as an auto-generated route, the auto-generated
    one is removed. This ensures custom mappings (e.g. mimo-v2.5 → glm-5.2)
    are not shadowed by the default identity mapping.
    """
    routes = {}
    if not DISABLE_MAPPING:
        alias_reverse = {}
        for alias, canonical in ALIASES.items():
            alias_reverse.setdefault(canonical, []).append(alias)

        for model_id in MODELS:
            key = model_id.replace("-", "").replace(".", "").replace("_", "")
            match_keywords = [model_id]
            prefix = model_id.split("-")[0]
            for alias in alias_reverse.get(prefix, []):
                match_keywords.append(model_id.replace(prefix, alias, 1))
            routes[key] = {"match": match_keywords, "model": model_id}

    # Collect all match patterns from custom routes
    custom_match_patterns = set()
    for value in CUSTOM_ROUTES.values():
        if isinstance(value, dict):
            for m in value.get("match", []):
                custom_match_patterns.add(m.lower())

    # Remove auto-generated routes whose match patterns overlap with custom routes
    if custom_match_patterns:
        keys_to_remove = []
        for key, route in routes.items():
            if isinstance(route, dict):
                for m in route.get("match", []):
                    if m.lower() in custom_match_patterns:
                        keys_to_remove.append(key)
                        break
        for key in keys_to_remove:
            del routes[key]

    # Custom user-defined routes (added AFTER cleanup so they always survive)
    for key, value in CUSTOM_ROUTES.items():
        routes[key] = value

    # Model route overrides
    routes["opus"]   = {"match": ["opus"],   "model": _env("OPUS_MAP_MODEL", yaml_get("routing", "opus_model", "kimi-k2.6"))}
    routes["sonnet"] = {"match": ["sonnet"], "model": _env("SONNET_MAP_MODEL", yaml_get("routing", "sonnet_model", "glm-5.1"))}
    routes["haiku"]  = {"match": ["haiku"],  "model": _env("HAIKU_MAP_MODEL", yaml_get("routing", "haiku_model", "minimax-m2.5"))}
    logger.debug("[config] load_routes: %d routes loaded", len(routes))
    return routes


# ── Custom Routes ───────────────────────────────────────────────────

def load_custom_routes() -> dict:
    """Load custom routes from YAML or JSON file."""
    # Try YAML first
    yaml_routes = yaml_get("custom_routes", default={})
    if yaml_routes:
        return yaml_routes
    # Fallback to JSON file
    if os.path.exists(CUSTOM_ROUTES_PATH):
        try:
            with open(CUSTOM_ROUTES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.warning("Failed to load custom_routes.json: %s", e)
    return {}


def save_custom_routes(routes: dict):
    """Save custom routes to YAML config and reload ROUTES."""
    global SORTED_ROUTES, SORTED_CUSTOM_ROUTES
    _yaml_data.setdefault("custom_routes", {}).update(routes)
    # Also keep JSON file for backward compat
    with open(CUSTOM_ROUTES_PATH, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2)
    with _reload_lock:
        CUSTOM_ROUTES.clear()
        CUSTOM_ROUTES.update(routes)
        ROUTES.clear()
        ROUTES.update(load_routes())
        SORTED_ROUTES = _sort_routes_by_match(ROUTES)
        SORTED_CUSTOM_ROUTES = _sort_routes_by_match(CUSTOM_ROUTES)
    save_yaml_config()


# ── Tool Capabilities ───────────────────────────────────────────────

def load_tool_capabilities() -> dict:
    """Load tool capabilities from YAML or JSON file."""
    yaml_tc = yaml_get("tool_capabilities", default={})
    if yaml_tc:
        return yaml_tc
    if os.path.exists(TOOL_CAPABILITIES_PATH):
        try:
            with open(TOOL_CAPABILITIES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.warning("Failed to load tool_capabilities.json: %s", e)
    return {}


def save_tool_capabilities(capabilities: dict):
    """Save tool capabilities to YAML config."""
    _yaml_data["tool_capabilities"] = capabilities
    save_yaml_config()
    TOOL_CAPABILITIES.clear()
    TOOL_CAPABILITIES.update(capabilities)


def get_tool_config(model_id: str) -> dict:
    """Return tool config for a model, falling back to _default."""
    defaults = {"supported_tools": None, "unsupported_tools": [], "system_hint": None, "fallback_model": None}
    model_cfg = TOOL_CAPABILITIES.get(model_id, {})
    default_cfg = TOOL_CAPABILITIES.get("_default", {})
    return {**defaults, **default_cfg, **model_cfg}


# ── API Keys ────────────────────────────────────────────────────────

def load_api_keys() -> list[dict]:
    """Load API key configs from YAML or JSON. Falls back to .env single-key."""
    yaml_keys = yaml_get("api_keys", default=[])
    if yaml_keys:
        for i, k in enumerate(yaml_keys):
            if not k.get("alias"):
                k["alias"] = f"Compte {i+1}"
        return yaml_keys
    # Fallback to JSON file
    if os.path.exists(API_KEYS_PATH):
        try:
            with open(API_KEYS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    for i, k in enumerate(data):
                        if not k.get("alias"):
                            k["alias"] = f"Compte {i+1}"
                    return data
        except Exception as e:
            logging.warning("Failed to load api_keys.json: %s", e)
    # Fallback: single key from .env
    if API_KEY:
        return [{"api_key": API_KEY,
                 "go_workspace_id": OPENCODE_GO_WORKSPACE_ID,
                 "go_auth_cookie": OPENCODE_GO_AUTH_COOKIE}]
    return []


def save_api_keys(configs: list[dict]):
    """Save API key configs to YAML."""
    _yaml_data["api_keys"] = configs
    save_yaml_config()
    API_KEYS[:] = configs  # Atomic replacement — readers never see empty list


# ── Module-level state ──────────────────────────────────────────────

CUSTOM_ROUTES = load_custom_routes()
ROUTES = load_routes()
API_KEYS = load_api_keys()
TOOL_CAPABILITIES = load_tool_capabilities()


# ── Hot-reload: Custom Routes ───────────────────────────────────────

def _get_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


_custom_routes_mtime = _get_mtime(CUSTOM_ROUTES_PATH)
_custom_routes_last_check = 0.0
_CUSTOM_ROUTES_CHECK_INTERVAL = yaml_get("background", "custom_routes_check_interval", 5)
_reload_lock = threading.Lock()


def _sort_routes_by_match(routes: dict) -> list:
    """Return routes sorted by longest match pattern first (most specific)."""
    return sorted(
        routes.values(),
        key=lambda r: max((len(m) for m in r.get("match", [])), default=0),
        reverse=True
    )


# Pre-sorted route lists (rebuilt on load/reload)
SORTED_ROUTES = _sort_routes_by_match(ROUTES)
SORTED_CUSTOM_ROUTES = _sort_routes_by_match(CUSTOM_ROUTES)


def maybe_reload_custom_routes():
    """Re-read custom_routes.json if modified. Rate-limited, thread-safe."""
    global _custom_routes_mtime, _custom_routes_last_check, SORTED_ROUTES, SORTED_CUSTOM_ROUTES
    now = time.time()
    if now - _custom_routes_last_check < _CUSTOM_ROUTES_CHECK_INTERVAL:
        return
    _custom_routes_last_check = now
    try:
        mtime = _get_mtime(CUSTOM_ROUTES_PATH)
        if mtime == _custom_routes_mtime:
            return
        _custom_routes_mtime = mtime

        new_cr = load_custom_routes()
        with _reload_lock:
            old_cr_keys = set(CUSTOM_ROUTES.keys())
            new_cr_keys = set(new_cr.keys())
            for k in (new_cr_keys - old_cr_keys) | (new_cr_keys & old_cr_keys):
                CUSTOM_ROUTES[k] = new_cr[k]
            for k in old_cr_keys - new_cr_keys:
                del CUSTOM_ROUTES[k]

            new_routes = load_routes()
            old_r_keys = set(ROUTES.keys())
            new_r_keys = set(new_routes.keys())
            for k in (new_r_keys - old_r_keys) | (new_r_keys & old_r_keys):
                ROUTES[k] = new_routes[k]
            for k in old_r_keys - new_r_keys:
                del ROUTES[k]

            SORTED_ROUTES = _sort_routes_by_match(ROUTES)
            SORTED_CUSTOM_ROUTES = _sort_routes_by_match(CUSTOM_ROUTES)

        logging.info("Reloaded custom_routes.json (%d routes)", len(ROUTES))
    except Exception as e:
        logging.warning("Failed to reload custom_routes.json: %s", e)


# ── Hot-reload: Tool Capabilities ───────────────────────────────────

_tool_cap_mtime = _get_mtime(TOOL_CAPABILITIES_PATH)
_tool_cap_last_check = 0.0
_TOOL_CAP_CHECK_INTERVAL = yaml_get("background", "tool_cap_check_interval", 5)
_tool_cap_lock = threading.Lock()


def maybe_reload_tool_capabilities():
    """Re-read tool_capabilities.json if modified. Rate-limited, thread-safe."""
    global _tool_cap_mtime, _tool_cap_last_check
    now = time.time()
    if now - _tool_cap_last_check < _TOOL_CAP_CHECK_INTERVAL:
        return
    _tool_cap_last_check = now
    try:
        mtime = _get_mtime(TOOL_CAPABILITIES_PATH)
        if mtime == _tool_cap_mtime:
            return
        _tool_cap_mtime = mtime

        new_tc = load_tool_capabilities()
        with _tool_cap_lock:
            old_keys = set(TOOL_CAPABILITIES.keys())
            new_keys = set(new_tc.keys())
            for k in (new_keys - old_keys) | (new_keys & old_keys):
                TOOL_CAPABILITIES[k] = new_tc[k]
            for k in old_keys - new_keys:
                del TOOL_CAPABILITIES[k]
    except Exception as e:
        logging.warning("Failed to reload tool_capabilities.json: %s", e)


def get_model_config(model_id: str) -> dict:
    """Return merged config for model_id with sensible defaults."""
    cfg = MODELS.get(model_id, {})
    defaults = {"endpoint": API_BASE_OPENAI, "protocol": "openai"}
    return {**defaults, **cfg}


# ── Runtime updates (called by dashboard API) ───────────────────────

def save_env(updates: dict):
    """Update .env file and apply values at runtime."""
    existing = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    existing[key.strip()] = value.strip()

    existing.update(updates)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        for key, value in existing.items():
            f.write(f"{key}={value}\n")

    for key, value in updates.items():
        os.environ[key] = value
        if key == "OPENCODE_PROXY":
            global PROXY; PROXY = value
        elif key == "OPENCODE_API_KEY":
            global API_KEY; API_KEY = value
        elif key == "OPENCODE_GO_WORKSPACE_ID":
            global OPENCODE_GO_WORKSPACE_ID; OPENCODE_GO_WORKSPACE_ID = value
        elif key == "OPENCODE_GO_AUTH_COOKIE":
            global OPENCODE_GO_AUTH_COOKIE; OPENCODE_GO_AUTH_COOKIE = value
        elif key == "API_KEY_ROUTING":
            global API_KEY_ROUTING; API_KEY_ROUTING = value
        elif key == "DISABLE_MAPPING":
            global DISABLE_MAPPING; DISABLE_MAPPING = value.lower() in ("1", "true", "yes")
        elif key == "OPENCODE_HOST":
            global HOST; HOST = value
        elif key == "OPENCODE_DEBUG":
            global DEBUG; DEBUG = value.lower() in ("1", "true", "yes")

    global ROUTES
    ROUTES = load_routes()
    logger.debug("[config] save_env: applied %d vars", len(updates))


def apply_server_changes(port=None, web_port=None, host=None):
    """Update HOST, PORT, WEB_PORT at runtime."""
    global HOST, PORT, WEB_PORT
    if host is not None:
        HOST = host
    if port is not None:
        PORT = int(port)
    if web_port is not None:
        WEB_PORT = int(web_port)
