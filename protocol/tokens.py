"""protocol.tokens — estimation de tokens (Phase 9 refonte).

Déplacement PUR depuis ``opencode.py``. AUCUN import du projet :

* ``estimate_tokens`` : pur (longueur/3) ;
* ``estimate_input_tokens`` : ``encoding`` (tiktoken, possédé par l'hôte),
  ``extract_fn`` (hôte : ``_extract_text`` de ``protocol.mapping``),
  ``debug_fn`` / ``log_fn`` injectés ;
* ``elapsed_ms`` : pur (``time.monotonic``).
"""

from __future__ import annotations

import time
from collections.abc import Callable


def _noop(*args, **kwargs) -> None:
    return None


def estimate_tokens(text: str) -> int:
    """Fast token estimation — char-length only (P1.5).

    [P1.5 perf] Plus de branche tiktoken ≥200 chars : cette fonction n'est
    appelée QUE par les compteurs incrémentaux de stream (deltas, boucle
    d'émission) — un encode tiktoken par delta à fort débit coûte cher sur
    la boucle. Dérive chars//3 vs tiktoken acceptable : affichage stats
    dashboard uniquement (usage réel lu dans `usage` quand l'upstream le
    fournit). tiktoken CONSERVÉ pour estimate_input_tokens / count_tokens
    (offloadés to_thread)."""
    return max(1, len(text) // 3)


# Alias historique (opencode._estimate_tokens — importé par test_proxy.py).
_estimate_tokens = estimate_tokens


def _media_token_cost(block: dict) -> int:
    """[Lot L4 — A10] Coût en tokens d'un bloc média, estimé par sa TAILLE.

    Avant ce correctif, ``_extract_text`` réduisait une image ou un document à
    ``"[image:base64]"`` : ``count_tokens`` rendait donc le MÊME compte pour une
    vignette de 5 ko et un PDF de 3 Mo. La sous-estimation est structurelle et
    silencieuse — elle fausse la facturation estimée, et surtout la décision de
    compaction (on croit avoir la place alors qu'on est au-delà du contexte).

    On ne prétend pas reproduire exactement le tokenizer vision de chaque
    upstream (il n'est pas documenté et varie) : on applique une estimation
    **proportionnelle à la charge utile**, bornée, pour que la taille cesse
    d'être ignorée. Le facteur est celui des implémentations publiques :
    ~1 token par 750 octets d'image base64 après redimensionnement, un document
    PDF coûtant plutôt ~1 token par 400 octets (texte + mise en page).
    """
    btype = block.get("type", "")

    # ── Charge utile encodée (base64 ou data URI) ──
    #
    # mypy : un `X.get(k) if isinstance(X.get(k), dict) else {}` répété ne se
    # narrow pas (l'appel est réévalué à chaque occurrence). On matérialise la
    # valeur une fois, puis on la narrow — sinon `src`/`fobj`/`aobj` restent
    # typés `Any | dict | None` et chaque `.get` est une erreur `union-attr`.
    payload = ""
    _src_raw = block.get("source")
    _src: dict = _src_raw if isinstance(_src_raw, dict) else {}
    if btype == "image":
        payload = _src.get("data") or _src.get("url") or ""
    elif btype == "document":
        payload = _src.get("data") or _src.get("url") or ""
    elif btype == "file":
        _fobj_raw = block.get("file")
        _fobj: dict = _fobj_raw if isinstance(_fobj_raw, dict) else {}
        payload = _fobj.get("file_data") or _fobj.get("file_id") or ""
    elif btype == "input_image":
        payload = block.get("image_url") or ""
    elif btype == "input_file":
        payload = block.get("file_data") or block.get("file_url") or block.get("file_id") or ""
    elif btype == "input_audio":
        _aobj_raw = block.get("input_audio")
        _aobj: dict = _aobj_raw if isinstance(_aobj_raw, dict) else {}
        payload = _aobj.get("data") or ""
    else:
        return 0

    if not isinstance(payload, str) or not payload:
        return 0

    # Les data URI transportent l'en-tête (`data:image/png;base64,`) : il ne
    # compte pas comme charge utile facturée.
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]

    n_bytes = len(payload)
    if btype in ("document", "file", "input_file"):
        cost = n_bytes // 400
    elif btype == "input_audio":
        # ~1 token par 100 octets d'audio encodé (ordre de grandeur Whisper).
        cost = n_bytes // 100
    else:
        cost = n_bytes // 750

    # Bornes : un média minuscule coûte au moins quelques tokens (une image
    # n'est jamais gratuite), et un média énorme reste plafonné pour ne pas
    # saturer à lui seul tous les compteurs de l'application.
    return max(4, min(cost, 200_000))


