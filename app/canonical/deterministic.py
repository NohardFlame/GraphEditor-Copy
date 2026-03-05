"""Deterministic canonical lookup by dedupe_key or action_sig (Phase 5)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.extraction import Canonical, CanonicalAlias, ObjectState
from app.db.repositories.canonical_repo import CanonicalRepo


def build_action_sig(
    canonical_actor_id: str,
    verb_norm: str,
    canonical_object_id: str,
) -> str:
    """Build deterministic action key for lookup and storage.
    Store ACTION canonicals with norm_name = action_sig for deterministic match.
    """
    return f"action::{canonical_actor_id}::{verb_norm}::{canonical_object_id}"


def build_state_dedupe_key(canonical_object_id: str, state_norm: str) -> str:
    """Build resolved dedupe key for STATE: state::<canonical_object_id>::<state_norm>."""
    return f"state::{canonical_object_id}::{state_norm}"


def _canonical_by_alias_norm(
    session: Session,
    canonical_type: str,
    norm_name: str,
) -> Canonical | None:
    """Find canonical by alias_norm and type (join aliases -> canonicals)."""
    stmt = (
        select(Canonical)
        .join(CanonicalAlias, Canonical.id == CanonicalAlias.canonical_id)
        .where(
            CanonicalAlias.alias_norm == norm_name,
            Canonical.canonical_type == canonical_type,
        )
    )
    return session.execute(stmt).scalar_one_or_none()


def find_canonical_by_dedupe_key(
    session: Session,
    mention_type: str,
    dedupe_key: str,
) -> Canonical | None:
    """Resolve canonical by dedupe_key for ACTOR, OBJECT, ACTION, or STATE.
    ACTION key format: action::<canonical_actor_id>::<verb_norm>::<canonical_object_id>.
    STATE key format: state::<canonical_object_id>::<state_norm>.
    """
    repo = CanonicalRepo()
    t = (mention_type or "").upper()
    if t == "ACTOR":
        if not dedupe_key.startswith("actor::"):
            return None
        norm_name = dedupe_key[7:].strip()
        if not norm_name:
            return None
        c = repo.get_by_type_norm(session, "ACTOR", norm_name)
        if c is not None:
            return c
        return _canonical_by_alias_norm(session, "ACTOR", norm_name)
    if t == "OBJECT":
        if not dedupe_key.startswith("object::"):
            return None
        norm_name = dedupe_key[8:].strip()
        if not norm_name:
            return None
        c = repo.get_by_type_norm(session, "OBJECT", norm_name)
        if c is not None:
            return c
        return _canonical_by_alias_norm(session, "OBJECT", norm_name)
    if t == "ACTION":
        if not dedupe_key.startswith("action::"):
            return None
        parts = dedupe_key.split("::", 3)
        if len(parts) != 4 or not parts[1] or not parts[2] or not parts[3]:
            return None
        return find_canonical_by_action_sig(session, dedupe_key)
    if t == "STATE":
        if not dedupe_key.startswith("state::"):
            return None
        parts = dedupe_key.split("::", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return None
        state_row = find_object_state(session, parts[1], parts[2])
        if state_row is None:
            return None
        return repo.get_by_id(session, state_row.id)
    return None


def find_canonical_by_action_sig(session: Session, action_sig: str) -> Canonical | None:
    """Look up ACTION canonical by action_sig (norm_name)."""
    repo = CanonicalRepo()
    return repo.get_by_type_norm(session, "ACTION", action_sig)


def find_object_state(
    session: Session,
    canonical_object_id: str,
    state_norm: str,
) -> ObjectState | None:
    """Return object state by (canonical_object_id, state_norm).
    Use state.id as canonical_id for mention_to_canonical (STATE canonical row has same id).
    """
    repo = CanonicalRepo()
    return repo.find_object_state(session, canonical_object_id, state_norm)
