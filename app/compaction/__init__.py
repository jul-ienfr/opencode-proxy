"""app.compaction — compaction serveur (voie free/payante unifiée + natifs)."""

from app.compaction.classify import has_free_leg, is_free_class
from app.compaction.lean import lean_summary_body
from app.compaction.overflow import is_overflow
from app.compaction.plan import summarizer_plan
from app.compaction.pool import free_pool_request, summarizer_dispatch
from app.compaction.react import maybe_condense
from app.compaction.router import detect_compaction, plan_for_conversation
from app.compaction.shapes import is_compaction_shape, is_native_compaction
from app.compaction.streaming import (
    anthropic_sse_from_completion,
    chat_sse_from_completion,
    completion_text,
    final_has_nontext_blocks,
    is_tiny_result,
    should_buffer_compaction,
)
from app.compaction.summarizer import (
    build_summarizer_anthropic_body,
    build_summarizer_body,
    build_summarizer_for_api,
    build_summarizer_responses_body,
    build_summary_user_text,
    cap_summary_max_tokens,
    extract_previous_summary,
    run_summarizer,
    should_clamp_summary,
)
from app.compaction.transport import (
    normalize_free_responses,
    should_use_tunnel,
    summarizer_headers,
    summarizer_request,
)
from app.compaction.truncate import (
    build_checkpoint_input,
    build_checkpoint_summary,
    build_condensed_history,
    build_condensed_input,
    split_keep_recent,
)

__all__ = [
    "is_compaction_shape",
    "is_native_compaction",
    "detect_compaction",
    "plan_for_conversation",
    "has_free_leg",
    "is_free_class",
    "summarizer_plan",
    "free_pool_request",
    "summarizer_dispatch",
    "summarizer_headers",
    "summarizer_request",
    "should_buffer_compaction",
    "is_tiny_result",
    "final_has_nontext_blocks",
    "lean_summary_body",
    "completion_text",
    "chat_sse_from_completion",
    "anthropic_sse_from_completion",
    "normalize_free_responses",
    "should_use_tunnel",
    "is_overflow",
    "maybe_condense",
    "split_keep_recent",
    "build_checkpoint_summary",
    "build_condensed_history",
    "build_checkpoint_input",
    "build_condensed_input",
    "build_summary_user_text",
    "build_summarizer_body",
    "build_summarizer_anthropic_body",
    "build_summarizer_responses_body",
    "build_summarizer_for_api",
    "should_clamp_summary",
    "cap_summary_max_tokens",
    "extract_previous_summary",
    "run_summarizer",
]
