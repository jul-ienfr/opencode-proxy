import os
import json
import logging
import time

logger = logging.getLogger(__name__)

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
CUSTOM_ROUTES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "custom_routes.json")
API_KEYS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_keys.json")

# Config keys that are safe to expose via the API (not secrets)
CONFIG_KEYS = ["OPENCODE_PROXY", "OPENCODE_HOST", "OPENCODE_PORT", "OPENCODE_WEB_PORT",
               "OPUS_MAP_MODEL", "SONNET_MAP_MODEL", "HAIKU_MAP_MODEL", "DISABLE_MAPPING"]

def load_env_file():
    """Load environment variables from .env file if it exists."""
    if not os.path.exists(ENV_PATH):
        logger.debug("[config] load_env_file: .env not found at %s", ENV_PATH)
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

load_env_file()

# Secrets (from .env or environment variables)
PROXY = os.getenv("OPENCODE_PROXY") or ""
API_KEY = os.getenv("OPENCODE_API_KEY") or ""
OPENCODE_GO_WORKSPACE_ID = os.getenv("OPENCODE_GO_WORKSPACE_ID", "")
OPENCODE_GO_AUTH_COOKIE = os.getenv("OPENCODE_GO_AUTH_COOKIE", "")
API_KEY_ROUTING = os.getenv("API_KEY_ROUTING", "round-robin")
CACHE_MIN_PROMPT_SIZE = int(os.getenv("CACHE_MIN_PROMPT_SIZE", "2000"))
DEBUG = os.getenv("OPENCODE_DEBUG", "").lower() in ("1", "true", "yes")

API_BASE_OPENAI    = "https://opencode.ai/zen/go/v1/chat/completions"
API_BASE_ANTHROPIC = "https://opencode.ai/zen/go/v1/messages"
HOST = os.getenv("OPENCODE_HOST", "0.0.0.0")
PORT = int(os.getenv("OPENCODE_PORT", "4000"))
WEB_PORT = int(os.getenv("OPENCODE_WEB_PORT", "8082"))

