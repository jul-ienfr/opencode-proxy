"""protocol_mapping — SHIM Phase 4 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``app.protocol.mapping`` (déplacement pur, contenu
identique). Ce module re-exporte toute la surface historique
(convertisseurs, caches mutables ``_anthropic_cache`` /
``_redacted_thinking_cache`` — MÊMES objets, état partagé — constantes,
état SSE ; seule ``tiktoken`` — import conditionnel du canonique, jamais
consommée via ce chemin — n'est pas reprise) pour les consommateurs
historiques : ``tests/test_conversion_*``, ``tests/test_tool_*``,
``tests/test_thinking_e2e``…, ``scripts/bench_perf.py``
(façade gelée ADR-006 §5).

Seule limite documentée : les compteurs int rebindés via ``global``
(``_conversion_hits``/``_conversion_misses``) se lisent via
``conversion_cache_stats()`` (aucun lecteur direct en repo).

Suppression prévue Phase 9 après preuve de non-usage (``grep``).
"""

import copy as copy  # noqa: F401  # surface historique (noms importés re-exportés)
import hashlib as hashlib  # noqa: F401
import json as json  # noqa: F401
import re as re  # noqa: F401
import time as time  # noqa: F401
import uuid as uuid  # noqa: F401
from collections import OrderedDict as OrderedDict  # noqa: F401
from typing import Any as Any  # noqa: F401

import orjson as _orjson  # noqa: F401

from app.protocol.mapping import _HAS_SYNTHETIC_REASONING_KEY as _HAS_SYNTHETIC_REASONING_KEY
from app.protocol.mapping import _IS_LOCAL_SIG_CACHE_MAX as _IS_LOCAL_SIG_CACHE_MAX
from app.protocol.mapping import _JSON_LIB as _JSON_LIB
from app.protocol.mapping import _P_CLASS_BMP as _P_CLASS_BMP
from app.protocol.mapping import _REDACTED_THINKING_CACHE_MAX as _REDACTED_THINKING_CACHE_MAX
from app.protocol.mapping import _SCHEMA_PROFILES as _SCHEMA_PROFILES
from app.protocol.mapping import _TOOL_NAME_MAP_KEY as _TOOL_NAME_MAP_KEY
from app.protocol.mapping import _TOOL_NAME_RE as _TOOL_NAME_RE
from app.protocol.mapping import CACHE_REWRITE_MODELS as CACHE_REWRITE_MODELS
from app.protocol.mapping import THINKING_MODELS as THINKING_MODELS
from app.protocol.mapping import TOOL_NAME_MAX_LEN as TOOL_NAME_MAX_LEN
from app.protocol.mapping import ResponsesSseState as ResponsesSseState
from app.protocol.mapping import _anthropic_cache as _anthropic_cache
from app.protocol.mapping import _anthropic_cache_key as _anthropic_cache_key
from app.protocol.mapping import _anthropic_cache_max as _anthropic_cache_max
from app.protocol.mapping import _anthropic_to_responses_request as _anthropic_to_responses_request
from app.protocol.mapping import _cfg_settings as _cfg_settings
from app.protocol.mapping import _chat_to_responses_request as _chat_to_responses_request
from app.protocol.mapping import _conversion_epoch as _conversion_epoch
from app.protocol.mapping import _conversion_hits as _conversion_hits
from app.protocol.mapping import _conversion_misses as _conversion_misses
from app.protocol.mapping import _drop_orphan_responses_input as _drop_orphan_responses_input
from app.protocol.mapping import _drop_orphan_tool_messages as _drop_orphan_tool_messages
from app.protocol.mapping import _effort_to_reasoning as _effort_to_reasoning
from app.protocol.mapping import _encoding as _encoding
from app.protocol.mapping import _extract_cache_creation_tokens as _extract_cache_creation_tokens
from app.protocol.mapping import _extract_cache_tokens as _extract_cache_tokens
from app.protocol.mapping import _extract_text as _extract_text
from app.protocol.mapping import _find_split_point as _find_split_point
from app.protocol.mapping import _inverse_tool_name_lookup as _inverse_tool_name_lookup
from app.protocol.mapping import _is_local_sig_cache as _is_local_sig_cache
from app.protocol.mapping import _is_local_signature as _is_local_signature
from app.protocol.mapping import _json_dumps as _json_dumps
from app.protocol.mapping import _json_dumps_str as _json_dumps_str
from app.protocol.mapping import _json_loads as _json_loads
from app.protocol.mapping import _local_signature as _local_signature
from app.protocol.mapping import _looks_encrypted_reasoning as _looks_encrypted_reasoning
from app.protocol.mapping import _normalize_tool_schema as _normalize_tool_schema
from app.protocol.mapping import _orig_anthropic_to_openai as _orig_anthropic_to_openai
from app.protocol.mapping import _reasoning_seen_ids as _reasoning_seen_ids
from app.protocol.mapping import _redacted_thinking_cache as _redacted_thinking_cache
from app.protocol.mapping import _register_defensive_short as _register_defensive_short
from app.protocol.mapping import _remap_responses_history_names as _remap_responses_history_names
from app.protocol.mapping import _remap_responses_tool_choice as _remap_responses_tool_choice
from app.protocol.mapping import _resolve_schema_profile as _resolve_schema_profile
from app.protocol.mapping import _rewrite_unicode_properties as _rewrite_unicode_properties
from app.protocol.mapping import _responses_sse_to_chat_deltas as _responses_sse_to_chat_deltas
from app.protocol.mapping import _responses_to_anthropic_response as _responses_to_anthropic_response
from app.protocol.mapping import _responses_to_chat_response as _responses_to_chat_response
from app.protocol.mapping import _responses_tool_cache as _responses_tool_cache
from app.protocol.mapping import _responses_tool_index_map as _responses_tool_index_map
from app.protocol.mapping import _restructure_for_cache as _restructure_for_cache
from app.protocol.mapping import _sanitize_native_responses_request as _sanitize_native_responses_request
from app.protocol.mapping import _short_tool_name as _short_tool_name
from app.protocol.mapping import _strip_billing_header as _strip_billing_header
from app.protocol.mapping import _thinking_cfg as _thinking_cfg
from app.protocol.mapping import anthropic_to_openai as anthropic_to_openai
from app.protocol.mapping import anthropic_to_openai_response as anthropic_to_openai_response
from app.protocol.mapping import anthropic_to_openai_responses as anthropic_to_openai_responses
from app.protocol.mapping import conversion_cache_stats as conversion_cache_stats
from app.protocol.mapping import openai_chat_to_responses as openai_chat_to_responses
from app.protocol.mapping import openai_responses_to_anthropic as openai_responses_to_anthropic
from app.protocol.mapping import openai_to_anthropic as openai_to_anthropic
from app.protocol.mapping import openai_to_anthropic_request as openai_to_anthropic_request
from app.protocol.mapping import restore_tool_name as restore_tool_name
from app.protocol.mapping import sanitize_tool_names as sanitize_tool_names
from app.protocol.mapping import strip_synthetic_thinking as strip_synthetic_thinking
from config import CACHE_MIN_PROMPT_SIZE as CACHE_MIN_PROMPT_SIZE  # noqa: F401
from config import yaml_get as yaml_get  # noqa: F401
from dashboard.display import debug as _debug  # noqa: F401
from dashboard.display import log as _log  # noqa: F401

