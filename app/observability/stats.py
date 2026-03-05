"""Run statistics helpers for extraction and canonicalization (Phase 6)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.extraction.big_extract_models import BigExtractResult


def compute_big_extract_stats(
    results: list[BigExtractResult],
    evidence_errors: list[str],
) -> dict[str, Any]:
    """Build stats dict for a BIG_LLM_EXTRACT run.

    Includes: frames_count, mentions_count_total, mentions_count_by_type,
    relations_count, evidence_valid_count, evidence_invalid_count.
    """
    frames_count = 0
    mentions_count_total = 0
    mentions_count_by_type: dict[str, int] = defaultdict(int)
    relations_count = 0
    for r in results:
        for frame in r.frames:
            frames_count += 1
            for m in frame.mentions:
                mentions_count_total += 1
                t = (m.type or "").upper()
                if t in ("ACTOR", "OBJECT", "ACTION", "STATE"):
                    mentions_count_by_type[t] += 1
            for _ in frame.relations:
                relations_count += 1
    evidence_invalid_count = len(evidence_errors)
    evidence_valid_count = max(0, mentions_count_total - evidence_invalid_count)
    return {
        "frames_count": frames_count,
        "mentions_count_total": mentions_count_total,
        "mentions_count_by_type": dict(mentions_count_by_type),
        "relations_count": relations_count,
        "evidence_valid_count": evidence_valid_count,
        "evidence_invalid_count": evidence_invalid_count,
    }


def compute_canonicalization_stats(
    mentions_processed: int,
    resolved_by_rule: int,
    resolved_by_llm: int,
    new_canonicals_created: int,
    llm_calls_count: int,
) -> dict[str, Any]:
    """Build stats dict for a canonicalization run (actors, objects, states, actions)."""
    return {
        "mentions_processed": mentions_processed,
        "resolved_by_rule": resolved_by_rule,
        "resolved_by_llm": resolved_by_llm,
        "new_canonicals_created": new_canonicals_created,
        "llm_calls_count": llm_calls_count,
    }
