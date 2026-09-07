"""config.discovery — découverte de modèles (Phase 1 refonte).

Déplacé depuis ``config/settings.py`` (déplacement pur) : fetch upstream
(+ thread background explicite), free discovery (urls, detect, fetch,
endpoint, apply, persist).

AUCUN import du projet. ``logger`` = ``"config.settings"`` (nom historique
préservé). Tout l'état du STORE reste possédé par l'hôte
(``config/settings.py``) et PASSÉ EN PARAMÈTRE — les wrappers hôtes
(``_ensure_free_models_sync``, ``_free_endpoint_for``…, signatures
INCHANGÉES) lisent leurs globaux À L'APPEL (tests rebindent/mutent
``settings._yaml_data`` / ``FREE_MODEL_*`` / ``MODELS`` directement).

Notes :
* ``_reload_lock`` RESTE possédé par l'hôte (``save_yaml_config`` le
  résout via ``globals()`` — fix race P4) : passé en paramètre.
* ``get_model_config.cache_clear`` : passé en ``cache_clear_fn`` (défini
  en FIN de settings — lambdas hôtes à résolution tardive).
* ``_CREATE_NO_WINDOW`` déménage ici (seuls usages : les fetch ci-dessous).
* ``_ensure_free_models_async`` + spawns de threads RESTENT côté hôte
  (référencent les wrappers hôtes).
"""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("config.settings")

# Windows: masquer la fenêtre console des subprocess (évite le flash noir 1s)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _noop_log(*args, **kwargs) -> None:
    return None


def fetch_upstream_models(
    models: dict,
    api_base_openai: str,
    api_base_anthropic: str,
    resolve_protocol_fn: Callable[[str], str],
    cache_clear_fn: Callable[[], None],
    timeout: float = 3.0,
    log=logger,
) -> int:
    """Fetch available models from upstream API and add them to MODELS.

    Timeout réduit à 3s (vs 10s avant) pour ne pas bloquer le démarrage.
    Appelé en arrière-plan, jamais bloquant pour le 1er chargement GUI.
    """
    try:
        url = f"{api_base_openai.rsplit('/chat/completions', 1)[0]}/models"
        # --max-time 3s + connect 2s : échec rapide si upstream lent
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)), "--connect-timeout", "2", url],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            creationflags=_CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            raise Exception(f"curl failed: {result.stderr[:200]}")
        data = json.loads(result.stdout)
        data_models = data.get("data", [])
        added = 0
        for m in data_models:
            model_id = m.get("id", "")
            if model_id and model_id not in models:
                proto = resolve_protocol_fn(model_id)
                endpoint = api_base_openai if proto == "openai" else api_base_anthropic
                models[model_id] = {"endpoint": endpoint, "protocol": proto}
                added += 1
        if added:
            # [P4 correctesse] les modèles découverts doivent être visibles
            # immédiatement : sans clear du LRU, get_model_config sert encore
            # les défauts OpenAI stale pour un id absent au moment du 1er appel.
            try:
                cache_clear_fn()
            except Exception:
                pass
        log.info("[config] upstream models: fetched %d, added %d new", len(data_models), added)
        return added
    except Exception as e:
        log.debug("[config] upstream models fetch failed (non-bloquant): %s", e)
        return 0


def start_background_fetch(
    models: dict,
    api_base_openai: str,
    api_base_anthropic: str,
    resolve_protocol_fn: Callable[[str], str],
    cache_clear_fn: Callable[[], None],
    log=logger,
) -> None:
    """Lancement non-bloquant en arrière-plan (daemon thread) — ne retarde pas l'import.

    [Phase 1] Point d'ancrage EXPLICITE pour le lifespan Phase 9
    (``app/composition.py``) : l'hôte l'appelle aujourd'hui au même point
    d'import qu'avant (ordre inchangé), la Phase 9 le déplacera dans le
    lifespan sans changer cette fonction.
    """

    # Lancement non-bloquant en arrière-plan (daemon thread) — ne retarde pas l'import
    def _background():
        try:
            time.sleep(0.5)
            fetch_upstream_models(
                models, api_base_openai, api_base_anthropic, resolve_protocol_fn, cache_clear_fn, timeout=3.0, log=log
            )
        except Exception:
            pass

    try:
        _t = threading.Thread(target=_background, daemon=True, name="upstream-models-fetch")
        _t.start()
        log.debug("[config] upstream fetch lancé en arrière-plan (3s timeout)")
    except Exception as e:
        log.debug("[config] impossible de lancer le thread upstream: %s", e)