__all__ = [
    "CACHE_REWRITE_MODELS",
    "ResponsesSseState",
    "THINKING_MODELS",
    "TOOL_NAME_MAX_LEN",
    "_HAS_SYNTHETIC_REASONING_KEY",
    "_JSON_LIB",
    "_IS_LOCAL_SIG_CACHE_MAX",
    "_REDACTED_THINKING_CACHE_MAX",
    "_SCHEMA_PROFILES",
    "_TOOL_NAME_MAP_KEY",
    "_TOOL_NAME_RE",
    "_anthropic_cache",
    "_anthropic_cache_key",
    "_anthropic_cache_max",
    "_anthropic_to_responses_request",
    "_cfg_settings",
    "_chat_to_responses_request",
    "_conversion_epoch",
    "_conversion_hits",
    "_conversion_misses",
    "_drop_orphan_responses_input",
    "_drop_orphan_tool_messages",
    "_effort_to_reasoning",
    "_encoding",
    "_extract_cache_creation_tokens",
    "_extract_cache_tokens",
    "_extract_text",
    "_find_split_point",
    "_inverse_tool_name_lookup",
    "_is_local_sig_cache",
    "_is_local_signature",
    "_json_dumps",
    "_json_dumps_str",
    "_json_loads",
    "_local_signature",
    "_looks_encrypted_reasoning",
    "_normalize_tool_schema",
    "_orig_anthropic_to_openai",
    "_P_CLASS_BMP",
    "_reasoning_seen_ids",
    "_redacted_thinking_cache",
    "_register_defensive_short",
    "_remap_responses_history_names",
    "_remap_responses_tool_choice",
    "_resolve_schema_profile",
    "_rewrite_unicode_properties",
    "_responses_sse_to_chat_deltas",
    "_responses_to_anthropic_response",
    "_responses_to_chat_response",
    "_responses_tool_cache",
    "_responses_tool_index_map",
    "_restructure_for_cache",
    "_sanitize_native_responses_request",
    "_short_tool_name",
    "_strip_billing_header",
    "_thinking_cfg",
    "anthropic_to_openai",
    "anthropic_to_openai_response",
    "anthropic_to_openai_responses",
    "conversion_cache_stats",
    "openai_chat_to_responses",
    "openai_responses_to_anthropic",
    "openai_to_anthropic",
    "openai_to_anthropic_request",
    "restore_tool_name",
    "sanitize_tool_names",
    "strip_synthetic_thinking",
]
