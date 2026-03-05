"""Tournament logic: compare mention with top candidates, return LINK or CREATE_NEW (Phase 5)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from app.canonical.comparator import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    DECISION_DIFFERENT,
    DECISION_SAME,
    DECISION_UNSURE,
    DecisionResult,
)
from app.canonical.models import Candidate

if TYPE_CHECKING:
    from app.db.models.extraction import Mention


@dataclass
class LinkToExisting:
    """Tournament outcome: link mention group to this canonical."""

    canonical_id: str


@dataclass
class CreateNewCanonical:
    """Tournament outcome: no candidate matched; create new canonical and link."""

    pass


TournamentOutcome = LinkToExisting | CreateNewCanonical

# Confidence ordering for threshold check (higher = more confident)
_CONFIDENCE_ORDER = {CONFIDENCE_LOW: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_HIGH: 2}


def _confidence_meets(confidence: str, threshold: str) -> bool:
    """True if confidence is at or above threshold."""
    return _CONFIDENCE_ORDER.get(confidence, -1) >= _CONFIDENCE_ORDER.get(threshold, 0)


async def run_tournament(
    representative_mention: "Mention",
    candidates: list[Candidate],
    *,
    compare_fn: Callable[["Mention", Candidate], Awaitable[DecisionResult]],
    top_n: int = 3,
    confidence_threshold: str = CONFIDENCE_MEDIUM,
) -> TournamentOutcome:
    """Compare representative mention with top candidates; return LINK_TO_EXISTING or CREATE_NEW_CANONICAL.

    - Takes top_n candidates (e.g. 3), calls compare_fn(mention, candidate) for each in order.
    - On first SAME with confidence >= confidence_threshold, return LinkToExisting(canonical_id).
    - On DIFFERENT, continue to next candidate.
    - On UNSURE, treat as soft negative (continue).
    - If none yield SAME above threshold, return CreateNewCanonical().
    """
    for candidate in candidates[:top_n]:
        result = await compare_fn(representative_mention, candidate)
        if result.decision == DECISION_SAME and _confidence_meets(result.confidence, confidence_threshold):
            return LinkToExisting(canonical_id=candidate.canonical_id)
        if result.decision == DECISION_DIFFERENT:
            continue
        # UNSURE: continue to next candidate
    return CreateNewCanonical()
