"""DTOs for canonical service (Phase 4)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CanonicalData:
    """Input for upsert_canonical_and_qdrant."""

    canonical_type: str
    name: str
    aliases: list[str] | None = None
    canonical_id: str | None = None
    created_run_id: str | None = None
    # When set (e.g. for ACTION action_sig), used as norm_name instead of norm(name)
    norm_name: str | None = None
    # Action-specific (stored in Qdrant payload only for MVP)
    actor_id: str | None = None
    object_id: str | None = None
    verb_norm: str | None = None


@dataclass
class Candidate:
    """One canonical candidate returned by get_canonical_candidates_for_mention."""

    canonical_id: str
    name: str
    norm_name: str
    aliases: list[str]
    score: float
    payload: dict[str, Any]
