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


def estimate_input_tokens(
    body: dict,
    *,
    encoding=None,
    extract_fn: Callable | None = None,
    debug_fn: Callable[..., None] = _noop,
    log_fn: Callable[..., None] = _noop,
) -> int:
    """Estimate input tokens from message content, tools, and tool_results."""
    try:
        chunks = []

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

        # Tools definitions
        for tool in body.get("tools", []):
            chunks.append(tool.get("name", ""))
            chunks.append(tool.get("description", ""))
            chunks.append(str(tool.get("input_schema", {})))

        # Messages
        for msg in body.get("messages", []):
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
                        elif btype == "thinking":
                            chunks.append(block.get("thinking", ""))
                        else:
                            chunks.append(block.get("text", ""))
                            chunks.append(str(block.get("input", "")))

        combined = "\n".join(chunks)
        if encoding:
            return len(encoding.encode(combined))
        return max(1, len(combined) // 3)
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
