"""app.compaction.summarizer — appel résumeur interne (Phase 2, réplique officielle).

Réplique EXACTE de la requête qu'OpenCode enverrait lui-même pour résumer :
- 1 seul message user texte (conversation sérialisée) ;
- PAS de tools (clé absente, pas même []) ;
- PAS de system (clé absente) ;
- stream:false, même modèle/route que la requête d'origine (sauf override
  explicite) ;
- max_tokens = min(summary_max_tokens, 32000) — cap client officiel
  (SUMMARY_OUTPUT_MAX) ;
- PAS de bloc thinking/effort, PAS de web_search/web_fetch ;
- timeout dédié, max_attempts:1 (jamais de récursion) ;
- jamais mis en cache (ni lecture ni écriture).

Tout échec → None : l'appelant relaie l'erreur overflow intacte (fail-open).
Ne lève jamais.
"""

from __future__ import annotations

import asyncio
import json as _json

_SUMMARY_CAP = 32000  # plafond officiel SUMMARY_OUTPUT_MAX (model-request.ts) :
# un résumé n'a jamais besoin de plus ; une demande au-delà ne peut pas être
# réduite pour tenir dans une fenêtre que le catalogue surestime.
_OUTPUT_TOKEN_FALLBACK = 32000  # catalogue sans limite de sortie (officiel)
_OUTPUT_TOKEN_MAX = 256000  # aucune réponse n'a besoin de plus (officiel)
_OUTPUT_TOKEN_MIN = 1024  # plancher officiel


# Gabarit officiel du résumé (compaction.ts:SUMMARY_TEMPLATE, verbatim) :
# rubriques exigées, réponse invalide → 1 corrective (NUDGE), cf.
# has_required_outline (sémantique officielle filled() : ligne exacte).
_SUMMARY_TEMPLATE = (
    "You MUST use this format for your response (you may omit sections that aren't "
    "applicable). Do not include the <template> tags in your response.\n"
    "<template>\n"
    "## Objective\n"
    "- [one or two brief sentences describing what the user is trying to accomplish]\n"
    "\n"
    "## Requirements\n"
    "- [constraints, preferences, requirements, and scope boundaries stated by the user, "
    'or "(none)"]\n'
    "\n"
    "## Decisions\n"
    '- [decisions already made and why, or "(none)"]\n'
    "\n"
    "## Work State\n"
    "Break the objective into smaller goals and report which are completed, which are "
    "being worked on, and which are blocked.\n"
    "### Completed\n"
    '- [goals that have been completed; otherwise "(none)"]\n'
    "\n"
    "### Active\n"
    '- [goals currently being worked on; otherwise "(none)"]\n'
    "\n"
    "### Blocked\n"
    '- [anything blocking progress, and why; otherwise "(none)"]\n'
    "\n"
    "## Next Move\n"
    '- [ordered list of next actions, or "(none)"]\n'
    "\n"
    "## Relevant Files\n"
    "List the files and directories, other than the current working directory, that "
    "another agent would need to open to continue this work. Include at most 15, most "
    "important first. Do not list every file that was read or changed. Include paths "
    'outside the current working directory when relevant. If none, write "(none)".\n'
    "- `[file or directory path]`: [brief reason it matters]\n"
    "\n"
    "## Important Context\n"
    "- [facts the next agent cannot continue without and cannot easily find on its own; "
    'or "(none)"]\n'
    "</template>"
)

_SUMMARY_RULES = (
    "Rules:\n"
    "- Keep each section concise. Use terse, single-line bullets, not prose paragraphs "
    "or nested lists.\n"
    "- Prefer short references over detailed restatement. It is fine to leave out "
    "information the next agent can recover from the code or the files listed above.\n"
    "- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers.\n"
    "- Carry forward only user questions or requests that remain unanswered or require "
    "further action. Do not repeat ones that newer history has answered or resolved. "
    "Preserve exact wording when carrying one forward.\n"
    "- Preserve consequential workflow state, including whether changes are uncommitted, "
    "committed, pushed, under review, or merged.\n"
    "- Do not mention the summary process or that context was compacted."
)

# Relance unique officielle quand les rubriques manquent (compaction.ts:NUDGE).
_NUDGE = (
    "The previous response did not fill in the required summary template. Do not call "
    "tools. Return the summary as text using the exact section headings from the template."
)

# Rubrique des résumés écrits avec l'ancien gabarit (compaction.ts:LEGACY_HEADING) :
# un checkpoint hérité est réécrit une fois au niveau de détail actuel.
_LEGACY_HEADING = "## Additional Context"


