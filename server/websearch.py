"""server.websearch — recherche DDG + fetch URL (Phase 7 refonte).

Déplacement PUR depuis ``opencode.py`` (§ web search/fetch v3.3).
AUCUN import du projet :

* ``normalize_query`` / ``format_ddg`` : purs ;
* ``is_safe_fetch_url`` : garde SSRF (stdlib ``ipaddress``/``socket``) —
  ``blocked_nets`` injecté ;
* ``execute_ddg_search`` : cache LRU + sémaphore + lock par clé passés en
  paramètres (l'hôte possède ``_DDG_CACHE`` / ``_DDG_LOCKS`` / ``_DDG_SEM``),
  ``normalize_fn`` / ``format_fn`` / ``log_fn`` injectés ;
* ``execute_web_fetch`` : ``role_client_fn`` (hôte : ``_role_client`` —
  patché par ``test_role_clients.py``, lu À L'APPEL par le wrapper),
  ``safe_fn`` (hôte : ``_is_safe_fetch_url`` — idem), ``sem`` injectés ;
* ``strip_web_tool`` : ``normalize_fn`` / ``debug_fn`` injectés.

NON déplacés (décision) : ``_handle_web_search`` / ``_handle_web_fetch``
(orchestrateurs yaml + glue, lus via globaux hôtes patchés par
``test_proxy.py`` — Phase 9), ``_BLOCKED_NETS`` et tout l'état (hôte).
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
import socket
import time
import urllib.parse
from collections.abc import Callable

DDG_CACHE_MAX = 512
DDG_CACHE_TTL_S = 300
DDG_LOCKS_MAX = 512
FETCH_MAX_BYTES = 5_000_000


def _noop_log(*args, **kwargs) -> None:
    return None


def _noop_debug(*args, **kwargs) -> None:
    return None


def normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())[:500]


# Alias historique (opencode._normalize_query).
_normalize_query = normalize_query


def format_ddg(results: list, query: str) -> str:
    if not results:
        return f"No results found for: {query}"
    lines = [f"Web search results for '{query}':\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body_text = r.get("body", "")
        href = r.get("href", "")
        lines.append(f"{i}. **{title}**\n   {body_text}\n   {href}\n")
    return "\n".join(lines)


# Alias historique (opencode._format_ddg).
_format_ddg = format_ddg


async def is_safe_fetch_url(url: str, blocked_nets) -> bool:
    """SSRF guard F1+R1+R4: async, fail-closed, budgeted via outer wait_for."""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = (p.hostname or "").lower().rstrip(".")
        if not host or host in ("localhost", "localhost."):
            return False
        # IP literal
        try:
            ip = ipaddress.ip_address(host)
            if any([ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast]) or any(ip in n for n in blocked_nets):
                return False
            return True
        except ValueError:
            pass
        # DNS rebinding - to_thread, outer wait_for budgets it
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for _, _, _, _, sa in infos:
                ip = ipaddress.ip_address(sa[0])
                if any([ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast]) or any(ip in n for n in blocked_nets):
                    return False
        except Exception:
            return False
        return True
    except Exception:
        return False


# Alias historique (opencode._is_safe_fetch_url — wrapper hôte conservé,
# patché par test_proxy.py ; cet alias désigne le cœur pur).
_is_safe_fetch_url = is_safe_fetch_url


async def execute_ddg_search(
    query: str,
    max_results: int = 5,
    timeout: int = 10,
    proxy=None,
    *,
    cache,
    locks,
    sem,
    normalize_fn=normalize_query,
    format_fn=format_ddg,
    log_fn: Callable[..., None] = _noop_log,
) -> str:
    """Execute DDG with cache, semaphore, lock per key, wait_for budget unique."""
    # clamps Q8A
    try:
        timeout = max(5, min(30, int(timeout)))
    except Exception:
        timeout = 10
    try:
        max_results = max(1, min(10, int(max_results)))
    except Exception:
        max_results = 5
    qnorm = normalize_fn(query)
    kstr = f"{qnorm}:{max_results}"
    now = time.monotonic()
    # LRU hit
    if kstr in cache:
        exp, val = cache[kstr]
        if now < exp:
            cache.move_to_end(kstr)
            return copy.deepcopy(val)
        else:
            try:
                del cache[kstr]
            except KeyError:
                pass
    # lock per key (v3.3 R3: pop after lock released, finally)
    lock = locks.get(kstr)
    if lock is None:
        lock = asyncio.Lock()
        locks[kstr] = lock
        _hit_val = None
    _hit = False
    try:
        async with lock:
            # double-check
            if kstr in cache:
                exp2, val2 = cache[kstr]
                if time.monotonic() < exp2:
                    cache.move_to_end(kstr)
                    _hit_val = copy.deepcopy(val2)
                    _hit = True
                else:
                    _hit = False
            else:
                _hit = False
            if not _hit:
                # semaphore + wait_for
                async with sem:

                    def _sync_ddg():
                        try:
                            from duckduckgo_search import DDGS

                            try:
                                ddgs = DDGS(timeout=timeout, proxy=proxy)
                            except TypeError:
                                ddgs = DDGS()
                            with ddgs:
                                return list(ddgs.text(qnorm, max_results=max_results))
                        except ImportError as e:
                            raise ImportError(f"duckduckgo-search not installed: {e}") from e

                    try:
                        results = await asyncio.wait_for(asyncio.to_thread(_sync_ddg), timeout + 2)
                    except TimeoutError:
                        log_fn(f"  WEB SEARCH: DDG timeout {timeout}s query='{qnorm[:60]}' queue={3 - sem._value}")
                        raise
                    except ImportError:
                        raise
                    except Exception as e:
                        # on_error strip - raise to let handler strip
                        raise RuntimeError(f"DDG error: {e}") from e
                formatted = format_fn(results, qnorm)
                cache[kstr] = (now + DDG_CACHE_TTL_S, formatted)
                if len(cache) > DDG_CACHE_MAX:
                    cache.popitem(last=False)
                _hit_val = copy.deepcopy(formatted)
    finally:
        # outside lock: evict lock conditionnel (R3 sans race) - always
        try:
            if not lock.locked() and not getattr(lock, "_waiters", None):
                locks.pop(kstr, None)
            if len(locks) > DDG_LOCKS_MAX:
                oldest = next(iter(locks))
                locks.pop(oldest, None)
        except Exception:
            try:
                locks.pop(kstr, None)
            except Exception:
                pass
    # Invariant : tous les chemins de succès assignent _hit_val (les échecs
    # propagent) — l'init `None` ne couvre que la branche nouveau-lock.
    assert _hit_val is not None
    return _hit_val


# Alias historique (opencode._execute_ddg_search — wrapper hôte conservé).
_execute_ddg_search = execute_ddg_search


async def execute_web_fetch(
    url: str,
    prompt: str = "",
    timeout: int = 15,
    max_bytes: int = 12000,
    via_vpn: bool = False,
    *,
    role_client_fn,
    safe_fn,
    sem,
) -> str:
    """Fetch URL with SSRF guard, redirect re-validation, content guards, sem."""
    # clamps Q8A
    try:
        timeout = max(5, min(30, int(timeout)))
    except Exception:
        timeout = 15
    try:
        max_bytes = max(2000, min(50000, int(max_bytes)))
    except Exception:
        max_bytes = 12000
    # SSRF initial - budgeted via outer wait_for, no inner wait_for
    if not await safe_fn(url):
        raise ValueError(f"SSRF rejected: {url}")
    # [plan-perf Lot 1] Client partagé par rôle : plus de handshake TLS /
    # pool par fetch. follow_redirects + timeout restent PAR REQUÊTE
    # (httpx 0.28), boucle de redirection + re-validation SSRF INCHANGÉES.
    # Rôle tunnel = URL SOCKS du pool/station active (cf. _role_tunnel_url),
    # comme les probes déjà migrées — jamais de client jetable ici.
    _role = "tunnel" if via_vpn else "direct"
    c = role_client_fn(_role)
    async with sem:
        r = await c.get(url, headers={"User-Agent": "opencode-proxy/1.0"}, follow_redirects=False, timeout=timeout)
        for _ in range(3):
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location", "")
                nxt = urllib.parse.urljoin(url, loc)
                if not loc or not await safe_fn(nxt):
                    raise ValueError(f"SSRF redirect rejected: {loc}")
                url = nxt
                r = await c.get(url, headers={"User-Agent": "opencode-proxy/1.0"}, follow_redirects=False, timeout=timeout)
            else:
                break
        # R4 guards
        ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ct and not (ct.startswith("text/") or "json" in ct or "xml" in ct):
            raise ValueError(f"Rejected Content-Type: {ct}")
        if int(r.headers.get("content-length", "0") or 0) > FETCH_MAX_BYTES or len(r.content) > FETCH_MAX_BYTES:
            raise ValueError("Content too large")
        r.raise_for_status()
        html = r.text[: max_bytes * 3]
    # extraction to_thread
    try:
        import trafilatura

        extracted = await asyncio.to_thread(trafilatura.extract, html) or ""
    except ImportError:
        extracted = ""
    if not extracted:
        try:
            from bs4 import BeautifulSoup

            extracted = await asyncio.to_thread(lambda: BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True))
        except ImportError:
            extracted = re.sub(r"<[^>]+>", " ", html)
    extracted = extracted[:max_bytes].strip()
    return f"Content of {url} (extracted {len(extracted)} chars):\n{extracted}"


# Alias historique (opencode._execute_web_fetch — wrapper hôte conservé,
# patché par test_proxy.py / test_role_clients.py).
_execute_web_fetch = execute_web_fetch


def strip_web_tool(body: dict, protocol: str, name: str, *, normalize_fn, debug_fn=_noop_debug):
    """Remove web_* tool and forced tool_choice."""
    if "tools" in body and isinstance(body["tools"], list):
        body["tools"] = [t for t in body["tools"] if normalize_fn(t) != name]
        if not body["tools"]:
            try:
                del body["tools"]
            except KeyError:
                pass
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        # check if tc references the tool being stripped
        tc_name = tc.get("name", "") or tc.get("function", {}).get("name", "")
        # also check type containing web_*
        tc_type = tc.get("type", "")
        is_target = False
        if normalize_fn({"name": tc_name}) == name:
            is_target = True
        elif isinstance(tc_type, str) and name in tc_type:
            is_target = True
        # empty orphan -> auto
        if not isinstance(tc_name, str) or not tc_name.strip():
            if tc_type in ("tool", "function") and not tc_name.strip():
                debug_fn("  [convert] _strip_web_tool: empty tool_choice name → auto")
                body["tool_choice"] = "auto"
                return
        if is_target and tc.get("type") in ("tool", "function"):
            try:
                del body["tool_choice"]
            except KeyError:
                pass
        # also strip type web_* without name
        if isinstance(tc_type, str) and tc_type.startswith(name):
            try:
                del body["tool_choice"]
            except KeyError:
                pass


# Alias historique (opencode._strip_web_tool — appelé par ~10 sites handlers).
_strip_web_tool = strip_web_tool


__all__ = [
    "DDG_CACHE_MAX",
    "DDG_CACHE_TTL_S",
    "DDG_LOCKS_MAX",
    "FETCH_MAX_BYTES",
    "execute_ddg_search",
    "execute_web_fetch",
    "format_ddg",
    "is_safe_fetch_url",
    "normalize_query",
    "strip_web_tool",
    "_execute_ddg_search",
    "_execute_web_fetch",
    "_format_ddg",
    "_is_safe_fetch_url",
    "_normalize_query",
    "_strip_web_tool",
]
