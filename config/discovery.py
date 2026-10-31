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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from typing import Any

logger = logging.getLogger("config.settings")

# Windows: masquer la fenêtre console des subprocess (évite le flash noir 1s)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _noop_log(*args, **kwargs) -> None:
    return None


def _normalize_ids(raw) -> set:
    """Normalise une liste d'ids de config (minuscules, strip, sans doublon).

    `isinstance(x, str)` et non `str(x)` : une entrée YAML `null` ou `42`
    deviendrait sinon le tombiveau "none"/"42", et un id réel collant à ces
    chaînes disparaîtrait du catalogue. Une clé absente/mal typée donne `set()`.

    Une chaîne nue n'est PAS une liste d'ids : l'itérer produirait un caractère
    par « id » (chacun devenant un tombiveau d'une lettre, susceptible de
    couvrir un id réel). Un scalaire non plus (`42` → TypeError). Les deux
    retombent donc sur `set()`, comme une clé absente.
    """
    if not raw or not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {x.strip().lower() for x in raw if isinstance(x, str) and x.strip()}


def models_endpoint_for(base: str) -> str:
    """Return the ``/models`` URL that actually serves ``base``'s leg.

    [audit 2026-10-30] The new inference bases (``/inference/openai/v1``,
    ``/inference/go/openai/v1``) only accept generation calls: their
    ``/models`` alias answers 404. The catalogue still lives on the legacy
    gateway paths (``/inference/v1/models`` and ``/zen/go/v1/models``), so
    map a configured base back to the real listing endpoint instead of
    blindly appending ``/models``.
    """
    text = str(base or "").strip()
    if not text:
        return ""
    for suffix in ("/chat/completions", "/messages", "/responses"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = text.rstrip("/")
    host, sep, path = text.partition("/inference/")
    if sep and path:
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "go":
            return host + "/zen/go/v1/models"
        return host + "/inference/v1/models"
    return text + "/models"


def fetch_upstream_models(
    models: dict,
    api_base_openai: str,
    api_base_anthropic: str,
    resolve_protocol_fn: Callable[[str], str],
    cache_clear_fn: Callable[[], None],
    timeout: float = 3.0,
    log=logger,
    purged_ids_fn: Callable[[], set] | None = None,
) -> int:
    """Fetch available models from upstream API and add them to MODELS.

    Timeout réduit à 3s (vs 10s avant) pour ne pas bloquer le démarrage.
    Appelé en arrière-plan, jamais bloquant pour le 1er chargement GUI.

    `purged_ids_fn` : résolution TARDIVE des tombeaux de purge, comme
    `cache_clear_fn` — `PURGED_IDS` n'existe pas encore quand l'hôte lance le
    thread (il est défini plus bas dans `config/settings.py`). C'est la 3e voie
    d'ajout automatique du catalogue (après `apply_discovered_free_models` et
    `persist_free_mappings`) : sans ce filtre, un id mort annoncé par l'upstream
    rentre dans `MODELS` 0,5 s après l'import et redevient routable — la purge
    manuelle de `models:` ne tenait alors que jusqu'au prochain démarrage.
    """
    try:
        purged = _normalize_ids(purged_ids_fn() if purged_ids_fn else ())
        if purged:
            # Process démarré avant la purge (ou id ré-annoncé) : sortie de
            # `MODELS`, sinon `get_model_config` résout encore l'id mort.
            for _mid in sorted(purged & set(models)):
                models.pop(_mid, None)
                log.info(
                    "[config] purged id %s dropped from MODELS (retiré à la main, "
                    "la découverte auto ne doit pas le ressusciter)",
                    _mid,
                )
        url = models_endpoint_for(api_base_openai)
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
                if model_id in purged:
                    log.info(
                        "[config] purged id %s kept out of MODELS (retiré à la main)",
                        model_id,
                    )
                    continue
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
    purged_ids_fn: Callable[[], set] | None = None,
) -> None:
    """Lancement non-bloquant en arrière-plan (daemon thread) — ne retarde pas l'import.

    `purged_ids_fn` est transmis tel quel à `fetch_upstream_models` (tombeaux de
    purge, résolution tardive — voir sa docstring) : sans lui, la 3e voie d'ajout
    automatique ré-annonce les ids morts à chaque démarrage.

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
                models,
                api_base_openai,
                api_base_anthropic,
                resolve_protocol_fn,
                cache_clear_fn,
                timeout=3.0,
                log=log,
                purged_ids_fn=purged_ids_fn,
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
        u = models_endpoint_for(base)
        if u and u not in urls:
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


def model_ids_from_payload(payload: Any) -> set[str]:
    """Ids d'un payload ``GET /models`` (OpenAI : ``{"data": [{"id": ...}]}``)."""
    ids: set[str] = set()
    if not isinstance(payload, dict):
        return ids
    data = payload.get("data")
    if not isinstance(data, list):
        return ids
    for row in data:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if isinstance(mid, str) and mid.strip():
            ids.add(mid.strip())
    return ids


