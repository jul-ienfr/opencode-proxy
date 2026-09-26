"""app.compaction — détection « shape compaction » (Phase 1, lecture seule).

Fonction pure, stdlib uniquement, ne lève jamais : tout body inattendu → False.
Utilisée pour : exclusion du response cache, bypass 503, log [compaction] strict.
"""

from __future__ import annotations

# Marqueurs explicites posés par les clients dans le texte user.
# Couvre les shapes qui ne sont PAS 100 % user-only :
# - Hermes Agent : "[CONTEXT COMPACTION – REFERENCE ONLY]"
# - Claude-Code   : "<conversation-checkpoint>"
_MARKERS = ("conversation-checkpoint", "context compaction")

# Taille minimale (caractères) du texte cumulé pour les shapes implicites
# (user-only sans marqueur). Un vrai compactage embarque l'historique complet
# sérialisé — toujours des dizaines de milliers de caractères. Sans cette borne,
# un simple {"role": "user", "content": "hi"} (2 chars) serait un faux positif.
# 1000 chars ≈ 300 tokens : très conservateur, aucun vrai compactage en dessous.
# Les shapes à marqueur explicite ne sont PAS soumises à cette borne
# (si le client dit "compaction", on le croit même sur un texte court).
_MIN_CHARS_IMPLICIT = 1000


def _text_of_content(content) -> str:
    """Extrait le texte concaténé d'un champ content (str ou liste de blocs)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type", "")
            if t in ("text", "input_text", "output_text"):
                txt = b.get("text", "")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def _has_marker(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _MARKERS)


def _is_text_only_message(msg: dict) -> tuple[bool, str]:
    """(ok, texte) — ok=True si message user texte pur (aucun bloc outil/thinking)."""
    if not isinstance(msg, dict):
        return False, ""
    if msg.get("role") != "user":
        return False, ""
    content = msg.get("content")
    if isinstance(content, str):
        return (True, content) if content else (False, "")
    if isinstance(content, list):
        if not content:
            return False, ""
        texts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                return False, ""
            t = b.get("type", "")
            if t in ("text", "input_text"):
                txt = b.get("text", "")
                if not isinstance(txt, str):
                    return False, ""
                texts.append(txt)
            else:
                # tool_use / tool_result / thinking / image / etc → pas une shape compaction officielle
                return False, ""
        return True, "\n".join(texts)
    return False, ""


def _no_tools(body: dict) -> bool:
    tools = body.get("tools")
    return tools is None or tools == []


def _no_system(body: dict) -> bool:
    system = body.get("system")
    if system is None:
        return True
    if isinstance(system, str):
        return system.strip() == ""
    if isinstance(system, list):
        return len(system) == 0
    return False


def _messages_shape(body: dict) -> bool:
    """Shape messages/chat : body['messages'] non vide, 100 % user texte pur."""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    total = 0
    for m in msgs:
        ok, txt = _is_text_only_message(m)
        if not ok:
            return False
        total += len(txt)
    if total < _MIN_CHARS_IMPLICIT:
        return False
    return _no_tools(body) and _no_system(body)


def _responses_shape(body: dict) -> bool:
    """Shape Responses API : body['input'] non vide, 100 % messages user texte pur."""
    inp = body.get("input")
    if not isinstance(inp, list) or not inp:
        return False
    total = 0
    for it in inp:
        if not isinstance(it, dict):
            return False
        t = it.get("type", "message")
        if t != "message":
            # function_call / function_call_output / reasoning / etc → pas une compaction
            return False
        if it.get("role") != "user":
            return False
        content = it.get("content")
        txt = _text_of_content(content)
        if not txt:
            return False
        total += len(txt)
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") not in ("text", "input_text", "output_text"):
                    return False
    if total < _MIN_CHARS_IMPLICIT:
        return False
    if not _no_tools(body):
        return False
    instr = body.get("instructions")
    if isinstance(instr, str) and instr.strip():
        return False
    return True


def _marker_shape(body: dict) -> bool:
    """Shape à marqueur : un texte user contient un marqueur de compaction explicite.

    Couvre Hermes ([CONTEXT COMPACTION – REFERENCE ONLY]) et Claude-Code
    (<conversation-checkpoint>), y compris mélangés à un historique multi-rôles.
    Exige quand même l'absence d'outils déclarés (tools null/vide) pour ne pas
    confondre avec un tour agent normal.
    """
    if not _no_tools(body):
        return False
    texts: list[str] = []
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            texts.append(_text_of_content(m.get("content")))
    inp = body.get("input")
    if isinstance(inp, list):
        for it in inp:
            if not isinstance(it, dict):
                continue
            if it.get("type", "message") != "message" or it.get("role") != "user":
                continue
            texts.append(_text_of_content(it.get("content")))
    return any(_has_marker(t) for t in texts if t)


def is_compaction_shape(body: dict) -> bool:
    """True si le body ressemble à une requête de (résumé de) compaction.

    Trois formes couvertes :
    1. officielle OpenCode : messages 100 % user texte pur, sans outils ni system,
       et texte cumulé >= _MIN_CHARS_IMPLICIT (un vrai compactage embarque
       l'historique complet — jamais 2 caractères comme "hi") ;
    2. Responses API équivalente (input 100 % user texte pur, même borne) ;
    3. à marqueur : texte user contenant 'conversation-checkpoint' ou
       'context compaction' (Hermes, Claude-Code), outils absents, SANS borne
       de taille (si le client dit "compaction", on le croit).

    Jamais de borne sur max_tokens / taille : seuils éventuels en config, pas en dur.
    """
    try:
        if not isinstance(body, dict):
            return False
        if _messages_shape(body):
            return True
        if _responses_shape(body):
            return True
        return _marker_shape(body)
    except Exception:
        return False


__all__ = ["is_compaction_shape"]
