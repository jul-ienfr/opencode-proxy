"""app.compaction.summarizer — appel résumeur interne (Phase 2, réplique officielle).

Réplique EXACTE de la requête qu'OpenCode enverrait lui-même pour résumer :
- 1 seul message user texte (conversation sérialisée) ;
- PAS de tools (clé absente, pas même []) ;
- PAS de system (clé absente) ;
- stream:false, même modèle/route que la requête d'origine (sauf override
  explicite) ;
- max_tokens = min(summary_max_tokens, 4096) — cap client officiel ;
- PAS de bloc thinking/effort, PAS de web_search/web_fetch ;
- timeout dédié, max_attempts:1 (jamais de récursion) ;
- jamais mis en cache (ni lecture ni écriture).

Tout échec → None : l'appelant relaie l'erreur overflow intacte (fail-open).
Ne lève jamais.
"""

from __future__ import annotations

import asyncio
import json as _json

_SUMMARY_CAP = 4096  # cap client officiel : min(outputLimit, 4096)


# Rubriques exigées du résumé (aligné officiel : template structuré imposé,
# réponse invalide → 1 corrective, cf. has_required_outline/REWRITE_PROMPT).
_REQUIRED_HEADINGS = (
    "objective",
    "decisions",
    "completed work",
    "active work",
    "next steps",
    "relevant files",
)

_STRUCTURE_INSTRUCTIONS = (
    "Write a structured summary to allow continuing the work in a fresh context "
    "window where the raw history above will be replaced with this summary. "
    "Use exactly these sections (omit none, keep each concise):\n"
    "## Objective\n## Decisions\n## Completed work\n"
    "## Active work and next steps\n## Relevant files and context"
)


def has_required_outline(summary_text) -> bool:
    """True si le résumé contient ≥1 rubrique exigée (`#` optionnel, casse libre).

    Validation réplique officielle : un résumé sans aucune rubrique est
    rejeté (1 corrective, puis échec franc). Ne lève jamais.
    """
    try:
        if not isinstance(summary_text, str) or not summary_text.strip():
            return False
        low = summary_text.lower()
        return any(h in low for h in _REQUIRED_HEADINGS)
    except Exception:
        return False


def build_rewrite_prompt(bad_summary: str) -> str:
    """Prompt correctif unique quand le résumé ne respecte pas les rubriques."""
    try:
        attempt = bad_summary if isinstance(bad_summary, str) else ""
    except Exception:
        attempt = ""
    return (
        "The previous response did not use the required sections. Rewrite it "
        "using exactly these sections (omit none, keep each concise):\n"
        "## Objective\n## Decisions\n## Completed work\n"
        "## Active work and next steps\n## Relevant files and context\n\n"
        "Previous attempt:\n<attempt>\n" + attempt[:8000] + "\n</attempt>"
    )


def build_summary_user_text(history_text: str, previous_summary: str | None = None) -> str:
    """Corps du message user résumeur (template compaction : chaînage previousSummary)."""
    prev = (previous_summary or "").strip()
    hist = (history_text or "").strip()
    if prev:
        return (
            "Previous summary:\n<previous-summary>\n" + prev + "\n</previous-summary>\n\n"
            "Conversation to summarize:\n<conversation>\n" + hist + "\n</conversation>\n\n"
            + _STRUCTURE_INSTRUCTIONS
            + " Continue from the previous summary: preserve still-true details, "
              "remove stale ones, merge in the new facts."
        )
    return (
        "Summarize the following conversation concisely.\n\n"
        "<conversation>\n" + hist + "\n</conversation>\n\n"
        + _STRUCTURE_INSTRUCTIONS
    )


def _norm_cap(cap) -> int:
    """Cap assaini (défaut officiel _SUMMARY_CAP). Ne lève jamais."""
    try:
        c = int(cap)
        return c if c > 0 else _SUMMARY_CAP
    except Exception:
        return _SUMMARY_CAP


def _int_field(body, key):
    """Entier positif d'un champ (hors bool), ou 0. Ne lève jamais."""
    try:
        v = body.get(key) if isinstance(body, dict) else None
        if isinstance(v, bool):
            return 0
        return int(v) if isinstance(v, int) and v > 0 else 0
    except Exception:
        return 0


