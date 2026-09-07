"""
app.protocol — converters Anthropic<->OpenAI (re-export de .mapping)

[Phase 4 refonte] Domicile canonique : app.protocol.mapping (déplacé depuis
protocol_mapping.py, shim conservé). Cette façade expose le sous-ensemble
historique ; la surface complète vit dans app.protocol.mapping.
"""

from app.protocol.mapping import CACHE_REWRITE_MODELS as CACHE_REWRITE_MODELS
from app.protocol.mapping import _drop_orphan_responses_input as _drop_orphan_responses_input
from app.protocol.mapping import _drop_orphan_tool_messages as _drop_orphan_tool_messages
from app.protocol.mapping import _effort_to_reasoning as _effort_to_reasoning
from app.protocol.mapping import anthropic_to_openai as anthropic_to_openai
from app.protocol.mapping import anthropic_to_openai_response as anthropic_to_openai_response
from app.protocol.mapping import anthropic_to_openai_responses as anthropic_to_openai_responses
from app.protocol.mapping import openai_to_anthropic as openai_to_anthropic
from app.protocol.mapping import openai_to_anthropic_request as openai_to_anthropic_request

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