def free_discovery_urls(api_base_free: str, api_base_openai: str, yaml_get_fn: Callable) -> list:
    """Union of free discovery URLs (derived from bases, dedup)."""
    override = yaml_get_fn("upstream", "free_models_url", "")
    if isinstance(override, str) and override.strip():
        return [override.strip().rstrip("/")]
    urls = []
    for base in (api_base_free, api_base_openai):
        if not base:
            continue
        b = base.strip()
        if "/chat/completions" in b:
            b = b.rsplit("/chat/completions", 1)[0]
        u = b.rstrip("/") + "/models"
        if u not in urls:
            urls.append(u)
    return urls


def is_free_model(m: dict) -> bool:
    """Cascade: pricing/is_free/free/capabilities.free → suffix -free."""
    if not isinstance(m, dict):
        return False
    mid = m.get("id", "")
    if not isinstance(mid, str) or not mid:
        return False
    pricing = m.get("pricing")
    if isinstance(pricing, dict):
        try:
            inp = pricing.get("input", None)
            out = pricing.get("output", None)
            if inp is not None and out is not None and float(inp) == 0 and float(out) == 0:
                return True
        except Exception:
            pass
    for k in ("is_free", "free"):
        if m.get(k) is True:
            return True
    caps = m.get("capabilities")
    if isinstance(caps, dict) and caps.get("free") is True:
        return True
    if mid.endswith("-free"):
        return True
    return False


# Alias historique (config.settings._is_free_model — ré-export hôte conservé).
_is_free_model = is_free_model


def detect_free_ids(payloads: list) -> set:
    """Extract free ids from a list of /models payloads (cascade)."""
    free = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        if not isinstance(data, list):
            continue
        for m in data:
            if is_free_model(m):
                mid = m.get("id", "")
                if isinstance(mid, str) and mid:
                    free.add(mid)
    return free


# Alias historique (config.settings._detect_free_ids — ré-export hôte conservé).
_detect_free_ids = detect_free_ids


def fetch_free_models_sync(
    timeout: float = 10,
    *,
    proxy: str = "",
    urls_fn: Callable[[], list],
    log=logger,
) -> tuple:
    """Fetch union of discovery URLs via httpx (3 retries exp 1.5 on 5xx/timeout only, 429 respects Retry-After).

    Returns (free_ids: set[str], source: str, payloads: list[dict]).
    Fail-soft: raises only if ALL urls failed; caller logs warning.
    """
    urls = urls_fn()
    payloads: list[Any] = []
    source_parts = []
    last_err: Exception | None = None
    for url in urls:
        success = False
        for attempt in range(3):
            try:
                try:
                    import httpx as _httpx
                except ImportError:
                    # Fallback to curl subprocess (Windows may lack curl but try)
                    import json as _json

                    r = subprocess.run(
                        ["curl", "-s", "--max-time", str(int(timeout)), url],
                        capture_output=True,
                        text=True,
                        timeout=timeout + 5,
                        creationflags=_CREATE_NO_WINDOW,
                    )
                    if r.returncode != 0:
                        raise RuntimeError(f"curl failed: {r.stderr[:200]}") from None
                    data = _json.loads(r.stdout)
                    payloads.append(data)
                    source_parts.append(url)
                    success = True
                    last_err = None
                    break
                # httpx path
                _kwargs: dict[str, Any] = {"timeout": timeout}
                if proxy:
                    _kwargs["proxy"] = proxy
                with _httpx.Client(**_kwargs) as _client:
                    _resp = _client.get(url)
                if _resp.status_code == 429:
                    _ra = _resp.headers.get("Retry-After", "")
                    try:
                        _delay = int(str(_ra).strip())
                    except Exception:
                        _delay = 60
                    log.warning("[free-discovery] 429 from %s Retry-After=%s", url, _delay)
                    # Do not retry blindly on 429 — respect Retry-After
                    last_err = RuntimeError(f"429 Retry-After {_delay} from {url}")
                    break
                if 500 <= _resp.status_code < 600:
                    raise RuntimeError(f"5xx {_resp.status_code} from {url}")
                _resp.raise_for_status()
                data = _resp.json()
                payloads.append(data)
                source_parts.append(url)
                success = True
                last_err = None
                break
            except Exception as e:
                last_err = e
                msg = str(e)
                is_retryable = (
                    "5xx" in msg
                    or "timeout" in msg.lower()
                    or "timed out" in msg.lower()
                    or "connect" in msg.lower()
                    or "ConnectTimeout" in msg
                    or "ReadTimeout" in msg
                )
                if not is_retryable or attempt == 2:
                    if not success:
                        log.debug(
                            "[free-discovery] fetch failed %s attempt %d: %s", url, attempt + 1, e
                        )
                    break
                delay = (1.5**attempt) + random.uniform(-0.1, 0.1)
                # jitter ±10% already via random; clamp min 0
                if delay < 0:
                    delay = 0
                time.sleep(delay)
        # next url
    if not payloads:
        if last_err is not None:
            raise last_err
        return set(), "none", []
    free_ids = detect_free_ids(payloads)
    # HTML filet only if cascade found nothing
    if not free_ids:
        try:
            docs_url = "https://opencode.ai/docs/fr/zen/"
            try:
                import httpx as _httpx2

                _kwargs2: dict[str, Any] = {"timeout": timeout}
                if proxy:
                    _kwargs2["proxy"] = proxy
                with _httpx2.Client(**_kwargs2) as _c2:
                    _r2 = _c2.get(docs_url)
                    if _r2.status_code == 200:
                        _html = _r2.text
                        _ids = set(
                            re.findall(r"(?i)<td[^>]*>\s*([a-z0-9.\-]+-free)\s*</td>", _html)
                        )
                        if _ids:
                            free_ids = _ids
                            source_parts.append("docs:html")
            except ImportError:
                pass
        except Exception as e:
            log.debug("[free-discovery] html filet failed: %s", e)
    source = "|".join(source_parts) if source_parts else "none"
    return free_ids, source, payloads


