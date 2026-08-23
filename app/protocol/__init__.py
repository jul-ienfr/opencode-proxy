"""
app.protocol — converters Anthropic<->OpenAI (re-export de protocol_mapping.py)

Extraction de opencode.py: toute la conversion vit déjà dans protocol_mapping.py
(1182l, orjson, orphan filter, dedup CACHE_REWRITE_MODELS). Ce package est le
facade pour la future DI.
"""

from protocol_mapping import (
    CACHE_REWRITE_MODELS as CACHE_REWRITE_MODELS,
)
from protocol_mapping import (
    _drop_orphan_responses_input as _drop_orphan_responses_input,
)
from protocol_mapping import (
    _drop_orphan_tool_messages as _drop_orphan_tool_messages,
)
from protocol_mapping import (
    _effort_to_reasoning as _effort_to_reasoning,
)
from protocol_mapping import (
    anthropic_to_openai as anthropic_to_openai,
)
from protocol_mapping import (
    anthropic_to_openai_response as anthropic_to_openai_response,
)
from protocol_mapping import (
    anthropic_to_openai_responses as anthropic_to_openai_responses,
)
from protocol_mapping import (
    openai_to_anthropic as openai_to_anthropic,
)
from protocol_mapping import (
    openai_to_anthropic_request as openai_to_anthropic_request,
)

__all__ = [
    "CACHE_REWRITE_MODELS",
    "anthropic_to_openai",
    "anthropic_to_openai_response",
    "anthropic_to_openai_responses",
    "openai_to_anthropic",
    "openai_to_anthropic_request",
    "_drop_orphan_tool_messages",
    "_drop_orphan_responses_input",
    "_effort_to_reasoning",
]
