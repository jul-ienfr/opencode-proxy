import os
import json

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
CUSTOM_ROUTES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "custom_routes.json")

# Config keys that are safe to expose via the API (not secrets)
CONFIG_KEYS = ["OPENCODE_PROXY", "OPENCODE_HOST", "OPENCODE_PORT", "OPENCODE_WEB_PORT",
               "OPUS_MAP_MODEL", "SONNET_MAP_MODEL", "HAIKU_MAP_MODEL", "DISABLE_MAPPING"]

def load_env_file():
    """Load environment variables from .env file if it exists."""
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key not in os.environ:
                        os.environ[key] = value

load_env_file()

# Secrets (from .env or environment variables)
PROXY = os.getenv("OPENCODE_PROXY") or ""
API_KEY = os.getenv("OPENCODE_API_KEY") or ""
OPENCODE_GO_WORKSPACE_ID = os.getenv("OPENCODE_GO_WORKSPACE_ID", "")
OPENCODE_GO_AUTH_COOKIE = os.getenv("OPENCODE_GO_AUTH_COOKIE", "")

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
    "minimax-m2.5"     : {"endpoint": API_BASE_ANTHROPIC, "protocol": "anthropic"},
    "qwen3.6-plus"     : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
    "qwen3.5-plus"     : {"endpoint": API_BASE_OPENAI,    "protocol": "openai"},
}


def load_routes():
    """Load ROUTES from environment variables or use default."""
    disable = os.getenv("DISABLE_MAPPING", "").lower() in ("1", "true", "yes")
    if disable:
        return {}

    # Aliases: user-provided name → canonical prefix
    ALIASES = {
        "nimo":  "mimo",
    }
    # Build reverse map: canonical prefix → list of alias prefixes
    alias_reverse = {}
    for alias, canonical in ALIASES.items():
        alias_reverse.setdefault(canonical, []).append(alias)

    routes = {}
    for model_id in MODELS:
        key = model_id.replace("-", "").replace(".", "").replace("_", "")
        match_keywords = [model_id]
        prefix = model_id.split("-")[0]
        for alias in alias_reverse.get(prefix, []):
            match_keywords.append(model_id.replace(prefix, alias, 1))
        routes[key] = {"match": match_keywords, "model": model_id}

    # Custom user-defined routes
    for key, value in CUSTOM_ROUTES.items():
        routes[key] = value

    routes["opus"]   = {"match": ["opus"],   "model": os.getenv("OPUS_MAP_MODEL", "kimi-k2.6")}
    routes["sonnet"] = {"match": ["sonnet"], "model": os.getenv("SONNET_MAP_MODEL", "glm-5.1")}
    routes["haiku"]  = {"match": ["haiku"],  "model": os.getenv("HAIKU_MAP_MODEL", "minimax-m2.5")}
    return routes


def load_custom_routes():
    """Load custom routes from JSON file."""
    if os.path.exists(CUSTOM_ROUTES_PATH):
        try:
            with open(CUSTOM_ROUTES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_custom_routes(routes: dict):
    """Save custom routes to JSON file and reload ROUTES."""
    with open(CUSTOM_ROUTES_PATH, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2)
    CUSTOM_ROUTES.clear()
    CUSTOM_ROUTES.update(routes)
    ROUTES.clear()
    ROUTES.update(load_routes())


CUSTOM_ROUTES = load_custom_routes()
ROUTES = load_routes()
DISABLE_MAPPING = os.getenv("DISABLE_MAPPING", "").lower() in ("1", "true", "yes")


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
        elif key == "DISABLE_MAPPING":
            global DISABLE_MAPPING
            DISABLE_MAPPING = value.lower() in ("1", "true", "yes")

    # Refresh routes
    global ROUTES
    ROUTES = load_routes()


def apply_port_changes(port=None, web_port=None):
    """Update PORT and WEB_PORT at runtime. Returns (new_port, new_web_port)."""
    global PORT, WEB_PORT
    if port is not None:
        PORT = int(port)
    if web_port is not None:
        WEB_PORT = int(web_port)
    return PORT, WEB_PORT