def free_endpoint_for(free_id: str, api_base_free: str) -> str:
    """Return the correct free endpoint for a model.

    muse-* and spark-* models use the /v1/responses endpoint (Responses API),
    while other models use the standard /v1/chat/completions endpoint.
    """
    lid = free_id.lower()
    if "muse" in lid or "spark" in lid:
        return "https://opencode.ai/zen/v1/responses"
    return api_base_free


def apply_discovered_free_models(
    free_ids: set,
    source: str = "none",
    *,
    go_only_ids: set,
    free_models: set,
    free_model_map: dict,
    models: dict,
    discovery_state: dict,
    default_target: str,
    api_base_free: str,
    resolve_protocol_fn: Callable[[str], str],
    known_protocols: dict,
    cache_clear_fn: Callable[[], None],
    lock=None,
    log=logger,
) -> tuple[int, list | None]:
    """Apply discovered free_ids to MODELS/FREE_MODEL_MAP/FREE_MODEL_POOL.

    Delta-check: if set == free_models → no-op (0, no mtime bump).
    Otherwise mutates models (add missing free_ids with endpoint/protocol),
    free_models in-place, and free_model_map add-only
    (paid → paid-free homonyme). Thread-safe via lock if available.

    Retourne (added, sorted_pool) — l'hôte réassigne son global
    FREE_MODEL_POOL (rebind historique préservé) ; sorted_pool est None
    sur le chemin no-delta (l'hôte conserve alors l'ancien pool, comme
    l'historique — jamais de rebind sur no-op).
    """
    if not isinstance(free_ids, set):
        free_ids = set(free_ids)
    if go_only_ids:
        _go_only_hits = {f for f in free_ids if str(f).lower() in go_only_ids}
        if _go_only_hits:
            log.info(
                "[free-discovery] go-only ids excluded from anonymous pool: %s",
                ", ".join(sorted(_go_only_hits)),
            )
            free_ids -= _go_only_hits
    if free_ids == free_models:
        for _fid in sorted(free_ids):
            if "muse" in _fid.lower() or "spark" in _fid.lower():
                _exp = free_endpoint_for(_fid, api_base_free)
                _cur = models.get(_fid, {}).get("endpoint", "")
                if _cur and _cur != _exp:
                    models[_fid]["endpoint"] = _exp
                    log.info("[free-discovery] corrected endpoint %s → %s", _fid, _exp)
        log.debug(
            "[free-discovery] no delta (still %d free ids) source=%s", len(free_ids), source
        )
        discovery_state["detected"] = sorted(free_ids)
        discovery_state["source"] = source
        return 0, None
    removed = sorted(free_models - free_ids) if free_models else []
    if removed:
        log.info(
            "[free-discovery] upstream removed %s — keeping local, manual cleanup needed",
            ", ".join(removed),
        )
        discovery_state["removed"] = removed
    else:
        discovery_state["removed"] = []
    added = 0
    try:
        if lock is not None:
            lock.acquire()
        for fid in sorted(free_ids):
            expected = free_endpoint_for(fid, api_base_free)
            if fid not in models:
                proto = resolve_protocol_fn(fid)
                models[fid] = {"endpoint": expected, "protocol": proto}
                added += 1
                prefix = fid.split("-")[0].split(".")[0].lower()
                prefix_clean = re.sub(r"\d+$", "", prefix)
                if prefix_clean not in known_protocols:
                    log.warning(
                        "[free-discovery] unknown family %s for %s → openai", prefix_clean, fid
                    )
            else:
                cur = models[fid].get("endpoint", "")
                if cur != expected and ("muse" in fid.lower() or "spark" in fid.lower()):
                    models[fid]["endpoint"] = expected
                    log.info("[free-discovery] corrected endpoint %s → %s", fid, expected)
        # Update free_models in-place (keep object identity for importers that hold ref)
        free_models.clear()
        free_models.update(free_ids)
        new_pool = sorted(free_ids)
        # Keep state
        discovery_state["detected"] = sorted(free_ids)
        discovery_state["source"] = source
        # free_model_map add-only: paid → paid-free homonyme if exists
        # Iterate over a snapshot of MODELS keys (paid candidates = not free themselves)
        for paid in list(models.keys()):
            if paid in free_ids:
                continue
            homonyme = f"{paid}-free"
            if homonyme in free_ids and paid not in free_model_map:
                free_model_map[paid] = homonyme
                log.info("[free-discovery] mapped %s → %s (homonyme)", paid, homonyme)
        # default_target validation
        dt = default_target
        if dt and dt not in free_ids and free_ids:
            fallback = new_pool[0] if new_pool else dt
            log.warning(
                "[free-discovery] default_target %r not in FREE_MODELS — fallback %r", dt, fallback
            )
        log.info(
            "[free-discovery] fetched %d free ids, added %d new MODELS, source=%s",
            len(free_ids),
            added,
            source,
        )
        try:
            cache_clear_fn()
        except Exception:
            pass
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass
    return added, new_pool


