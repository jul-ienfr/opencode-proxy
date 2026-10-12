"""app.compaction.truncate — forward d'historique condensé (Phase 2, pur).

Fonctions pures, stdlib uniquement, ne lèvent jamais : tout input inattendu
→ repli sûr (historique original inchangé).

Règles (fidélité client officiel + garde-fous proxy) :
- le résumé remplace l'historique ancien SANS le dupliquer (pas de
  double-facturation : l'original n'est plus renvoyé) ;
- keep_recent_pairs : paires user/assistant récentes jamais condensées,
  gardées par PAIRES COMPLÈTES (tool_call/tool_result ensemble) pour ne pas
  déclencher _drop_orphan_* côté proxy ;
- troncation uniquement aux frontières de paires : un tool_result sans son
  tool_call (ou l'inverse) n'est jamais émis seul — la paire entière bascule
  d'un côté ou de l'autre ;
- le résumé est injecté comme message user <conversation-checkpoint>…
  (format exact du client officiel, cf. plan Phase 2 §4).
"""

from __future__ import annotations


def _role_of(msg) -> str:
    if isinstance(msg, dict):
        r = msg.get("role")
        if isinstance(r, str):
            return r
    return ""


def _has_tool_call(msg) -> bool:
    """True si un message assistant porte au moins un tool_call (formats OAI/Anthropic)."""
    try:
        if not isinstance(msg, dict):
            return False
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") in ("tool_use", "function_call"):
                    return True
                if isinstance(b, dict) and b.get("type") == "function" and isinstance(b.get("function"), dict):
                    return True
        for k in ("tool_calls", "tool_uses", "function_call"):
            v = msg.get(k)
            if isinstance(v, list) and v:
                return True
            if isinstance(v, dict) and v:
                return True
        return False
    except Exception:
        return False


def _is_tool_result(msg) -> bool:
    """True si le message est un résultat d'outil (formats OAI/Anthropic)."""
    try:
        if not isinstance(msg, dict):
            return False
        if _role_of(msg) in ("tool", "function"):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") in ("tool_result", "function_call_output"):
                    return True
        return False
    except Exception:
        return False


def _pair_text(pair) -> str:
    """Texte d'une paire (role: contenu) pour estimation tokens. Ne lève jamais."""
    try:
        parts = []
        for m in pair or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, list):
                txt = " ".join(
                    str(b.get("text", b.get("content", "")))
                    if isinstance(b, dict) else str(b)
                    for b in content
                )
            else:
                txt = "" if content is None else str(content)
            parts.append(f"{role}: {txt}")
        return "\n".join(parts)
    except Exception:
        return ""


def split_keep_recent(messages, keep_recent_pairs: int, keep_tokens=None, estimate_fn=None):
    """Scinde (ancien, récent) : paires complètes, par nombre OU par budget tokens.

    Mode legacy (défaut) : ``keep_recent_pairs`` paires max.
    Mode budget (aligné officiel ``keep.tokens``) : ``keep_tokens`` (int>0) +
    ``estimate_fn(texte_paire) -> int`` — on garde les paires les plus
    récentes jusqu'au budget, TOUJOURS au moins la paire la plus récente, et
    la coupe reste sur frontière user (jamais d'orphelin). Sans estimate_fn
    ou budget invalide : repli legacy. Ne lève jamais.
    """
    try:
        if not isinstance(messages, list) or not messages:
            return [], []
        try:
            budget = int(keep_tokens) if keep_tokens is not None else 0
        except Exception:
            budget = 0
        if budget <= 0 or not callable(estimate_fn):
            return _split_by_pairs(messages, keep_recent_pairs)
        user_idx = [
            i for i, m in enumerate(messages)
            if _role_of(m) == "user" and not _is_tool_result(m)
        ]
        if not user_idx:
            return list(messages), []
        # Paires (début, fin) de la plus récente à la plus ancienne.
        bounds = []
        for k, start in enumerate(user_idx):
            end = user_idx[k + 1] if k + 1 < len(user_idx) else len(messages)
            bounds.append((start, end))
        total = 0
        cut = len(messages)
        for start, end in reversed(bounds):
            try:
                size = int(estimate_fn(_pair_text(messages[start:end]))) or 0
            except Exception:
                # Estimation fautive : repli legacy immédiat (sûr).
                return _split_by_pairs(messages, keep_recent_pairs)
            if size < 0:
                size = 0
            if total + size > budget and total > 0:
                break
            total += size
            cut = start
            if total >= budget:
                break
        return list(messages[:cut]), list(messages[cut:])
    except Exception:
        try:
            return list(messages), []
        except Exception:
            return [], []


