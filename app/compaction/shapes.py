"""app.compaction — détection « shape compaction » (Phase 1, lecture seule).

Fonction pure, stdlib uniquement, ne lève jamais : tout body inattendu → False.
Utilisée pour : exclusion du response cache, bypass 503, log [compaction] strict.

Configuration (optionnelle, via paramètres à is_compaction_shape) :
- min_chars_implicit : borne anti-faux-positif sur les shapes implicites
  (user-only sans marqueur). Défaut 1000 chars ≈ 300 tokens : très
  conservateur, aucun vrai compactage en dessous. Les shapes à marqueur
  explicite n'y sont PAS soumises (si le client dit "compaction", on le
  croit même sur un texte court).
"""

from __future__ import annotations

# Marqueurs explicites posés par les clients dans le texte user.
# Couvre les shapes qui ne sont PAS 100 % user-only :
# - Hermes Agent : "[CONTEXT COMPACTION – REFERENCE ONLY]"
# - Claude-Code   : "<conversation-checkpoint>"
_MARKERS = ("conversation-checkpoint", "context compaction")

# Taille minimale (caractères) du texte cumulé pour les shapes implicites
# (user-only sans marqueur) — voir docstring du module. Valeur par défaut ;
# sera configurable via config.yaml:server_compaction.min_chars_implicit
# (Phase 2 — pour l'instant la valeur en dur fait foi).
_DEFAULT_MIN_CHARS_IMPLICIT = 1000


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


def _messages_shape(body: dict, min_chars_implicit: int = _DEFAULT_MIN_CHARS_IMPLICIT) -> bool:
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
    if total < min_chars_implicit:
        return False
    return _no_tools(body) and _no_system(body)


def _responses_shape(body: dict, min_chars_implicit: int = _DEFAULT_MIN_CHARS_IMPLICIT) -> bool:
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
    if total < min_chars_implicit:
        return False
    if not _no_tools(body):
        return False
    instr = body.get("instructions")
    if isinstance(instr, str) and instr.strip():
        return False
    return True


def _marker_shape(body: dict) -> bool:
    """Shape à marqueur : un texte contient un marqueur de compaction explicite.

    Couvre Hermes ([CONTEXT COMPACTION – REFERENCE ONLY]) et Claude-Code
    (<conversation-checkpoint>), y compris mélangés à un historique multi-rôles.

    Le marqueur est cherché dans TOUS les rôles (user ET assistant) : certains
    clients (relevé sur traces réelles : 38 corps sur 132) injectent le
    checkpoint dans un message ``assistant`` (replay de la conversation
    précédente) et non dans un message ``user``. Ne scanner que ``user``
    faisait rater ces requêtes, qui partaient alors vers l'amont avec
    l'historique complet — cause directe du 400-overflow définitif côté client.
    Un marqueur de compaction n'est jamais émis par un tour agent ordinaire :
    le tester quel que soit le rôle ne crée pas de faux positif.

    PAS de veto sur les outils déclarés : les compactions Claude Code
    rejouent l'historique complet AVEC leurs 15+ outils (prouvé sur traces
    réelles : requêtes 40-76k tokens avec tools, sorties tiny 17-80).
    Exiger l'absence d'outils les rendait invisibles (ni exclusion cache,
    ni relais intact, ni tiny-retry, ni lean) — boucle de thrash garantie.
    Un tour normal ne contient jamais ces marqueurs : les croire même avec
    outils ne crée pas de faux positif (pire cas : un tour qui discute de
    « context compaction » avec outils → bypass cache + fetch bufferisé à
    contenu identique, inoffensif).
    """
    if not isinstance(body, dict):
        return False
    texts: list[str] = []
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            texts.append(_text_of_content(m.get("content")))
    inp = body.get("input")
    if isinstance(inp, list):
        for it in inp:
            if not isinstance(it, dict):
                continue
            if it.get("type", "message") != "message":
                continue
            texts.append(_text_of_content(it.get("content")))
    return any(_has_marker(t) for t in texts if t)


def is_native_compaction(body: dict) -> bool:
    """True si le body porte un compactage natif provider (passthrough verbatim).

    - Anthropic : ``context_management.edits[]`` avec ``type`` contenant
      ``compact`` (``compact_20260112``), ou bloc ``compaction`` dans
      ``messages[].content`` / ``system`` ;
    - OpenAI Responses : ``context_management`` avec ``compact_threshold``,
      item ``compaction`` / ``compaction_trigger`` dans ``input``, ou
      ``previous_response_id`` + ``store:false`` enchaîné après compaction ;
    - ``/responses/compact`` standalone : ``{model, input}`` fenêtré (le handler
      le traite comme natif pour exclusion cache + relais intact).

    L'item/bloc natif est opaque (chiffré provider) : le proxy le relaie tel
    quel, jamais interprété ni élagué. Ne lève jamais.
    """
    try:
        if not isinstance(body, dict):
            return False
        cm = body.get("context_management")
        if isinstance(cm, dict):
            if "compact_threshold" in cm:
                return True
            edits = cm.get("edits")
            if isinstance(edits, list):
                for e in edits:
                    if isinstance(e, dict) and "compact" in str(e.get("type", "")).lower():
                        return True
        # compaction explicite demandée (beta compact-*) : {"compaction": {"type": "summarize"}}
        comp = body.get("compaction")
        if isinstance(comp, dict) and str(comp.get("type", "")).lower() in ("summarize", "auto"):
            return True
        # Bloc compaction rejoué dans l'historique (Anthropic) : {"type": "compaction", ...}
        for key in ("messages", "input"):
            seq = body.get(key)
            if isinstance(seq, list):
                for m in seq:
                    if not isinstance(m, dict):
                        continue
                    content = m.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and str(b.get("type", "")).lower() == "compaction":
                                return True
                    # Item Responses opaque : {"type": "compaction", "id": ..., "encrypted_content": ...}
                    if str(m.get("type", "")).lower() in ("compaction", "compaction_trigger"):
                        return True
        return False
    except Exception:
        return False


def is_compaction_shape(body: dict, min_chars_implicit: int | None = None) -> bool:
    """True si le body ressemble à une requête de (résumé de) compaction.

    Quatre formes couvertes :
    1. officielle OpenCode : messages 100 % user texte pur, sans outils ni system,
       et texte cumulé >= min_chars_implicit (défaut _DEFAULT_MIN_CHARS_IMPLICIT ;
       un vrai compactage embarque l'historique complet — jamais 2 caractères
       comme "hi") ;
    2. Responses API équivalente (input 100 % user texte pur, même borne) ;
    3. à marqueur : texte quelconque contenant 'conversation-checkpoint' ou
       'context compaction' (Hermes, Claude-Code), SANS borne de taille et
       SANS veto outils (les compactions rejouent l'historique avec outils ;
       si le client dit "compaction", on le croit) ;
    4. native provider (``is_native_compaction``) : ``context_management`` /
       bloc ou item ``compaction`` opaque — passthrough verbatim, toujours
       détecté même avec outils/system (le provider gère le pairing).

    Jamais de borne sur max_tokens / taille : seuils éventuels en config, pas en dur.
    """
    try:
        if not isinstance(body, dict):
            return False
        try:
            if bool(is_native_compaction(body)):
                return True
        except Exception:
            pass
        bound = _DEFAULT_MIN_CHARS_IMPLICIT if min_chars_implicit is None else min_chars_implicit
        if _messages_shape(body, bound):
            return True
        if _responses_shape(body, bound):
            return True
        return _marker_shape(body)
    except Exception:
        return False


__all__ = ["is_compaction_shape", "is_native_compaction"]