def persist_free_mappings(
    *,
    auto_persist: bool,
    free_model_map: dict,
    models: dict,
    yaml_data: dict,
    save_yaml_fn: Callable[[], None],
    log=logger,
) -> None:
    """Merge add-only free mappings into config.yaml (atomic tmp+fsync+replace under lock)."""
    if not auto_persist:
        return
    try:
        # Ensure sections exist
        if "free_model_map" not in yaml_data or not isinstance(
            yaml_data.get("free_model_map"), dict
        ):
            yaml_data["free_model_map"] = {}
        if "models" not in yaml_data or not isinstance(yaml_data.get("models"), dict):
            yaml_data["models"] = {}
        # Merge FREE_MODEL_MAP add-only
        for k, v in free_model_map.items():
            if k not in yaml_data["free_model_map"]:
                yaml_data["free_model_map"][k] = v
        # Merge MODELS add-only (only free ids or newly discovered)
        for mid, cfg in models.items():
            if mid not in yaml_data["models"]:
                # Persist minimal protocol hint
                proto = cfg.get("protocol", "openai")
                yaml_data["models"][mid] = {"protocol": proto}
        save_yaml_fn()
        log.debug("[free-discovery] persisted %d free mappings", len(free_model_map))
    except Exception as e:
        log.warning("[free-discovery] persist failed: %s", e)


__all__ = [
    "_CREATE_NO_WINDOW",
    "_detect_free_ids",
    "_is_free_model",
    "apply_discovered_free_models",
    "detect_free_ids",
    "fetch_free_models_sync",
    "fetch_upstream_models",
    "free_discovery_urls",
    "free_endpoint_for",
    "is_free_model",
    "persist_free_mappings",
    "start_background_fetch",
]

