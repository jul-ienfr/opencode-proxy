"""app.compaction.lean — amaigrissement des sorties d'outils (résumés seuls).

L'amont free digère mal les résumés géants : Hermes envoie jusqu'à 2 Mo
d'outputs intégraux → ``400 too long`` ou effondrement tiny (17-97 tokens
pour 40k d'input). Le client officiel, lui, tronque chaque sortie d'outil
(``toolOutputMaxChars``) : un résumé reste un résumé même sur sources
tronquées (compression avec pertes par construction).

``lean_summary_body`` applique la même règle, UNIQUEMENT sur trafic
compactage détecté (l'appelant gate via ``is_compaction_shape``) :
tout texte de sortie d'outil au-delà de ``max_chars`` est ramené à sa tête
+ marqueur d'omission explicite. Historique, prompts, system, tools et
sorties courtes : intacts. Ne mute jamais l'entrée (copie). Ne lève jamais :
tout input inattendu → (body inchangé, 0).
"""

from __future__ import annotations

_MARK_TEMPLATE = "\n[... {omitted} chars omitted by proxy summary-lean (tool output truncated to {kept}) ...]"


def _lean_text(text, max_chars: int) -> tuple[str, int]:
    """(texte_amaigri, coupés). Court-circuit rapide si sous le seuil."""
    try:
        if not isinstance(text, str) or len(text) <= max_chars:
            return text, 0
        kept = text[:max_chars]
        return kept + _MARK_TEMPLATE.format(omitted=len(text) - max_chars, kept=max_chars), len(text) - max_chars
    except Exception:
        return text if isinstance(text, str) else "", 0


def _lean_blocks(blocks, max_chars: int) -> tuple[list, int]:
    """Amaigrit les textes d'une liste de blocs de contenu (copie).

    Couvre les blocs texte (``text``) ET les blocs ``tool_result`` /
    ``function_call_output`` à contenu brut (str ou liste) — la forme
    réelle d'Anthropic/Chat pour les sorties d'outils.
    """
    cut = 0
    try:
        if not isinstance(blocks, list):
            return blocks, 0
        out = []
        for b in blocks:
            if not isinstance(b, dict):
                out.append(b)
                continue
            if isinstance(b.get("text"), str) and b.get("type") not in ("tool_result", "function_call_output"):
                t, c = _lean_text(b["text"], max_chars)
                cut += c
                if c:
                    nb = dict(b)
                    nb["text"] = t
                    out.append(nb)
                    continue
            if b.get("type") in ("tool_result", "function_call_output") and isinstance(b.get("content"), str):
                t, c = _lean_text(b["content"], max_chars)
                cut += c
                if c:
                    nb = dict(b)
                    nb["content"] = t
                    out.append(nb)
                    continue
                out.append(b)
                continue
            if b.get("type") in ("tool_result", "function_call_output") and isinstance(b.get("content"), list):
                ncontent, c = _lean_blocks(b["content"], max_chars)
                cut += c
                if c:
                    nb = dict(b)
                    nb["content"] = ncontent
                    out.append(nb)
                    continue
            out.append(b)
        return out, cut
    except Exception:
        return blocks if isinstance(blocks, list) else [], 0


def _is_tool_message(msg) -> bool:
    """Message de sortie d'outil : rôle tool/function OU bloc tool_result /
    function_call_output (formats Anthropic/Chat/Responses)."""
    try:
        if not isinstance(msg, dict):
            return False
        if msg.get("role") in ("tool", "function"):
            return True
        if str(msg.get("type", "")).lower() == "function_call_output":
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") in ("tool_result", "function_call_output"):
                    return True
        return False
    except Exception:
        return False


def _lean_tool_message(msg, max_chars: int) -> tuple[dict, int]:
    """Amaigrit UN message de sortie d'outil (copie). Autres messages : inchangés."""
    cut = 0
    try:
        if not _is_tool_message(msg):
            return msg, 0
        nm = dict(msg)
        content = nm.get("content")
        if isinstance(content, str):
            t, cut = _lean_text(content, max_chars)
            nm["content"] = t
        elif isinstance(content, list):
            nm["content"], cut = _lean_blocks(content, max_chars)
        # Responses : le texte vit aussi sous output/output_text.
        for key in ("output",):
            val = nm.get(key)
            if isinstance(val, str) and len(val) > max_chars:
                t, c = _lean_text(val, max_chars)
                nm[key] = t
                cut += c
            elif isinstance(val, list):
                # [{type: output_text/input_text, text: ...}, ...]
                nval, c = _lean_blocks(val, max_chars)
                nm[key] = nval
                cut += c
        return nm, cut
    except Exception:
        return msg, 0


def lean_summary_body(body, *, max_chars: int = 2000) -> tuple:
    """(body_amaigri, chars_coupés) — sorties d'outils tronquées à max_chars.

    Couvre ``messages[]`` (Anthropic/Chat) et ``input[]`` (Responses).
    Entrée non-dict / max_chars invalide → (body inchangé, 0). Ne lève jamais.
    """
    try:
        if not isinstance(body, dict):
            return body, 0
        try:
            mc = int(max_chars)
        except Exception:
            return body, 0
        if mc <= 0:
            return body, 0
        total = 0
        out = dict(body)
        for key in ("messages", "input"):
            seq = out.get(key)
            if not isinstance(seq, list):
                continue
            nseq = []
            for m in seq:
                nm, c = _lean_tool_message(m, mc)
                total += c
                nseq.append(nm)
            out[key] = nseq
        if not total:
            return body, 0
        return out, total
    except Exception:
        try:
            return body, 0
        except Exception:
            return {}, 0


__all__ = ["lean_summary_body"]