def _template_headings() -> tuple:
    """Rubriques du gabarit (officiel : toute ligne commençant par ``##``)."""
    try:
        return tuple(ln for ln in _SUMMARY_TEMPLATE.splitlines() if ln.startswith("##"))
    except Exception:
        return ()


def build_prompt(update: bool, legacy: bool = False) -> str:
    """Prompt résumeur officiel (compaction.ts:buildPrompt, verbatim).

    ``update=False`` : premier résumé ; ``update=True`` : consolidation d'un
    checkpoint existant (``legacy=True`` : réécriture au niveau actuel).
    Ne lève jamais.
    """
    try:
        shared = [
            "Summarize only what the user and the assistant said and did. Leave out "
            "instructions and setup the assistant was given rather than told by the user: "
            "repository conventions, instruction files such as AGENTS.md, and environment "
            "details like the session ID. The next agent receives current versions of all "
            "of these separately.",
            _SUMMARY_TEMPLATE,
            _SUMMARY_RULES,
            "Do not continue the task or call tools.",
            "Return only the structured summary in the requested format. Do not include "
            "a preamble, explanation, or other commentary.",
        ]
        if not update:
            return "\n\n".join(
                [
                    "You MUST summarize the conversation above into a structured summary "
                    "that will be given to another agent to resume the work.",
                    *shared,
                ]
            )
        parts = [
            "Update the existing checkpoint in the conversation above into one consolidated summary.",
        ]
        if legacy:
            parts.append(
                "The existing checkpoint was written with an earlier format that recorded "
                "far more detail than this one asks for. Rewrite it at the level of detail "
                "described below rather than carrying its detail forward. Keep its "
                "requirements, decisions, and open questions; they came from earlier "
                "conversation with the user."
            )
        parts.extend(
            [
                "Newer history always takes precedence over the existing checkpoint. "
                "Preserve previous information unless newer history clearly contradicts, "
                "supersedes, resolves, or makes it stale. If something is no longer relevant "
                "to continuing the work, you may remove it.",
                "Incorporate newer requirements, decisions, progress, and context. Reconcile "
                "Work State and Next Move: move completed work out of Active, remove resolved "
                "blockers and answered questions, and preserve unresolved or pending work.",
                "Return only the updated Markdown sections. Do not reproduce the "
                "`<conversation-checkpoint>`, `<summary>`, or `<recent-context>` wrapper tags "
                "from the previous checkpoint.",
                *shared,
            ]
        )
        return "\n\n".join(parts)
    except Exception:
        return _NUDGE


def has_required_outline(summary_text) -> bool:
    """True si le résumé remplit le gabarit (sémantique officielle filled()).

    Une ligne au moins égale (après strip) à une rubrique du gabarit —
    comparaison exacte, sensible à la casse, comme l'officiel. Ne lève jamais.
    """
    try:
        if not isinstance(summary_text, str) or not summary_text.strip():
            return False
        heads = set(_template_headings())
        if not heads:
            return False
        # Official `filled()` semantics: one exact heading is enough. The
        # anti-stub guard lives in run_summarizer (minimum length), not here.
        for ln in summary_text.splitlines():
            try:
                if ln.strip() in heads:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def build_rewrite_prompt(bad_summary: str) -> str:
    """Prompt correctif unique (NUDGE officiel + tentative pour contexte).

    Le NUDGE officiel n'embarque pas la tentative (elle est dans l'historique
    de la requête officielle) ; notre transport mono-message n'a pas
    d'historique, on la joint donc (tronquée) pour garder la corrective utile.
    Ne lève jamais.
    """
    try:
        attempt = bad_summary if isinstance(bad_summary, str) else ""
    except Exception:
        attempt = ""
    return _NUDGE + "\n\nPrevious attempt:\n<attempt>\n" + attempt[:8000] + "\n</attempt>"


def build_summary_user_text(history_text: str, previous_summary: str | None = None) -> str:
    """Corps du message user résumeur (template compaction : chaînage previousSummary)."""
    prev = (previous_summary or "").strip() if isinstance(previous_summary, str) else ""
    if not isinstance(history_text, str):
        try:
            hist = str(history_text or "")
        except Exception:
            hist = ""
    else:
        hist = history_text
    hist = hist.strip()
    if prev:
        return (
            build_prompt(True, _LEGACY_HEADING in prev)
            + "\n\nPrevious summary:\n<previous-summary>\n"
            + prev
            + "\n</previous-summary>\n\n"
            "Conversation to summarize:\n<conversation>\n" + hist + "\n</conversation>"
        )
    return (
        "Summarize the following conversation concisely.\n\n"
        "<conversation>\n" + hist + "\n</conversation>\n\n" + build_prompt(False)
    )


