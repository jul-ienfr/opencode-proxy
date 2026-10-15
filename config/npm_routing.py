"""config.npm_routing — matrice `npm` → endpoint (parité catalogue officiel).

Règle officielle (`model.ts:141-162`, doc COMPARAISON §1) :
- `@ai-sdk/anthropic` → `/v1/messages`
- `@ai-sdk/openai` → `/v1/responses`
- `@ai-sdk/openai-compatible` → `/v1/chat/completions`
- autre `npm` (ex. `@ai-sdk/google` ×8, `@ai-sdk/mistral` ×1, N3) → indéductible.

Module PUR (aucun import projet) : l'hôte (`config/settings.py`) reste seul
juge du routage ; ce module expose la vérité catalogue pour le job diff
lecture seule (`scripts/check_models_catalog.py`) et les tests.
"""
from __future__ import annotations

NPM_ANTHROPIC = "@ai-sdk/anthropic"
NPM_OPENAI = "@ai-sdk/openai"
NPM_OPENAI_COMPATIBLE = "@ai-sdk/openai-compatible"

NPM_TO_ENDPOINT = {
    NPM_ANTHROPIC: "/v1/messages",
    NPM_OPENAI: "/v1/responses",
    NPM_OPENAI_COMPATIBLE: "/v1/chat/completions",
}

# N3 : npm hors table → endpoint indéductible (ne jamais inventer).
KNOWN_NPMS = frozenset(NPM_TO_ENDPOINT)


def endpoint_for_npm(npm: object) -> str | None:
    """Endpoint officiel pour un `npm` catalogue, ou `None` si indéductible."""
    if not isinstance(npm, str) or not npm:
        return None
    return NPM_TO_ENDPOINT.get(npm.strip())


def is_routable_npm(npm: object) -> bool:
    return endpoint_for_npm(npm) is not None


__all__ = [
    "KNOWN_NPMS",
    "NPM_ANTHROPIC",
    "NPM_OPENAI",
    "NPM_OPENAI_COMPATIBLE",
    "NPM_TO_ENDPOINT",
    "endpoint_for_npm",
    "is_routable_npm",
]
