"""Observability and replay (Phase 6): stats, debug views, replay hooks."""

from app.observability.debug_views import (
    canonical_view,
    chunk_view,
    doc_debug,
    mention_view,
)
from app.observability.prompt_registry import get_prompt_template
from app.observability.stats import (
    compute_big_extract_stats,
    compute_canonicalization_stats,
)

__all__ = [
    "canonical_view",
    "chunk_view",
    "compute_big_extract_stats",
    "compute_canonicalization_stats",
    "doc_debug",
    "get_prompt_template",
    "mention_view",
]
