"""app.compaction — compaction serveur (voie free/payante unifiée + natifs)."""

from app.compaction.classify import has_free_leg, is_free_class
from app.compaction.overflow import is_overflow
from app.compaction.plan import summarizer_plan
from app.compaction.react import maybe_condense
from app.compaction.router import detect_compaction, plan_for_conversation
from app.compaction.shapes import is_compaction_shape, is_native_compaction
from app.compaction.summarizer import (
    build_summarizer_anthropic_body,
    build_summarizer_body,
    build_summarizer_for_api,
    build_summarizer_responses_body,
    build_summary_user_text,
    extract_previous_summary,
    run_summarizer,
)
from app.compaction.transport import (
    normalize_free_responses,
    should_use_tunnel,
    summarizer_headers,
    summarizer_request,
)
from app.compaction.truncate import build_checkpoint_summary, build_condensed_history, split_keep_recent

__all__ = [
    "is_compaction_shape",
    "is_native_compaction",
    "detect_compaction",
    "plan_for_conversation",
    "has_free_leg",
    "is_free_class",
    "summarizer_plan",
    "summarizer_headers",
    "summarizer_request",
    "normalize_free_responses",
    "should_use_tunnel",
    "is_overflow",
    "maybe_condense",
    "split_keep_recent",
    "build_checkpoint_summary",
    "build_condensed_history",
    "build_summary_user_text",
    "build_summarizer_body",
    "build_summarizer_anthropic_body",
    "build_summarizer_responses_body",
    "build_summarizer_for_api",
    "extract_previous_summary",
    "run_summarizer",
]