def should_clamp_summary(body, cap=None) -> bool:
    """True si body porte un max_tokens/max_output_tokens au-delà du cap.

    Pré-filtre bon marché (comparaison d'entiers, sans scan) avant la
    détection de shape : les requêtes ordinaires (cap respecté) ne paient
    jamais le coût de ``is_compaction_shape``. Ne lève jamais.
    """
    try:
        c = _norm_cap(cap)
        return _int_field(body, "max_tokens") > c or _int_field(body, "max_output_tokens") > c
    except Exception:
        return False


def cap_summary_max_tokens(body, cap=None) -> tuple:
    """Plafonne max_tokens/max_output_tokens au cap officiel (4096).

    Réplique officielle : l'officiel n'envoie jamais plus de
    ``min(outputLimit, 4096)`` pour un résumé — borne les générations
    runaway (13k tokens → 2 min) qui font thrasher les clients en attente.
    Jamais de remontée, jamais d'ajout de clé, copie (l'original reste
    intact pour logs/DB). Retourne (body, capped: bool). Ne lève jamais.
    """
    try:
        c = _norm_cap(cap)
        if not isinstance(body, dict):
            return body, False
        changed = False
        out = body
        for k in ("max_tokens", "max_output_tokens"):
            if _int_field(out, k) > c:
                if out is body:
                    out = dict(body)
                out[k] = c
                changed = True
        return out, changed
    except Exception:
        try:
            return body, False
        except Exception:
            return {}, False


def build_summarizer_body(summary_text_input: str, max_tokens: int) -> dict:
    """Body de la requête résumeur (jamais envoyé tel quel : model remappé par l'appelant)."""
    cap = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else 2048
    cap = min(cap, _SUMMARY_CAP)
    return {
        "messages": [{"role": "user", "content": summary_text_input}],
        "max_tokens": cap,
        "stream": False,
    }


def build_summarizer_anthropic_body(summary_text_input: str, max_tokens: int, model_id: str = "") -> dict:
    """Body résumeur natif Anthropic (porte /v1/messages, protocol anthropic).

    Même contenu que la forme chat (1 message user, pas de tools/system,
    ``stream:false``) mais sans champ Chat-only : pas de ``tool_choice``,
    ``reasoning_effort`` ni ``response_format``. Le routage free/payant,
    le tunnel geo et l'auth restent décidés par le plan/transport.
    """
    cap = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else 2048
    cap = min(cap, _SUMMARY_CAP)
    body: dict = {
        "model": model_id or "",
        "messages": [{"role": "user", "content": summary_text_input}],
        "max_tokens": cap,
        "stream": False,
    }
    if not body["model"]:
        body.pop("model", None)
    return body


def build_summarizer_responses_body(summary_text_input: str, max_tokens: int, model_id: str = "") -> dict:
    """Body résumeur natif Responses (porte /v1/responses, api responses).

    Forme ``input`` (jamais ``messages``) : 1 item message user texte,
    ``store:false`` (ZDR-friendly), ``stream:false``, pas de ``tools`` ni
    d'``instructions`` — le provider gère le pairing et rend un objet
    ``output[]`` (item ``compaction`` opaque relayé tel quel par le transport).
    """
    cap = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else 2048
    cap = min(cap, _SUMMARY_CAP)
    body: dict = {
        "model": model_id or "",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": summary_text_input}],
            }
        ],
        "max_output_tokens": cap,
        "stream": False,
        "store": False,
    }
    if not body["model"]:
        body.pop("model", None)
    return body


def build_summarizer_for_api(summary_text_input: str, max_tokens: int, model_id: str = "", api: str = "chat") -> dict:
    """Construit le body résumeur dans la forme de l'API de destination.

    - ``chat`` (défaut) → ``build_summarizer_body`` (``messages``) ;
    - ``anthropic`` → ``build_summarizer_anthropic_body`` ;
    - ``responses`` → ``build_summarizer_responses_body``.
    Ne lève jamais : repli chat sur ``api`` inconnue.
    """
    try:
        low = str(api or "chat").lower()
        if low == "responses":
            return build_summarizer_responses_body(summary_text_input, max_tokens, model_id)
        if low == "anthropic":
            return build_summarizer_anthropic_body(summary_text_input, max_tokens, model_id)
        return build_summarizer_body(summary_text_input, max_tokens)
    except Exception:
        return build_summarizer_body(summary_text_input, max_tokens)