def fetch_models_index_sync(
    urls_by_leg: dict[str, str],
    *,
    timeout: float = 12.0,
    proxy: str = "",
    log=logger,
) -> tuple[dict[str, set], str, dict[str, str]]:
    """Relève les ids *réellement annoncés* par chaque jambe opencode.

    Retourne ``(ids_par_jambe, source, erreurs_par_jambe)``. Une jambe en
    échec est absente du premier dict : l'appelant la conserve depuis son
    dernier relevé et marque l'instantané comme partiel (jamais de suppression
    sur un relevé incomplet).

    Pas de retry ici : la boucle live repasse à intervalle court. Le but est
    de refléter vite l'amont, pas de bloquer un cycle sur un 5xx transitoire.
    """
    out: dict[str, set] = {}
    errors: dict[str, str] = {}
    sources: list[str] = []
    if not urls_by_leg:
        return out, "none", errors
    for leg, url in urls_by_leg.items():
        if not isinstance(url, str) or not url.strip():
            errors[str(leg)] = "url absente"
            continue
        try:
            data = None
            try:
                import httpx as _httpx

                kwargs: dict[str, Any] = {"timeout": timeout}
                if proxy:
                    kwargs["proxy"] = proxy
                with _httpx.Client(**kwargs) as client:
                    resp = client.get(url, headers={"User-Agent": "opencode-proxy/live-models"})
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                data = resp.json()
            except ImportError:
                import urllib.request as _urlreq

                req = _urlreq.Request(url, headers={"User-Agent": "opencode-proxy/live-models"})
                with _urlreq.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    data = json.loads(resp.read().decode("utf-8"))
            ids = model_ids_from_payload(data)
            if not ids:
                raise RuntimeError("aucun id dans la réponse")
            out[str(leg)] = ids
            sources.append(url)
        except Exception as exc:
            errors[str(leg)] = f"{type(exc).__name__}: {exc}"
            log.debug("[live-models] fetch %s failed: %s", leg, exc)
    return out, "|".join(sources) if sources else "none", errors


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
                        log.debug("[free-discovery] fetch failed %s attempt %d: %s", url, attempt + 1, e)
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
                        _ids = set(re.findall(r"(?i)<td[^>]*>\s*([a-z0-9.\-]+-free)\s*</td>", _html))
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
    jev-* models use the /v1/systemone endpoint (TypeSafe SystemOne API —
    state + typed questions, NOT chat), other models use the standard
    /v1/chat/completions endpoint.
    """
    lid = free_id.lower()
    # [audit 2026-10-30] Derive from the configured free chat base instead of
    # hard-coding the deprecated /inference/v1 URLs. The official catalog now
    # exposes /inference/openai/v1 (chat/responses) and /inference/v1
    # (systemone legacy, only used when no explicit endpoint is configured).
    _base = str(api_base_free or "").rstrip("/")
    if _base.endswith("/chat/completions"):
        _base = _base[: -len("/chat/completions")]
    if "muse" in lid or "spark" in lid:
        return _base + "/responses" if _base else api_base_free
    if "jev" in lid:
        return _base + "/systemone" if _base else api_base_free
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
    purged_ids: set | frozenset = frozenset(),
    blocked_ids: set | frozenset = frozenset(),
) -> tuple[int, list | None]:
    """Apply discovered free_ids to MODELS/FREE_MODEL_MAP/FREE_MODEL_POOL.

    Delta-check: if set == free_models → no-op (0, no mtime bump).
    Otherwise mutates models (add missing free_ids with endpoint/protocol),
    free_models in-place, and free_model_map add-only
    (paid → paid-free homonyme). Thread-safe via lock if available.

    ``blocked_ids`` : ids retirés par le relevé vivant (dépréciés/absents).
    La découverte free est add-only et ne doit pas les ressusciter.

    Retourne (added, sorted_pool) — l'hôte réassigne son global
    FREE_MODEL_POOL (rebind historique préservé) ; sorted_pool est None
    sur le chemin no-delta (l'hôte conserve alors l'ancien pool, comme
    l'historique — jamais de rebind sur no-op).
    """
    if not isinstance(free_ids, set):
        free_ids = set(free_ids)
    purged = _normalize_ids(purged_ids)
    blocked = _normalize_ids(blocked_ids)
    if purged:
        _purged_hits = free_ids & purged
        if _purged_hits:
            log.info(
                "[free-discovery] purged ids kept out of the anonymous pool: %s",
                ", ".join(sorted(_purged_hits)),
            )
        free_ids -= purged
    if blocked:
        _blocked_hits = free_ids & blocked
        if _blocked_hits:
            log.info(
                "[free-discovery] live-retired ids kept out of the anonymous pool: %s",
                ", ".join(sorted(_blocked_hits)),
            )
        free_ids -= blocked
    if go_only_ids:
        _go_only_hits = {f for f in free_ids if str(f).lower() in go_only_ids}
        if _go_only_hits:
            log.info(
                "[free-discovery] go-only ids excluded from anonymous pool: %s",
                ", ".join(sorted(_go_only_hits)),
            )
            free_ids -= _go_only_hits
    if purged:
        # Un process démarré avant la purge a déjà ré-ajouté l'id en mémoire :
        # sans ce retrait, le persist add-only le réécrirait sur le disque.
        for _mid in sorted(purged & set(models)):
            models.pop(_mid, None)
            log.info(
                "[free-discovery] purged id %s dropped from models: (retiré à la main, "
                "la découverte auto ne doit pas le ressusciter)",
                _mid,
            )
        free_models -= purged
    if blocked:
        for _mid in sorted(blocked & set(models)):
            models.pop(_mid, None)
            log.info(
                "[free-discovery] live-retired id %s dropped from models (déprécié/absent du relevé vivant)",
                _mid,
            )
        free_models -= blocked
    if free_ids == free_models:
        for _fid in sorted(free_ids):
            if "muse" in _fid.lower() or "spark" in _fid.lower():
                _exp = free_endpoint_for(_fid, api_base_free)
                _cur = models.get(_fid, {}).get("endpoint", "")
                if _cur and _cur != _exp:
                    models[_fid]["endpoint"] = _exp
                    log.info("[free-discovery] corrected endpoint %s → %s", _fid, _exp)
        log.debug("[free-discovery] no delta (still %d free ids) source=%s", len(free_ids), source)
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
                    log.warning("[free-discovery] unknown family %s for %s → openai", prefix_clean, fid)
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
            log.warning("[free-discovery] default_target %r not in FREE_MODELS — fallback %r", dt, fallback)
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
    purged_ids: set | frozenset = frozenset(),
    blocked_ids: set | frozenset = frozenset(),
) -> None:
    """Merge add-only free mappings into config.yaml (atomic tmp+fsync+replace under lock)."""
    if not auto_persist:
        return
    purged = _normalize_ids(purged_ids)
    blocked = _normalize_ids(blocked_ids)
    try:
        # Ensure sections exist
        if "free_model_map" not in yaml_data or not isinstance(yaml_data.get("free_model_map"), dict):
            yaml_data["free_model_map"] = {}
        if "models" not in yaml_data or not isinstance(yaml_data.get("models"), dict):
            yaml_data["models"] = {}
        # Merge FREE_MODEL_MAP add-only
        for k, v in free_model_map.items():
            if k in blocked or v in blocked:
                continue
            if k not in yaml_data["free_model_map"]:
                yaml_data["free_model_map"][k] = v
        # Purge tombstonée : un id retiré à la main de `models:` ne doit jamais
        # revenir par le persist add-only (le process vivant peut encore le
        # porter en mémoire — voir apply_discovered_free_models).
        for _mid in sorted(purged & set(yaml_data["models"])):
            yaml_data["models"].pop(_mid, None)
            log.info(
                "[free-discovery] purged id %s dropped from config models: (retiré à la main)",
                _mid,
            )
        for _mid in sorted(blocked & set(yaml_data["models"])):
            yaml_data["models"].pop(_mid, None)
            log.info(
                "[free-discovery] live-retired id %s dropped from config models",
                _mid,
            )
        # Merge MODELS add-only (only free ids or newly discovered)
        for mid, cfg in models.items():
            if mid in purged or mid in blocked:
                continue
            if mid not in yaml_data["models"]:
                # Persist minimal protocol hint
                proto = cfg.get("protocol", "openai")
                yaml_data["models"][mid] = {"protocol": proto}
        save_yaml_fn()
        log.debug("[free-discovery] persisted %d free mappings", len(free_model_map))
    except Exception as e:
        log.warning("[free-discovery] persist failed: %s", e)


# ── [PLAN_STRATEGIE_FREE_MODELS Lot A] Sonde active de servabilité ─────────────
#
# Philosophie (plan 2026-10-16 §3) : la cascade de découverte *croit* le listing
# ``/models`` ; la sonde active le *vérifie* par une requête minimale réelle
# (cap tokens tiny, face publique) AVANT promotion : 200/429 → promu.
# Ce module ne porte QUE la mécanique pure (config, shaping, verdict, recheck
# plan, exécutant borné) — la GATE runtime (face officielle
# ``_official_free_headers``, egress PROXY, usage DB, état discovery) vit côté
# hôte (``opencode.py``) et est DI-ifiée via ``settings._FREE_PROBE_GATE``
# (pattern G4 : poussée par l'hôte, jamais importée par ce module).
#
# Verdicts : alive | limited | dead | not_free | unknown | not_probeable
#   * alive          2xx                                  → promu
#   * limited        429 (quota, modèle vivant)           → promu (+ marqué)
#   * dead           dead_status (404/410) OU marqueur body → refusé
#   * not_free       401/402 (refus Bearer public)        → refusé
#   * unknown        5xx / transport / 403 (geo-policy, peut dépendre de
#                    l'IP de sortie) / 400 sans marqueur  → PAS promu ce cycle,
#                    re-sonde au suivant (probe_recheck_plan)
#   * not_probeable  jev-* (SystemOne non-chat, v1)       → promu comme avant

_PROBE_DEFAULT_DEAD_MARKERS = (
    "not found",
    "unknown model",
    "invalid model",
    "no such model",
    "no longer",
    "retired",
    "sunset",
    "does not exist",
)


def probe_cfg(raw: Any) -> dict:
    """Parse + clamp d'un bloc ``free_discovery.probe`` (fail-soft).

    ``enabled`` défaut FALSE : la suite tests historique ne connaît pas la
    clef (les scripts qui n'ont pas de section probe gardent le comportement
    add-only simple) ; la prod dépend du config.yaml explicite. Valeur
    invalide → défaut, hors bornes → clamp (jamais d'écrasement silencieux).
    """
    raw = raw if isinstance(raw, dict) else {}

    def _num(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(raw.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        if v != v:  # NaN
            return float(default)
        return max(lo, min(hi, v))

    def _status_set(key: str, default: list) -> set:
        xs = raw.get(key, default)
        if not isinstance(xs, (list, tuple, set)):
            xs = default
        out: set = set()
        for x in xs:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                pass
        return out

    markers_raw = raw.get("dead_markers")
    if isinstance(markers_raw, (list, tuple)) and markers_raw:
        markers = tuple(str(m).lower() for m in markers_raw if isinstance(m, str) and m.strip())
    else:
        markers = _PROBE_DEFAULT_DEAD_MARKERS
    return {
        "enabled": bool(raw.get("enabled", False)),
        "timeout": _num("timeout", 8.0, 2.0, 60.0),
        "concurrency": int(_num("concurrency", 3.0, 1.0, 10.0)),
        "max_ids": int(_num("max_ids", 10.0, 1.0, 50.0)),
        "dead_ttl_s": _num("dead_ttl_s", 86400.0, 60.0, 604800.0),
        "recheck_interval_s": _num("recheck_interval_s", 1800.0, 60.0, 86400.0),
        "limited_recheck_s": _num("limited_recheck_s", 3600.0, 60.0, 86400.0),
        "transient_429_retry_s": _num("transient_429_retry_s", 300.0, 30.0, 3600.0),
        "skip_recent_ok_s": _num("skip_recent_ok_s", 1800.0, 0.0, 86400.0),
        "dead_status": _status_set("dead_status", [404, 410]),
        "not_free_status": _status_set("not_free_status", [401, 402]),
        "dead_markers": markers,
    }


def free_probe_shaping(mid: str, api_base_free: str) -> tuple:
    """(endpoint_url, body_minimal) pour UN id — (None, None) = non sondable.

    ``muse-*``/``spark-*`` → /responses (``input``/``max_output_tokens``),
    sinon /chat/completions (``messages``/``max_tokens``). ``jev-*`` :
    SystemOne est state+typed-questions (NON chat) — une sonde synthétique
    serait une fausse 400 permanente → non sondable v1 (promu comme avant).
    Cap frugal 64 tokens ; un upstream qui exige un min_tokens répond 400
    sans marqueur → verdict unknown (pas de promotion erronée pour autant).
    """
    lid = str(mid).lower()
    if "jev" in lid:
        return None, None
    url = free_endpoint_for(mid, api_base_free)
    if "/responses" in url:
        body = {"model": mid, "input": "ping", "max_output_tokens": 64, "stream": False}
    else:
        body = {
            "model": mid,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 64,
            "stream": False,
        }
    return url, body


def free_probe_verdict(status: Any, body: str, cfg: dict) -> tuple:
    """(verdict, raison) — PUR, aucune I/O. Erreur transport → ``unknown``.

    403 n'est JAMAIS mort par défaut : le fléchage 403 amont peut venir de la
    policy GEO/tenant de l'IP de sortie (les chemins free du proxy retentent
    déjà les 403-DataPolicy sur station fraîche) — verdict ``unknown`` plutôt
    qu'un faux ``dead`` irrévocable.
    """
    try:
        s = int(status)
    except (TypeError, ValueError):
        return "unknown", "transport/réseau — pas de statut"
    if 200 <= s < 300:
        return "alive", "2xx"
    if s in (cfg.get("dead_status") or ()):
        return "dead", f"status {s}"
    if s == 429:
        return "limited", "429 quota — modèle vivant"
    if s in (cfg.get("not_free_status") or ()):
        return "not_free", f"status {s} — refus Bearer public"
    if 500 <= s < 600:
        return "unknown", f"status {s} — transitoire upstream"
    low = (body or "").lower()
    for marker in cfg.get("dead_markers") or ():
        if marker in low:
            return "dead", f"marque corps {marker!r} (status {s})"
    return "unknown", f"status {s} — sans marque (param shape / incertitude)"


def probe_recheck_plan(results: dict, cfg: dict, now: float) -> list:
    """Ids à RE-sonder : ``unknown`` périmés (``recheck_interval_s``) et
    ``limited`` périmés (``limited_recheck_s``).

    Un 429 n'est pas une preuve définitive — quota fenêtré ou rate-limit
    transitoire —, donc un verdict ``limited`` doit expirer : sans TTL il
    restait affiché indéfiniment alors que le modèle pouvait déjà répondre.
    Les ``dead`` suivent le dead_ttl (hors plan), ``alive``/``not_probeable``
    sont finaux."""
    out = []
    for mid, r in (results or {}).items():
        if not isinstance(r, dict):
            continue
        try:
            ts = float(r.get("checked_epoch") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        verdict = r.get("verdict")
        if verdict == "unknown":
            try:
                not_before = float(r.get("not_before_epoch") or 0.0)
            except (TypeError, ValueError):
                not_before = 0.0
            if not_before:
                if now >= not_before:
                    out.append(mid)
            elif (now - ts) >= cfg["recheck_interval_s"]:
                out.append(mid)
        elif verdict == "limited":
            interval = float(cfg.get("limited_recheck_s") or cfg.get("recheck_interval_s") or 1800.0)
            if (now - ts) >= interval:
                out.append(mid)
    return sorted(out)


def _demote_global_429(results: dict, cfg: dict, control_row: dict | None = None) -> None:
    """Un 429 sur TOUS les ids testés est ambigu (quota global/transitoire).

    Si un modèle de contrôle connu répond 2xx, le 429 est ciblé et reste
    ``limited``. Sinon on le remplace par ``unknown`` avec un re-test court,
    au lieu de promouvoir un lot entier sur un rate-limit global.
    """
    rows = [r for r in (results or {}).values() if isinstance(r, dict) and r.get("status") is not None]
    if not rows or not all(int(r.get("status") or 0) == 429 for r in rows):
        return
    if isinstance(control_row, dict) and control_row.get("verdict") == "alive":
        for row in rows:
            if row.get("verdict") == "limited":
                row["reason"] = "429 ciblé (modèle de contrôle 2xx)"
        return
    retry_s = float(cfg.get("transient_429_retry_s") or 300.0)
    now = time.time()
    for row in rows:
        if row.get("verdict") == "limited":
            row["verdict"] = "unknown"
            row["reason"] = "429 global/transitoire (tous les ids testés) — re-test"
            row["not_before_epoch"] = now + retry_s


def probe_free_models(
    ids: list,
    *,
    api_base_free: str,
    headers_fn: Callable | None,
    dispatch_fn: Callable,
    cfg: dict,
    control_id: str | None = None,
    log=logger,
) -> dict:
    """Sonde SANS ÉTAT de ``ids`` → {mid: {verdict, status, reason, checked_at,
    checked_epoch}}. Borné : ``max_ids`` ids max (ordre de l'appelant),
    ``concurrency`` threads max (ThreadPoolExecutor borné). Une erreur de
    dispatch est un verdict ``unknown`` — jamais propagée. ``headers_fn`` est
    DI-hôte (face officielle) ; à défaut, headers publics minimaux. Un lot
    dont tous les ids répondent 429 est requalifié ``unknown`` (quota global
    ambigu) avec un re-test court, sauf si ``control_id`` (modèle free connu,
    hors lot) répond 2xx : le 429 est alors ciblé et reste ``limited``."""
    results: dict = {}
    if not ids:
        return results
    to_probe = list(ids)[: cfg["max_ids"]]

    def _one(mid: str) -> tuple:
        checked_at = _dt_now_utc_iso()
        checked_epoch = time.time()
        url, body = free_probe_shaping(mid, api_base_free)
        if url is None:
            return mid, {
                "verdict": "not_probeable",
                "status": None,
                "reason": "jev-* SystemOne (non-chat) — hors sonde v1",
                "checked_at": checked_at,
                "checked_epoch": checked_epoch,
            }
        try:
            payload = json.dumps(body).encode("utf-8")
        except Exception:
            payload = b"{}"
        if headers_fn is not None:
            headers = headers_fn(url)
        else:
            headers = {
                "Authorization": "Bearer public",
                "Content-Type": "application/json",
            }
        try:
            status, body_text = dispatch_fn(url, headers, payload)
        except Exception as e:
            return mid, {
                "verdict": "unknown",
                "status": None,
                "reason": f"transport {type(e).__name__}: {e}",
                "checked_at": checked_at,
                "checked_epoch": checked_epoch,
            }
        verdict, reason = free_probe_verdict(status, body_text, cfg)
        return mid, {
            "verdict": verdict,
            "status": int(status) if isinstance(status, int) else None,
            "reason": reason,
            "checked_at": checked_at,
            "checked_epoch": checked_epoch,
        }

    conc = max(1, int(cfg.get("concurrency") or 1))
    if conc <= 1 or len(to_probe) == 1:
        for mid in to_probe:
            m, r = _one(mid)
            results[m] = r
    else:
        with ThreadPoolExecutor(max_workers=min(conc, len(to_probe))) as ex:
            for m, r in ex.map(_one, to_probe):
                results[m] = r
    control_row = results.get(control_id) if control_id else None
    _statuses = [r.get("status") for r in results.values() if isinstance(r, dict) and r.get("status") is not None]
    _all_429 = bool(_statuses) and all(int(s) == 429 for s in _statuses)
    if _all_429 and control_id and control_row is None:
        _, control_row = _one(control_id)
    _demote_global_429(results, cfg, control_row)
    if log is not None:
        log.debug("[free-discovery] probe done: %s", results)
    return results


def _dt_now_utc_iso() -> str:
    from datetime import datetime as _dt

    return _dt.now(UTC).isoformat()


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
    "free_probe_shaping",
    "free_probe_verdict",
    "is_free_model",
    "models_endpoint_for",
    "persist_free_mappings",
    "probe_cfg",
    "probe_free_models",
    "probe_recheck_plan",
    "start_background_fetch",
]