MODELS = {
    "glm-5.1"          : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "glm-5"            : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "kimi-k2.5"        : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "kimi-k2.6"        : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "deepseek-v4-pro"  : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "deepseek-v4-flash": {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "mimo-v2-pro"      : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "mimo-v2-omni"     : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "mimo-v2.5-pro"    : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "mimo-v2.5"        : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "minimax-m2.7"     : {"endpoint": API_BASE_ANTHROPIC, "protocol": "anthropic"},
    "minimax-m3"       : {"endpoint": API_BASE_ANTHROPIC, "protocol": "anthropic"},
    "minimax-m2.5"     : {"endpoint": API_BASE_ANTHROPIC, "protocol": "anthropic"},
    "qwen3.7-max"      : {"endpoint": API_BASE_ANTHROPIC, "protocol": "anthropic"},
    "qwen3.6-plus"     : {"endpoint": API_BASE_ANTHROPIC, "protocol": "anthropic"},
    "qwen3.5-plus"     : {"endpoint": API_BASE_ANTHROPIC, "protocol": "anthropic"},
}


def load_routes():
    """Load ROUTES from environment variables or use default."""
    disable = os.getenv("DISABLE_MAPPING", "").lower() in ("1", "true", "yes")
    routes = {}
    if not disable:
        # Aliases: user-provided name → canonical prefix
        ALIASES = {
            "nimo":  "mimo",
        }
        # Build reverse map: canonical prefix → list of alias prefixes
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

    # Custom user-defined routes (applied even when mapping is disabled)
    for key, value in CUSTOM_ROUTES.items():
        routes[key] = value

    # Always apply model route overrides
    routes["opus"]   = {"match": ["opus"],   "model": os.getenv("OPUS_MAP_MODEL", "kimi-k2.6")}
    routes["sonnet"] = {"match": ["sonnet"], "model": os.getenv("SONNET_MAP_MODEL", "glm-5.1")}
    routes["haiku"]  = {"match": ["haiku"],  "model": os.getenv("HAIKU_MAP_MODEL", "minimax-m2.5")}
    logger.debug("[config] load_routes: %d routes loaded (DISABLE_MAPPING=%s)", len(routes), disable)
    return routes


def load_custom_routes():
    """Load custom routes from JSON file."""
    if os.path.exists(CUSTOM_ROUTES_PATH):
        try:
            with open(CUSTOM_ROUTES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    logger.debug("[config] load_custom_routes: loaded %d custom routes", len(data))
                    return data
        except Exception as e:
            logging.warning("Failed to load custom_routes.json: %s", e)
    return {}


def save_custom_routes(routes: dict):
    """Save custom routes to JSON file and reload ROUTES."""
    with open(CUSTOM_ROUTES_PATH, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2)
    CUSTOM_ROUTES.clear()
    CUSTOM_ROUTES.update(routes)
    ROUTES.clear()
    ROUTES.update(load_routes())
    logger.debug("[config] save_custom_routes: saved %d custom routes and reloaded ROUTES", len(routes))


def load_api_keys() -> list[dict]:
    """Load API key configs from JSON. Falls back to .env single-key."""
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
        logger.debug("[config] load_api_keys: no api_keys.json, falling back to .env key")
        return [{"api_key": API_KEY,
                 "go_workspace_id": OPENCODE_GO_WORKSPACE_ID,
                 "go_auth_cookie": OPENCODE_GO_AUTH_COOKIE}]
    logger.debug("[config] load_api_keys: no keys found")
    return []


def save_api_keys(configs: list[dict]):
    """Save API key configs to JSON."""
    with open(API_KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(configs, f, indent=2, ensure_ascii=False)
    API_KEYS.clear()
    API_KEYS.extend(configs)
    logger.debug("[config] save_api_keys: saved %d API key configs", len(configs))


CUSTOM_ROUTES = load_custom_routes()
ROUTES = load_routes()
DISABLE_MAPPING = os.getenv("DISABLE_MAPPING", "").lower() in ("1", "true", "yes")
API_KEYS = load_api_keys()


def _get_mtime():
    try:
        return os.path.getmtime(CUSTOM_ROUTES_PATH)
    except OSError:
        return 0.0


_custom_routes_mtime = _get_mtime()
_custom_routes_last_check = 0.0
_CUSTOM_ROUTES_CHECK_INTERVAL = 5  # seconds between file stat checks


def maybe_reload_custom_routes():
    """Re-read custom_routes.json if it has been modified since last load.

    Rate-limited to check the filesystem at most once every 5 seconds
    to avoid syscall overhead under high request rates.
    """
    global _custom_routes_mtime, _custom_routes_last_check
    now = time.time()
    if now - _custom_routes_last_check < _CUSTOM_ROUTES_CHECK_INTERVAL:
        logger.debug("[config] maybe_reload_custom_routes: skipped (within check interval)")
        return
    _custom_routes_last_check = now
    try:
        mtime = _get_mtime()
        if mtime == _custom_routes_mtime:
            logger.debug("[config] maybe_reload_custom_routes: skipped (mtime unchanged)")
            return
        _custom_routes_mtime = mtime

        new_cr = load_custom_routes()
        old_cr_keys = set(CUSTOM_ROUTES.keys())
        new_cr_keys = set(new_cr.keys())
        added_cr = new_cr_keys - old_cr_keys
        removed_cr = old_cr_keys - new_cr_keys
        updated_cr = new_cr_keys & old_cr_keys
        for k in added_cr:
            CUSTOM_ROUTES[k] = new_cr[k]
        for k in updated_cr:
            CUSTOM_ROUTES[k] = new_cr[k]
        for k in removed_cr:
            del CUSTOM_ROUTES[k]

        new_routes = load_routes()
        old_r_keys = set(ROUTES.keys())
        new_r_keys = set(new_routes.keys())
        added_r = new_r_keys - old_r_keys
        removed_r = old_r_keys - new_r_keys
        updated_r = new_r_keys & old_r_keys
        for k in added_r:
            ROUTES[k] = new_routes[k]
        for k in updated_r:
            ROUTES[k] = new_routes[k]
        for k in removed_r:
            del ROUTES[k]

        logger.debug("[config] maybe_reload_custom_routes: reloaded -- custom_routes: +%d/-%d/~%d  routes: +%d/-%d/~%d  total=%d",
                      len(added_cr), len(removed_cr), len(updated_cr),
                      len(added_r), len(removed_r), len(updated_r),
                      len(ROUTES))
        logging.info("Reloaded custom_routes.json (%d routes)", len(ROUTES))
    except Exception as e:
        logging.warning("Failed to reload custom_routes.json: %s", e)


def get_model_config(model_id: str) -> dict:
    """Return merged config for model_id with sensible defaults."""
    cfg = MODELS.get(model_id, {})
    defaults = {"endpoint": API_BASE_OPENAI, "protocol": "openai"}
    return {**defaults, **cfg}


def save_env(updates: dict):
    """Update .env file with new values and apply them at runtime."""
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

    # Apply to current process
    for key, value in updates.items():
        os.environ[key] = value
        if key == "OPENCODE_PROXY":
            global PROXY
            PROXY = value
        elif key == "OPENCODE_API_KEY":
            global API_KEY
            API_KEY = value
        elif key == "OPENCODE_GO_WORKSPACE_ID":
            global OPENCODE_GO_WORKSPACE_ID
            OPENCODE_GO_WORKSPACE_ID = value
        elif key == "OPENCODE_GO_AUTH_COOKIE":
            global OPENCODE_GO_AUTH_COOKIE
            OPENCODE_GO_AUTH_COOKIE = value
        elif key == "API_KEY_ROUTING":
            global API_KEY_ROUTING
            API_KEY_ROUTING = value
        elif key == "DISABLE_MAPPING":
            global DISABLE_MAPPING
            DISABLE_MAPPING = value.lower() in ("1", "true", "yes")
        elif key == "OPENCODE_HOST":
            global HOST
            HOST = value

    # Refresh routes
    global ROUTES
    ROUTES = load_routes()
    logger.debug("[config] save_env: applied %d env vars: %s", len(updates), list(updates.keys()))


def apply_server_changes(port=None, web_port=None, host=None):
    """Update HOST, PORT, WEB_PORT at runtime."""
    global HOST, PORT, WEB_PORT
    if host is not None:
        HOST = host
    if port is not None:
        PORT = int(port)
    if web_port is not None:
        WEB_PORT = int(web_port)
    logger.debug("[config] apply_server_changes: port=%s web_port=%s host=%s", PORT, WEB_PORT, HOST)