_MEDIA_PLACEHOLDER = "[Attached {kind}: {name}]"


def _media_descriptor(block) -> str | None:
    """Descripteur texte d'un bloc média (image/fichier), ou None si non-média.

    Aligné officiel (stripMedia) : les octets embarqués ne partent jamais
    vers le résumeur — un descripteur suffit à situer la pièce.
    Ne lève jamais.
    """
    try:
        if not isinstance(block, dict):
            return None
        t = str(block.get("type", "")).lower()
        if t in ("image", "image_url"):
            src = block.get("source", {}) if isinstance(block.get("source"), dict) else {}
            st = str(src.get("type", "")).lower()
            if st == "url" or block.get("image_url"):
                url = src.get("url", "") or block.get("image_url", "")
                return _MEDIA_PLACEHOLDER.format(kind="image", name=str(url)[:120] or "url")
            if st == "base64":
                return _MEDIA_PLACEHOLDER.format(kind="image", name="embedded data omitted")
            return _MEDIA_PLACEHOLDER.format(kind="image", name="omitted")
        if t in ("file", "document", "input_file"):
            name = block.get("filename", "") or block.get("name", "") or "file"
            return _MEDIA_PLACEHOLDER.format(kind="file", name=str(name)[:120])
        return None
    except Exception:
        return None


def _serialize_history_for_summary(messages) -> str:
    """Sérialise messages → texte (role: contenu), jamais d'exception."""
    try:
        if not isinstance(messages, list):
            return ""
        # Allègement façon officielle AVANT sérialisation : sorties d'outils
        # rognées (même règle que summary-lean, 2000 chars) — jamais l'original.
        # L'historique est une liste : on l'enveloppe pour lean (messages/input).
        try:
            from app.compaction.lean import lean_summary_body as _lean_hist

            _looks_responses = any(
                isinstance(_m, dict) and "type" in _m for _m in (messages or [])
            )
            _wrap_key = "input" if _looks_responses else "messages"
            _wrapped, _ = _lean_hist({_wrap_key: messages}, max_chars=2000)
            if isinstance(_wrapped, dict) and isinstance(_wrapped.get(_wrap_key), list):
                messages = _wrapped[_wrap_key]
        except Exception:
            pass
        parts = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, list):
                chunks = []
                for b in content:
                    if not isinstance(b, dict):
                        chunks.append(str(b))
                        continue
                    desc = _media_descriptor(b)
                    if desc is not None:
                        chunks.append(desc)
                        continue
                    chunks.append(str(b.get("text", b.get("content", ""))))
                txt = " ".join(chunks)
            else:
                txt = "" if content is None else str(content)
            parts.append(f"{role}: {txt[:4000]}")
        return "\n".join(parts)[:120000]
    except Exception:
        return ""


def extract_previous_summary(messages) -> str | None:
    """Extrait le résumé d'un checkpoint précédent (<summary>…), ou None."""
    try:
        if not isinstance(messages, list):
            return None
        for m in messages:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            text = c if isinstance(c, str) else ""
            if isinstance(c, list):
                text = " ".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
            if "<summary>" in text and "</summary>" in text:
                s = text.split("<summary>", 1)[1].split("</summary>", 1)[0].strip()
                if s:
                    return s[:8000]
        return None
    except Exception:
        return None