def build_prompt_for_history(messages) -> str:
    """Official prompt for the real conversation (update when a checkpoint exists)."""
    prev = extract_previous_summary(messages)
    return build_prompt(prev is not None, bool(prev and _LEGACY_HEADING in prev))


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
    """Plafonne max_tokens/max_output_tokens au cap officiel (32000).

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


def _copy_messages(messages):
    """Shallow copy of message dicts (never mutate the caller's request)."""
    out = []
    for m in messages if isinstance(messages, list) else []:
        if isinstance(m, dict):
            out.append(dict(m))
    return out


def build_summarizer_body_from_history(
    history_messages,
    prompt: str,
    max_tokens: int,
    model_id: str = "",
    api: str = "chat",
    *,
    system=None,
    tools=None,
    instructions=None,
) -> dict | None:
    """Official-shape summary request: the real conversation + prompt.

    Mirrors the client: the normal request prefix is kept intact and the
    summary instructions are appended as one extra user message, so the
    provider can reuse its prompt cache. ``None`` when the history is not in
    the destination API shape (the caller then falls back to the text
    transcript, like the official client does when it has to flatten).
    """
    try:
        if not isinstance(history_messages, list) or not history_messages:
            return None
        low = str(api or "chat").lower()
        cap = _norm_cap(max_tokens)
        if low == "responses":
            items = _copy_messages(history_messages)
            body = {
                "model": model_id or "",
                "input": [
                    *items,
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                ],
                "max_output_tokens": cap,
                "stream": False,
                "store": False,
            }
            if isinstance(instructions, str) and instructions.strip():
                body["instructions"] = instructions
            if isinstance(tools, list) and tools:
                body["tools"] = tools
            if not body["model"]:
                body.pop("model", None)
            return body
        messages = _copy_messages(history_messages)
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": model_id or "",
            "messages": messages,
            "max_tokens": cap,
            "stream": False,
        }
        if low == "anthropic" and isinstance(system, (str, list)) and system:
            body["system"] = system
        if isinstance(tools, list) and tools:
            body["tools"] = tools
        if not body["model"]:
            body.pop("model", None)
        return body
    except Exception:
        return None


def _append_nudge_to_body(body: dict, api: str = "chat") -> dict | None:
    """Official corrective: same request + one NUDGE user message."""
    try:
        if not isinstance(body, dict):
            return None
        out = dict(body)
        if str(api or "chat").lower() == "responses":
            out["input"] = list(out.get("input") or []) + [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": _NUDGE}],
                }
            ]
        else:
            out["messages"] = list(out.get("messages") or []) + [
                {"role": "user", "content": _NUDGE}
            ]
        return out
    except Exception:
        return None


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

            _looks_responses = any(isinstance(_m, dict) and "type" in _m for _m in (messages or []))
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
            # [audit 2026-10-30] Responses items have no role/content: the
            # old serializer dropped function calls and their outputs, so the
            # summary lost the entire tool history.
            _mit = m.get("type")
            if _mit == "function_call":
                parts.append(
                    f"assistant tool_call {m.get('name','')}({m.get('arguments','')})"
                )
                continue
            if _mit == "function_call_output":
                parts.append(f"tool {m.get('call_id','')}: {str(m.get('output',''))[:4000]}")
                continue
            if _mit == "reasoning":
                _sum = " ".join(
                    str(x.get("text", "")) for x in (m.get("summary") or []) if isinstance(x, dict)
                )
                if _sum:
                    parts.append(f"assistant reasoning: {_sum[:4000]}")
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
            # Chat tool calls live outside content.
            for _tc in m.get("tool_calls") or []:
                if isinstance(_tc, dict):
                    _fn = _tc.get("function") if isinstance(_tc.get("function"), dict) else {}
                    parts.append(
                        f"assistant tool_call {_fn.get('name','')}({_fn.get('arguments','')})"
                    )
        text = "\n".join(parts)
        if len(text) > 120000:
            # Keep both ends: the tail carries the current state/next move.
            text = text[:60000] + "\n...[history truncated]...\n" + text[-60000:]
        return text
    except Exception:
        return ""


