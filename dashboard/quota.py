"""
OpenCode Go quota fetcher and parser.

Fetches subscription quota data (5h rolling, weekly, monthly)
from the OpenCode Go workspace page by scraping embedded JS objects.
"""

import os
import re
import json
import time
import asyncio
import logging
import httpx

from .events import get_event_manager

logger = logging.getLogger(__name__)

# Shared HTTP client for quota fetcher (reused across calls, avoids fd leaks)
_http_client: httpx.AsyncClient | None = None


async def _get_http_client() -> httpx.AsyncClient:
    """Get or create a shared httpx client for the quota fetcher."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30, follow_redirects=True)
    return _http_client

# ── Env var validation ──

_WORKSPACE_ID_ENV = "OPENCODE_GO_WORKSPACE_ID"
_AUTH_COOKIE_ENV = "OPENCODE_GO_AUTH_COOKIE"

# ── Per-model capabilities ──

MODEL_CAPABILITIES: dict[str, list[str]] = {
    "glm-5.1":          ["chat", "tools", "vision"],
    "glm-5":            ["chat", "tools", "vision"],
    "kimi-k2.5":        ["chat", "tools", "vision", "code", "web-search"],
    "kimi-k2.6":        ["chat", "tools", "vision", "code", "web-search"],
    "deepseek-v4-pro":  ["chat", "tools", "code"],
    "deepseek-v4-flash":["chat", "tools", "code"],
    "mimo-v2-pro":      ["chat", "tools"],
    "mimo-v2-omni":     ["chat", "tools", "vision"],
    "mimo-v2.5-pro":    ["chat", "tools", "vision"],
    "mimo-v2.5":        ["chat", "tools", "vision"],
    "minimax-m2.7":     ["chat", "tools", "vision"],
    "minimax-m2.5":     ["chat", "tools", "vision"],
    "qwen3.6-plus":     ["chat", "tools", "vision", "code", "web-search"],
    "qwen3.5-plus":     ["chat", "tools", "vision", "code", "web-search"],
}

# ── Per-model estimated request limits ──
# Fetched from docs at startup; fallback if offline.

_MODEL_LIMITS_FALLBACK: dict[str, list[int]] = {
    "glm-5.1":          [880,   2150,   4300],
    "glm-5":            [1150,  2880,   5750],
    "kimi-k2.5":        [1850,  4630,   9250],
    "kimi-k2.6":        [1150,  2880,   5750],
    "deepseek-v4-pro":  [3450,  8550,  17150],
    "deepseek-v4-flash":[31650, 79050, 158150],
    "mimo-v2-pro":      [1290,  3225,   6450],
    "mimo-v2-omni":     [2150,  5450,  10900],
    "mimo-v2.5-pro":    [1290,  3225,   6450],
    "mimo-v2.5":        [2150,  5450,  10900],
    "minimax-m2.7":     [3400,  8500,  17000],
    "minimax-m2.5":     [6300,  15900, 31800],
    "qwen3.6-plus":     [3300,  8200,  16300],
    "qwen3.5-plus":     [10200, 25200, 50500],
}

_model_limits_cache: dict[str, list[int]] | None = None
_model_limits_lock: asyncio.Lock | None = None


def _get_model_limits_lock() -> asyncio.Lock:
    global _model_limits_lock
    if _model_limits_lock is None:
        _model_limits_lock = asyncio.Lock()
    return _model_limits_lock

DOCS_URL = "https://opencode.ai/docs/fr/go/"
MODELS_URL = "https://opencode.ai/zen/go/v1/models"

# ── Upstream model discovery cache ──

_models_cache: list[str] | None = None

API_BASE_OPENAI    = "https://opencode.ai/zen/go/v1/chat/completions"
API_BASE_ANTHROPIC = "https://opencode.ai/zen/go/v1/messages"


async def fetch_available_models() -> list[str]:
    """Fetch model IDs from the upstream OpenCode /v1/models endpoint."""
    try:
        client = await _get_http_client()
        resp = await client.get(MODELS_URL)
        if resp.status_code != 200:
            raise RuntimeError(f"Models endpoint HTTP {resp.status_code}")
        data = resp.json()
        ids = sorted(set(m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m))
        if not ids:
            raise RuntimeError("No models returned from upstream")
        return ids
    except Exception as e:
        logger.warning("Failed to fetch available models: %s", e)
        raise


def get_available_models() -> dict:
    """Return merged model info: local config + auto-discovered upstream."""
    from config.settings import MODELS
    result = {}
    for mid, cfg in MODELS.items():
        result[mid] = {
            "protocol": cfg["protocol"],
            "endpoint": cfg["endpoint"].rsplit("/", 1)[-1],
            "source": "local",
        }
    if _models_cache:
        for mid in _models_cache:
            if mid not in result:
                if mid.startswith("minimax-m"):
                    ep = API_BASE_ANTHROPIC
                    proto = "anthropic"
                else:
                    ep = API_BASE_OPENAI
                    proto = "openai"
                result[mid] = {
                    "protocol": proto,
                    "endpoint": ep.rsplit("/", 1)[-1],
                    "source": "upstream",
                }
    return result


# ── In-memory quota cache ──

_caches: dict[str, dict] = {}
_cache_lock = asyncio.Lock()


def _new_cache(status="not_configured", error=None):
    return {
        "status": status,
        "error": error,
        "fetched_at": None,
        "quotas": {
            "rolling": {"usage_percent": 0, "reset_in_sec": 0},
            "weekly": {"usage_percent": 0, "reset_in_sec": 0},
            "monthly": {"usage_percent": 0, "reset_in_sec": 0},
        },
    }


def get_configured_workspaces() -> list[dict]:
    """Return list of API key configs that have workspace_id + auth_cookie."""
    try:
        from config.settings import API_KEYS
        return [
            k for k in API_KEYS
            if k.get("go_workspace_id") and re.match(r"^wrk_[A-Za-z0-9_-]+$", k["go_workspace_id"])
               and k.get("go_auth_cookie") and len(k["go_auth_cookie"]) >= 10
        ]
    except (ImportError, AttributeError):
        return []


# ── Model limits fetcher ──

async def fetch_model_limits() -> dict[str, list[int]]:
    """Fetch per-model request limits from the OpenCode docs page."""
    try:
        client = await _get_http_client()
        resp = await client.get(DOCS_URL)

        if resp.status_code != 200:
            raise RuntimeError(f"Docs page returned HTTP {resp.status_code}")

        html = resp.text

        # Find the first table — it contains the model limits
        table_match = re.search(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
        if not table_match:
            raise RuntimeError("No table found in docs page")

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(1), re.DOTALL)
        if not rows:
            raise RuntimeError("No rows found in docs table")

        result: dict[str, list[int]] = {}

        for row in rows[1:]:  # skip header
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
            if len(cells) < 4:
                continue

            # Clean HTML tags from cells
            name = re.sub(r"<[^>]+>", "", cells[0]).strip()
            if not name:
                continue

            # Normalize model name: strip parenthetical notes, lowercase, spaces→hyphens
            name_clean = re.sub(r"\s*\(.*?\)\s*", "", name).strip()
            model_id = name_clean.lower().replace(" ", "-")

            # Parse numbers (French format: "2,150" → 2150)
            try:
                limits = [
                    int(re.sub(r"[^\d]", "", cells[1])),
                    int(re.sub(r"[^\d]", "", cells[2])),
                    int(re.sub(r"[^\d]", "", cells[3])),
                ]
            except (ValueError, IndexError):
                continue

            result[model_id] = limits

        if not result:
            raise RuntimeError("No model limits parsed from docs table")

        logger.info("Fetched model limits for %d models from docs", len(result))
        return result
    except Exception as e:
        logger.warning("Failed to fetch model limits from docs: %s", e)
        raise


def get_model_limits() -> dict[str, list[int]]:
    """Return model limits: fetched cache topped up with fallback defaults."""
    base = dict(_MODEL_LIMITS_FALLBACK)
    if _model_limits_cache:
        base.update(_model_limits_cache)
    return base


# ── Capability estimation for unknown models ──


def _estimate_capabilities(model_id: str) -> list[str]:
    """Estimate capabilities for unknown models based on name patterns."""
    caps = ["chat", "tools"]
    m = model_id.lower()
    if any(x in m for x in ("vision", "omni", "vl", "visual")):
        caps.append("vision")
    if any(x in m for x in ("code", "coder", "deepseek")):
        caps.append("code")
    if "search" in m:
        caps.append("web-search")
    return caps


def get_model_capabilities_for_all(models: dict) -> dict[str, list[str]]:
    """Return capabilities for every model — known caps + heuristic estimation."""
    result = {}
    for mid in models:
        if mid in MODEL_CAPABILITIES:
            result[mid] = MODEL_CAPABILITIES[mid]
        else:
            result[mid] = _estimate_capabilities(mid)
    return result


# ── Limit estimation for unknown models ──


def _estimate_limits(model_id: str) -> list[int]:
    """Estimate request limits for unknown models based on name patterns."""
    m = model_id.lower()
    if "flash" in m:
        return [30000, 75000, 150000]
    return [1000, 2500, 5000]


def get_model_limits_for_all(models: dict) -> dict[str, list[int]]:
    """Return limits for every model — real values topped up with estimation."""
    base = get_model_limits()
    result = {}
    for mid in models:
        if mid in base:
            result[mid] = base[mid]
        else:
            result[mid] = _estimate_limits(mid)
    return result


# ── HTML parsing ──

_OBJECT_START_PATTERNS = [
    # Order matters: most specific first
    lambda name: re.compile(rf'{re.escape(name)}\s*:\s*\$R\[\d+\]\s*=\s*\{{'),
    lambda name: re.compile(rf'"{re.escape(name)}"\s*:\s*\{{'),
    lambda name: re.compile(rf"'{re.escape(name)}'\s*:\s*\{{"),
    lambda name: re.compile(rf'{re.escape(name)}\s*:\s*\{{'),
    lambda name: re.compile(rf'{re.escape(name)}\s*=\s*\{{'),
]


def _is_string_char(ch: str, quote: str) -> bool:
    return quote != "" and ch != quote


def _read_object_literal(text: str, start_pos: int) -> str:
    """Extract a balanced { ... } object literal starting at start_pos.

    Handles single-quoted, double-quoted, and backtick strings with escapes.
    """
    if text[start_pos] != "{":
        raise ValueError("start_pos must point to '{'")

    depth = 0
    quote = ""
    escaped = False
    for i in range(start_pos, len(text)):
        ch = text[i]

        if escaped:
            escaped = False
            continue

        if ch == "\\" and quote:
            escaped = True
            continue

        if quote:
            if ch == quote:
                quote = ""
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            continue

        if ch == "{":
            depth += 1

        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_pos : i + 1]

    raise ValueError("Unbalanced object literal (no closing '}' found)")


def _normalize_js_object(raw: str) -> str:
    """Normalize a loose JS object literal into valid JSON."""
    # Quote unquoted keys: `{foo:` or `,foo:` → `{"foo":`
    s = re.sub(r'([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)', r'\1"\2"\3', raw)
    # Single-quoted strings → double-quoted
    s = re.sub(r"'((?:\\.|[^'\\])*)'", lambda m: '"' + m.group(1).replace('"', '\\"') + '"', s)
    # Bare undefined → null (but not inside strings)
    s = re.sub(r'(?:"(?:[^"\\]|\\.)*")|\bundefined\b', lambda m: m.group(0) if m.group(0).startswith('"') else "null", s)
    # Trailing commas before } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)
    return s


def _parse_window(raw: str) -> dict | None:
    """Parse a single JS object literal into {usage_percent, reset_in_sec}."""
    try:
        normalized = _normalize_js_object(raw)
        obj = json.loads(normalized)
        usage = obj.get("usagePercent")
        reset = obj.get("resetInSec")
        if usage is None and reset is None:
            return None
        return {
            "usage_percent": max(0.0, min(100.0, float(usage or 0))),
            "reset_in_sec": max(0, int(round(float(reset or 0)))),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _extract_field(html: str, field_name: str) -> dict | None:
    """Try to extract a named field object from HTML."""
    for pattern_factory in _OBJECT_START_PATTERNS:
        pattern = pattern_factory(field_name)
        m = pattern.search(html)
        if m:
            start = m.end() - 1  # point to the opening `{`
            try:
                raw_obj = _read_object_literal(html, start)
                return _parse_window(raw_obj)
            except (ValueError, IndexError):
                continue
    return None


def parse_quota_html(html: str) -> dict:
    """Parse OpenCode Go workspace HTML for all three quota windows.

    Returns::
        {
            "rolling":  {"usage_percent": ..., "reset_in_sec": ...},
            "weekly":   {"usage_percent": ..., "reset_in_sec": ...},
            "monthly":  {"usage_percent": ..., "reset_in_sec": ...},
        }
    Raises ValueError if none of the three windows can be parsed.
    """
    result = {}
    for field in ("rollingUsage", "weeklyUsage", "monthlyUsage"):
        key = field[0].lower() + field[1:].replace("Usage", "")
        w = _extract_field(html, field)
        if w:
            result[key] = w

    if not result:
        raise ValueError(
            "Could not parse quota data from the OpenCode Go page. "
            "The page format may have changed."
        )

    # Fill in any missing windows with zeroed data
    for key in ("rolling", "weekly", "monthly"):
        result.setdefault(key, {"usage_percent": 0.0, "reset_in_sec": 0})

    return result


# ── HTTP fetch ──

QUOTA_FETCH_INTERVAL = 300  # 5 minutes


async def fetch_quotas(workspace_id: str, auth_cookie: str) -> dict:
    """Fetch and parse quota data from opencode.ai for a given workspace."""
    from urllib.parse import quote
    url = f"https://opencode.ai/workspace/{quote(workspace_id, safe='')}/go"

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": f"auth={auth_cookie}",
        "User-Agent": "opencode-proxy/1.0",
    }

    client = await _get_http_client()
    resp = await client.get(url, headers=headers)

    if resp.status_code in (401, 403):
        logger.debug("[quota] auth failed for workspace %s (HTTP %d)", workspace_id[:8], resp.status_code)
        raise RuntimeError("OpenCode Go authentication failed. Refresh your auth cookie.")
    if resp.status_code != 200:
        raise RuntimeError(f"OpenCode Go request failed with HTTP {resp.status_code}.")

    return parse_quota_html(resp.text)


# ── API accessor ──

def get_quota_snapshot() -> dict:
    """Return a JSON-serializable snapshot for the API endpoint."""
    return dict(_caches)


# ── Background poller ──

async def start_quota_fetcher(app):
    """Start a background task that polls OpenCode Go quotas every 5 minutes
    and fetches model limits from docs at startup.

    The poller always runs and checks env vars dynamically at each cycle,
    so config changes (workspace ID, auth cookie) take effect without restart.
    """
    logger.debug("[quota] start_quota_fetcher called")

    # Fetch model limits + available models on startup
    async def _startup_fetch():
        global _models_cache

        # Model limits from docs
        try:
            limits = await fetch_model_limits()
            async with _get_model_limits_lock():
                global _model_limits_cache
                _model_limits_cache = limits
        except Exception:
            logger.info("Using fallback model limits (docs fetch failed)")

        # Available models from upstream
        try:
            models = await fetch_available_models()
            _models_cache = models
            logger.debug("[quota] discovered %d models from upstream", len(models))
            logger.info("Discovered %d models from upstream", len(models))
        except Exception:
            logger.info("Using local models only (upstream fetch failed)")

    await _startup_fetch()

    async def _poll():
        while True:
            # Periodically refresh upstream model list (every cycle = ~5 min)
            try:
                models = await fetch_available_models()
                old_cache: list[str] = _models_cache or []
                if set(models) != set(old_cache):
                    _models_cache = models
                    logger.info("Upstream model list changed: %d models", len(models))
                    get_event_manager().publish("models_updated", {"count": len(models)})
            except Exception:
                pass

            workspaces = get_configured_workspaces()
            active_ids = {k["go_workspace_id"] for k in workspaces}

            # Remove caches for workspaces no longer configured
            async with _cache_lock:
                for wid in list(_caches.keys()):
                    if wid not in active_ids:
                        del _caches[wid]

            if not workspaces:
                await asyncio.sleep(QUOTA_FETCH_INTERVAL)
                continue

            for ws in workspaces:
                wid = ws["go_workspace_id"]
                cookie = ws["go_auth_cookie"]
                try:
                    quotas = await fetch_quotas(wid, cookie)
                    async with _cache_lock:
                        _caches[wid] = {
                            "status": "ok",
                            "error": None,
                            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "quotas": quotas,
                        }
                    get_event_manager().publish("quotas_updated", {"workspace_id": wid, "status": "ok"})
                    logger.debug("Quotas refreshed for workspace %s", wid[:8])
                except Exception as e:
                    logger.warning("Quota fetch failed for workspace %s: %s", wid[:8], e)
                    async with _cache_lock:
                        if wid in _caches:
                            _caches[wid]["status"] = "error"
                            _caches[wid]["error"] = str(e)
                        else:
                            _caches[wid] = _new_cache("error", str(e))
                    get_event_manager().publish("quotas_updated", {"workspace_id": wid, "status": "error", "error": str(e)})

            await asyncio.sleep(QUOTA_FETCH_INTERVAL)

    # Cancel existing task if called again (double-invocation guard)
    existing = getattr(app.state, '_quota_task', None)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_poll())
    app.state._quota_task = task