async def run_summarizer(
    history_messages,
    *,
    model_id: str,
    endpoint: str,
    protocol: str,
    auth_headers_fn,
    do_request_fn,
    summary_max_tokens: int = 2048,
    timeout_s: int = 60,
    api: str = "chat",
) -> str | None:
    """Exécute l'appel résumeur interne. Retourne le texte du résumé, ou None.

    - model_id/endpoint/protocol/api : même voie que la requête d'origine
      (ou override explicite résolu par l'appelant) ; ``api`` vaut
      ``chat`` | ``anthropic`` | ``responses`` et choisit la forme du body
      (``build_summarizer_for_api``) — parité avec l'endpoint (``/responses``
      attend ``input``, jamais ``messages``) ;
    - auth_headers_fn(protocol) → headers ; do_request_fn(endpoint, body,
      headers, protocol) → (resp, headers) ;
    - timeout dédié asyncio (timeout_s), 1 seule tentative ;
    - jamais de cache, jamais de thinking/effort, jamais de tools/system.
    """
    try:
        hist_text = _serialize_history_for_summary(history_messages)
        if not hist_text.strip():
            return None
        user_text = build_summary_user_text(hist_text, extract_previous_summary(history_messages))
        try:
            _api = api
            if not _api and "/responses" in str(endpoint or ""):
                _api = "responses"
        except Exception:
            _api = api
        body = build_summarizer_for_api(user_text, summary_max_tokens, model_id, _api or "chat")
        # build_summarizer_for_api("chat") ne pose pas model : parité historique.
        try:
            body["model"] = model_id
        except Exception:
            pass
        try:
            headers = auth_headers_fn(protocol)
        except Exception:
            return None

        async def _once():
            return await do_request_fn(endpoint, body, headers, protocol)

        try:
            resp, _ = await asyncio.wait_for(_once(), timeout=timeout_s)
        except Exception:
            return None
        try:
            status = resp.status_code
        except Exception:
            return None
        if status != 200:
            return None
        try:
            raw = resp.content
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8", errors="replace")
            data = _json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            return None
        summary = _extract_text(data)
        if summary and has_required_outline(summary):
            return summary
        # Aligné officiel : 1 SEULE corrective quand les rubriques manquent
        # (résumé vide/hors-format), puis échec franc (None → relais intact).
        if not summary:
            return None
        try:
            rewrite_body = build_summarizer_for_api(
                build_rewrite_prompt(summary), summary_max_tokens, model_id, _api or "chat"
            )
            try:
                rewrite_body["model"] = model_id
            except Exception:
                pass

            async def _once_more():
                return await do_request_fn(endpoint, rewrite_body, headers, protocol)

            try:
                resp2, _ = await asyncio.wait_for(_once_more(), timeout=timeout_s)
            except Exception:
                return None
            try:
                if getattr(resp2, "status_code", 0) != 200:
                    return None
                raw2 = resp2.content
                if isinstance(raw2, (bytes, bytearray)):
                    raw2 = bytes(raw2).decode("utf-8", errors="replace")
                data2 = _json.loads(raw2) if isinstance(raw2, str) else {}
            except Exception:
                return None
            summary2 = _extract_text(data2)
            if summary2 and has_required_outline(summary2):
                return summary2
            return None
        except Exception:
            return None
    except Exception:
        return None


def _extract_text(data) -> str | None:
    """Extrait le texte résumé (formats Anthropic + OpenAI Chat + Responses), ou None."""
    try:
        if not isinstance(data, dict):
            return None
        content = data.get("content")
        if isinstance(content, list):
            txt = "".join(
                str(b.get("text", "")) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if txt:
                return txt
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            txt = str(msg.get("content", "") or "").strip()
            if txt:
                return txt
        # Responses natif : {"output": [{"type": "message", "content": [{"type": "output_text", "text": ...}]}]}
        # L'item "compaction" opaque n'est JAMAIS interprété (non humain, chiffré
        # provider) : on n'en extrait rien, on ne le confond pas avec un résumé.
        output = data.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type", "")).lower() in ("compaction", "compaction_trigger"):
                    continue
                icontent = item.get("content")
                if isinstance(icontent, list):
                    for b in icontent:
                        if not isinstance(b, dict):
                            continue
                        if str(b.get("type", "")) in ("output_text", "text", "input_text"):
                            t = b.get("text", "")
                            if isinstance(t, str) and t.strip():
                                parts.append(t)
                        elif isinstance(b.get("text"), str) and b.get("text", "").strip():
                            parts.append(b["text"])
            txt = "".join(parts).strip()
            if txt:
                return txt
        return None
    except Exception:
        return None


__all__ = [
    "build_summary_user_text",
    "build_summarizer_body",
    "build_summarizer_anthropic_body",
    "build_summarizer_responses_body",
    "build_summarizer_for_api",
    "should_clamp_summary",
    "cap_summary_max_tokens",
    "extract_previous_summary",
    "run_summarizer",
]