def _split_by_pairs(messages, keep_recent_pairs: int):
    """Cœur legacy : coupe aux N dernières paires (extrait de split_keep_recent)."""
    try:
        n = keep_recent_pairs if isinstance(keep_recent_pairs, int) and keep_recent_pairs > 0 else 0
        if n <= 0:
            return list(messages), []
        # Index des messages user = frontières de paires.
        # Un tool_result à role:user N'est PAS une frontière : c'est la
        # continuation du tour précédent. Sinon la coupe pourrait démarrer
        # le récent sur un tool_result orphelin (séparé de son tool_call).
        user_idx = [
            i for i, m in enumerate(messages)
            if _role_of(m) == "user" and not _is_tool_result(m)
        ]
        if not user_idx:
            # Pas de vrai tour user (que des tool_result, ex. tool-only) :
            # tout est « ancien », rien de gardable par paires.
            return list(messages), []
        cut = user_idx[max(0, len(user_idx) - n)]
        return list(messages[:cut]), list(messages[cut:])
    except Exception:
        try:
            return list(messages), []
        except Exception:
            return [], []


def build_checkpoint_summary(summary_text: str, recent_count: int) -> dict:
    """Construit le message user <conversation-checkpoint> portant le résumé."""
    text = summary_text if isinstance(summary_text, str) else ""
    return {
        "role": "user",
        "content": (
            "<conversation-checkpoint>"
            f"<summary>{text}</summary>"
            f"<recent-context>omitted, {recent_count} recent messages preserved verbatim</recent-context>"
            "</conversation-checkpoint>"
        ),
    }


def build_condensed_history(messages, summary_text: str, keep_recent_pairs: int,
                            keep_tokens=None, estimate_fn=None):
    """[checkpoint, *recent] : historique condensé prêt à forwarder.

    ``keep_tokens`` + ``estimate_fn`` : mode budget (aligné officiel) ;
    sinon paires fixes. Repli sûr : si messages invalide/vide ou résumé
    vide → (None, messages originaux inchangés).
    """
    try:
        if not isinstance(messages, list) or not messages:
            return None, messages
        if not isinstance(summary_text, str) or not summary_text.strip():
            return None, messages
        _old, recent = split_keep_recent(messages, keep_recent_pairs, keep_tokens, estimate_fn)
        checkpoint = build_checkpoint_summary(summary_text.strip(), len(recent))
        return [checkpoint] + recent, recent
    except Exception:
        return None, messages


def build_checkpoint_input(summary_text: str, recent_count: int) -> dict:
    """Construit l'item Responses portant le résumé (forme ``input``, jamais ``messages``).

    Miroir Responses de ``build_checkpoint_summary`` : item
    ``{"type": "message", "role": "user", "content": [{"type": "input_text",
    "text": "<conversation-checkpoint>…"}]}`` — pas de ``tool_choice``,
    pas d'item ``compaction`` opaque fabriqué (le proxy ne forge jamais
    d'item chiffré provider, relay-only).
    """
    text = summary_text if isinstance(summary_text, str) else ""
    return {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "<conversation-checkpoint>"
                    f"<summary>{text}</summary>"
                    f"<recent-context>omitted, {recent_count} recent messages preserved verbatim</recent-context>"
                    "</conversation-checkpoint>"
                ),
            }
        ],
    }


def build_condensed_input(history_input, summary_text: str, keep_recent_pairs: int,
                          keep_tokens=None, estimate_fn=None):
    """[checkpoint_input, *recent] : fenêtre Responses condensée prête à forwarder.

    Miroir Responses de ``build_condensed_history`` (budget ou paires).
    """
    try:
        if not isinstance(history_input, list) or not history_input:
            return None, history_input
        if not isinstance(summary_text, str) or not summary_text.strip():
            return None, history_input
        _old, recent = split_keep_recent(history_input, keep_recent_pairs, keep_tokens, estimate_fn)
        checkpoint = build_checkpoint_input(summary_text.strip(), len(recent))
        return [checkpoint] + recent, recent
    except Exception:
        return None, history_input


__all__ = [
    "split_keep_recent",
    "build_checkpoint_summary",
    "build_condensed_history",
    "build_checkpoint_input",
    "build_condensed_input",
]
