"""Canonical and alias repository (Phase 4)."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonical.norm import norm
from app.db.models.extraction import Canonical, CanonicalAlias, ObjectState


def _state_norm_name(canonical_object_id: str, state_norm: str) -> str:
    """Unique norm_name for a STATE canonical: object scoped."""
    return f"{canonical_object_id}::{state_norm}"


class CanonicalRepo:
    """CRUD for canonicals, aliases, and object_states."""

    def get_by_id(self, session: Session, canonical_id: str) -> Canonical | None:
        """Return canonical by id or None."""
        return session.execute(
            select(Canonical).where(Canonical.id == canonical_id)
        ).scalar_one_or_none()

    def get_by_type_norm(
        self,
        session: Session,
        canonical_type: str,
        norm_name: str,
    ) -> Canonical | None:
        """Return canonical by (canonical_type, norm_name) or None."""
        return session.execute(
            select(Canonical).where(
                Canonical.canonical_type == canonical_type,
                Canonical.norm_name == norm_name,
            )
        ).scalar_one_or_none()

    def upsert_canonical(
        self,
        session: Session,
        canonical_type: str,
        name: str,
        norm_name: str,
        *,
        created_run_id: str | None = None,
        canonical_id: str | None = None,
    ) -> Canonical:
        """Insert or update a canonical. Lookup by canonical_id if provided, else by (type, norm_name)."""
        if canonical_id:
            existing = self.get_by_id(session, canonical_id)
        else:
            existing = self.get_by_type_norm(session, canonical_type, norm_name)
        if existing:
            existing.name = name
            if created_run_id is not None:
                existing.created_run_id = created_run_id
            session.flush()
            return existing
        canonical = Canonical(
            id=canonical_id or str(uuid4()),
            canonical_type=canonical_type,
            name=name,
            norm_name=norm_name,
            created_run_id=created_run_id,
        )
        session.add(canonical)
        session.flush()
        return canonical

    def upsert_aliases(
        self,
        session: Session,
        canonical_id: str,
        alias_texts: list[str],
    ) -> None:
        """Ensure each alias exists for the canonical. Idempotent by (canonical_id, alias_norm)."""
        if not alias_texts:
            return
        existing = session.execute(
            select(CanonicalAlias.alias_norm).where(CanonicalAlias.canonical_id == canonical_id)
        ).scalars().all()
        existing_norms = set(existing)
        for text in alias_texts:
            alias_norm = norm(text)
            if not alias_norm or alias_norm in existing_norms:
                continue
            session.add(
                CanonicalAlias(
                    canonical_id=canonical_id,
                    alias_text=text.strip(),
                    alias_norm=alias_norm,
                )
            )
            existing_norms.add(alias_norm)
        session.flush()

    def get_or_create_object_state(
        self,
        session: Session,
        canonical_object_id: str,
        state_name: str,
        state_norm: str,
    ) -> ObjectState:
        """Get existing object state by (canonical_object_id, state_norm) or create.

        When creating, also ensures a Canonical row exists with id=ObjectState.id and
        canonical_type=STATE so mention_to_canonical.canonical_id can point to the state.
        """
        existing = session.execute(
            select(ObjectState).where(
                ObjectState.canonical_object_id == canonical_object_id,
                ObjectState.state_norm == state_norm,
            )
        ).scalar_one_or_none()
        if existing:
            existing.state_name = state_name
            session.flush()
            self._ensure_state_canonical(session, existing)
            return existing
        state_id = str(uuid4())
        state = ObjectState(
            id=state_id,
            canonical_object_id=canonical_object_id,
            state_name=state_name,
            state_norm=state_norm,
        )
        session.add(state)
        session.flush()
        norm_name = _state_norm_name(canonical_object_id, state_norm)
        canonical = self.get_by_id(session, state_id)
        if not canonical:
            canonical = Canonical(
                id=state_id,
                canonical_type="STATE",
                name=state_name,
                norm_name=norm_name,
            )
            session.add(canonical)
            session.flush()
        return state

    def find_object_state(
        self,
        session: Session,
        canonical_object_id: str,
        state_norm: str,
    ) -> ObjectState | None:
        """Return object state by (canonical_object_id, state_norm) or None.
        The state's id is the canonical id for mention_to_canonical (STATE canonical row shares it).
        """
        return session.execute(
            select(ObjectState).where(
                ObjectState.canonical_object_id == canonical_object_id,
                ObjectState.state_norm == state_norm,
            )
        ).scalar_one_or_none()

    def _ensure_state_canonical(self, session: Session, state: ObjectState) -> None:
        """Ensure a Canonical row exists for this ObjectState (for mention_to_canonical)."""
        if self.get_by_id(session, state.id) is not None:
            return
        norm_name = _state_norm_name(state.canonical_object_id, state.state_norm)
        canonical = Canonical(
            id=state.id,
            canonical_type="STATE",
            name=state.state_name,
            norm_name=norm_name,
        )
        session.add(canonical)
        session.flush()