def extract_previous_summary(messages) -> str | None:
    """Extrait le résumé d'un checkpoint précédent (<summary>…), ou None."""
    try:
        if not isinstance(messages, list):
            return None
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            text = c if isinstance(c, str) else ""
            if isinstance(c, list):
                text = " ".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
            if "<summary>" in text and "</summary>" in text:
                s = text.split("<summary>", 1)[1].split("</summary>", 1)[0].strip()
                if s:
                    return s[:16000]
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
    full_history: bool = False,
    history_api: str | None = None,
    system=None,
    tools=None,
    instructions=None,
) -> str | None:
    """Exécute l'appel résumeur interne. Retourne le texte du résumé, ou None.

    - model_id/endpoint/protocol/api : même voie que la requête d'origine
      (ou override explicite résolu par l'appelant) ; ``api`` vaut
      ``chat`` | ``anthropic`` | ``responses`` et choisit la forme du body
      (``build_summarizer_for_api``) — parité avec l'endpoint (``/responses``
      attend ``input``, jamais ``messages``) ;
    - ``full_history`` : quand l'historique est déjà dans la forme de ``api``,
      envoie la conversation réelle + le prompt officiel (comme le client,
      pour réutiliser le cache de prompt). Sinon repli sur le transcript
      sérialisé (le client officiel fait de même quand il doit aplatir) ;
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
        _low_api = str(_api or "chat").lower()
        body = None
        if full_history and str(history_api or "").lower() == _low_api:
            body = build_summarizer_body_from_history(
                history_messages,
                build_prompt_for_history(history_messages),
                summary_max_tokens,
                model_id,
                _low_api,
                system=system,
                tools=tools,
                instructions=instructions,
            )
        if body is None:
            body = build_summarizer_for_api(user_text, summary_max_tokens, model_id, _api or "chat")
        _text_body = build_summarizer_for_api(user_text, summary_max_tokens, model_id, _api or "chat")
        try:
            _text_body["model"] = model_id
        except Exception:
            pass
        if body is None:
            body = _text_body
        # build_summarizer_for_api("chat") ne pose pas model : parité historique.
        try:
            body["model"] = model_id
        except Exception:
            pass
        try:
            headers = auth_headers_fn(protocol)
        except Exception:
            return None

        async def _send(request_body):
            try:
                resp, _ = await asyncio.wait_for(
                    do_request_fn(endpoint, request_body, headers, protocol), timeout=timeout_s
                )
            except Exception:
                return None
            try:
                if getattr(resp, "status_code", 0) != 200:
                    return {"status": getattr(resp, "status_code", 0)}
                raw = resp.content
                if isinstance(raw, (bytes, bytearray)):
                    raw = bytes(raw).decode("utf-8", errors="replace")
                return {"status": 200, "data": _json.loads(raw) if isinstance(raw, str) else {}}
            except Exception:
                return None

        sent = await _send(body)
        # Official fallback: a full-conversation summary that the provider
        # rejects as too large is resent as a shortened text transcript.
        if (
            full_history
            and isinstance(sent, dict)
            and sent.get("status") in (400, 413, 422)
            and body is not _text_body
        ):
            sent = await _send(_text_body)
        if not isinstance(sent, dict) or sent.get("status") != 200:
            return None
        data = sent.get("data") or {}
        # [audit 2026-10-30] A summary truncated by max_tokens is not a
        # valid replacement for the history.
        try:
            _fr = ""
            _choices = data.get("choices") if isinstance(data, dict) else None
            if isinstance(_choices, list) and _choices and isinstance(_choices[0], dict):
                _fr = str(_choices[0].get("finish_reason") or "")
            if not _fr and isinstance(data, dict):
                _fr = str(data.get("stop_reason") or "")
            if _fr in ("length", "max_tokens"):
                return None
        except Exception:
            pass
        summary = _extract_text(data)
        # [audit 2026-10-30] Reject a one-line stub like "## Objective"
        # (12 chars) while keeping the official one-heading outline rule.
        # A short/invalid first attempt must still get its corrective retry.
        if summary and has_required_outline(summary) and len(summary.strip()) >= 14:
            return summary
        # Aligné officiel : 1 SEULE corrective quand les rubriques manquent
        # (résumé vide/hors-format), puis échec franc (None → relais intact).
        if not summary:
            return None
        try:
            # Official corrective: resend the SAME request with one NUDGE
            # user message. The text-transcript fallback keeps the previous
            # attempt in the message (it has no conversation to append to).
            rewrite_body = None
            if full_history and str(history_api or "").lower() == _low_api:
                rewrite_body = _append_nudge_to_body(body, _low_api)
            if rewrite_body is None:
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
            if summary2 and has_required_outline(summary2) and len(summary2.strip()) >= 14:
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
                str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
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
    "build_prompt",
    "build_prompt_for_history",
    "build_summary_user_text",
    "build_summarizer_body",
    "build_summarizer_body_from_history",
    "build_summarizer_anthropic_body",
    "build_summarizer_responses_body",
    "build_summarizer_for_api",
    "should_clamp_summary",
    "cap_summary_max_tokens",
    "extract_previous_summary",
    "run_summarizer",
]
