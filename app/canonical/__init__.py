"""Canonical registries and Qdrant integration (Phase 4) and canonicalization runners (Phase 5)."""

from app.canonical.embedding_text import (
    build_action_embedding_text,
    build_actor_embedding_text,
    build_object_embedding_text,
    build_state_embedding_text,
)
from app.canonical.models import CanonicalData, Candidate
from app.canonical.norm import norm
from app.canonical.bootstrap import bootstrap_canonical_collections
from app.canonical.service import (
    build_mention_card_text,
    embed_mention_card,
    get_canonical_candidates_for_mention,
    record_canonical_merge,
    upsert_canonical_and_qdrant,
    upsert_state_and_qdrant,
)
from app.canonical.deterministic import (
    build_action_sig,
    build_state_dedupe_key,
    find_canonical_by_action_sig,
    find_canonical_by_dedupe_key,
    find_object_state,
)
from app.canonical.comparator import DecisionResult, compare_mention_with_candidate
from app.canonical.tournament import CreateNewCanonical, LinkToExisting, run_tournament
from app.canonical.runners import (
    canonicalize_actors,
    canonicalize_objects,
    canonicalize_object_states,
    canonicalize_actions,
    run_canonicalization_for_document,
)

__all__ = [
    "norm",
    "build_actor_embedding_text",
    "build_object_embedding_text",
    "build_action_embedding_text",
    "build_state_embedding_text",
    "CanonicalData",
    "Candidate",
    "build_mention_card_text",
    "embed_mention_card",
    "get_canonical_candidates_for_mention",
    "record_canonical_merge",
    "upsert_canonical_and_qdrant",
    "upsert_state_and_qdrant",
    "bootstrap_canonical_collections",
    "build_action_sig",
    "build_state_dedupe_key",
    "find_canonical_by_action_sig",
    "find_canonical_by_dedupe_key",
    "find_object_state",
    "DecisionResult",
    "compare_mention_with_candidate",
    "CreateNewCanonical",
    "LinkToExisting",
    "run_tournament",
    "canonicalize_actors",
    "canonicalize_objects",
    "canonicalize_object_states",
    "canonicalize_actions",
    "run_canonicalization_for_document",
]
