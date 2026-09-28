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


def split_keep_recent(messages, keep_recent_pairs: int):
    """Scinde (ancien, récent) : récent = keep_recent_pairs paires complètes max.

    Une « paire » = 1 message user + les messages suivants jusqu'au prochain
    user (réponses assistant + tool_result inclus). La coupe se fait toujours
    sur un message user : jamais de tool_result orphelin côté récent, jamais
    de tool_call séparé de ses résultats.
    """
    try:
        if not isinstance(messages, list) or not messages:
            return [], []
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


def build_condensed_history(messages, summary_text: str, keep_recent_pairs: int):
    """[checkpoint, *recent] : historique condensé prêt à forwarder.

    Repli sûr : si messages invalide/vide ou résumé vide → (None, messages
    originaux inchangés) — l'appelant relaie alors l'erreur intacte.
    """
    try:
        if not isinstance(messages, list) or not messages:
            return None, messages
        if not isinstance(summary_text, str) or not summary_text.strip():
            return None, messages
        _old, recent = split_keep_recent(messages, keep_recent_pairs)
        checkpoint = build_checkpoint_summary(summary_text.strip(), len(recent))
        return [checkpoint] + recent, recent
    except Exception:
        return None, messages


__all__ = [
    "split_keep_recent",
    "build_checkpoint_summary",
    "build_condensed_history",
]