def estimate_input_tokens(
    body: dict,
    *,
    encoding=None,
    extract_fn: Callable | None = None,
    debug_fn: Callable[..., None] = _noop,
    log_fn: Callable[..., None] = _noop,
) -> int:
    """Estimate input tokens from message content, tools, and tool_results."""
    # [audit 2026-10-30] ``compaction.truncate.split_keep_recent`` passes a
    # plain string (the text of one conversation pair), while every handler
    # injects this body-shaped estimator. The old code called ``.get`` on a
    # str, swallowed the AttributeError and returned 0 for every pair, so
    # ``keep_tokens`` kept the whole history instead of truncating it.
    if isinstance(body, str):
        try:
            if encoding:
                return len(encoding.encode(body))
            return max(1, len(body) // 3)
        except Exception:
            return max(1, len(body) // 3)
    try:
        chunks = []
        # [Lot L4 — A10] Tokens des médias (image/document/audio), accumulés à
        # part : ils ne passent pas par le texte (tiktoken) mais par une
        # estimation proportionnelle à la taille de la charge utile.
        media_cost = 0

        # [audit 2026-10-30] Responses met le prompt système dans
        # ``instructions`` (pas ``system``) : il était ignoré, donc un client
        # Responses sous-estimait tout son contexte et ne compactait jamais.
        instructions = body.get("instructions")
        if isinstance(instructions, str):
            chunks.append(instructions)

        # System prompt
        system = body.get("system", "")
        if isinstance(system, str):
            chunks.append(system)
        elif isinstance(system, list):
            for s in system:
                if isinstance(s, str):
                    chunks.append(s)
                elif isinstance(s, dict):
                    chunks.append(s.get("text", ""))

        # Tools definitions. Trois formes coexistent :
        #   Anthropic  {name, description, input_schema}
        #   Chat       {type:"function", function:{name, description, parameters}}
        #   Responses  {type:"function", name, description, parameters}
        # L'ancien code ne lisait que `name`/`description`/`input_schema` :
        # les schémas Chat/Responses (souvent 10-50 Ko) comptaient pour zéro et
        # le déclencheur de compaction ne voyait pas le vrai budget.
        for tool in body.get("tools", []) or []:
            if not isinstance(tool, dict):
                continue
            _fn_raw = tool.get("function")
            fn = _fn_raw if isinstance(_fn_raw, dict) else tool
            chunks.append(str(fn.get("name", "")))
            chunks.append(str(fn.get("description", "")))
            _schema = fn.get("input_schema")
            if _schema is None:
                _schema = fn.get("parameters", {})
            chunks.append(str(_schema))

        # Messages
        for msg in body.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, str):
                        chunks.append(block)
                    elif isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "tool_result":
                            chunks.append(extract_fn(block.get("content", "")) if extract_fn else "")
                            # [Lot L4 — A10] Les médias DANS un tool_result
                            # comptaient pour zéro (l'extracteur les réduit à un
                            # marqueur) : une capture d'écran renvoyée par un
                            # outil est un cas fréquent, pas un cas limite.
                            _tr_content = block.get("content", "")
                            if isinstance(_tr_content, list):
                                for _tb in _tr_content:
                                    if isinstance(_tb, dict):
                                        _m = _media_token_cost(_tb)
                                        if _m:
                                            media_cost += _m
                        elif btype == "thinking":
                            chunks.append(block.get("thinking", ""))
                        else:
                            chunks.append(block.get("text", ""))
                            chunks.append(str(block.get("input", "")))
                            # [Lot L4 — A10] Coût média proportionnel à la taille.
                            _m = _media_token_cost(block)
                            if _m:
                                media_cost += _m
            # Chat: les arguments des appels d'outils sont du contexte réel.
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                _tc_fn_raw = tc.get("function")
                _tc_fn = _tc_fn_raw if isinstance(_tc_fn_raw, dict) else {}
                chunks.append(str(_tc_fn.get("name", "")))
                chunks.append(str(_tc_fn.get("arguments", "")))

        # [audit 2026-10-30] Responses: l'historique vit dans ``input``, pas
        # ``messages``. Il n'était jamais estimé (0 token), donc la compaction
        # proactive ne pouvait pas se déclencher sur cette porte.
        for item in body.get("input", []) or []:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, dict):
                continue
            itype = str(item.get("type") or "")
            if itype in ("function_call", "function_call_output"):
                chunks.append(str(item.get("name", "")))
                chunks.append(str(item.get("arguments", "")))
                chunks.append(str(item.get("output", "")))
                continue
            if itype == "reasoning":
                _summary = item.get("summary")
                if isinstance(_summary, list):
                    for part in _summary:
                        if isinstance(part, dict):
                            chunks.append(str(part.get("text", "")))
                elif isinstance(_summary, str):
                    chunks.append(_summary)
            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, str):
                        chunks.append(block)
                    elif isinstance(block, dict):
                        chunks.append(str(block.get("text", "")))
                        chunks.append(str(block.get("refusal", "")))
                        _m = _media_token_cost(block)
                        if _m:
                            media_cost += _m

        combined = "\n".join(chunks)
        if encoding:
            return len(encoding.encode(combined)) + media_cost
        return max(1, len(combined) // 3) + media_cost
    except Exception as e:
        debug_fn(f"  ✗ token estimation failed: {type(e).__name__}: {e}")
        log_fn(f"  WARN: token estimation failed: {type(e).__name__}: {e}")
        return 0


# Alias historique (opencode._estimate_input_tokens — wrapper hôte conservé).
_estimate_input_tokens = estimate_input_tokens


def elapsed_ms(start_time: float) -> int:
    return int((time.monotonic() - start_time) * 1000)


# Alias historique (opencode._elapsed_ms — ~17 sites internes).
_elapsed_ms = elapsed_ms


__all__ = [
    "_elapsed_ms",
    "_estimate_input_tokens",
    "_estimate_tokens",
    "elapsed_ms",
    "estimate_input_tokens",
    "estimate_tokens",
]
