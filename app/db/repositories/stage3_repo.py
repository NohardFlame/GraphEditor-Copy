"""Stage 3 repositories: resolved cards and baked objects."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.stage3 import Stage3ObjectBaked, Stage3ResolvedCard


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Stage3ResolvedCardRepo:
    """CRUD for stage3_resolved_cards."""

    def upsert(
        self,
        session: Session,
        document_id: str,
        run_id: str,
        canonical_claim_id: str,
        kind: str,
        resolved_json: str,
        *,
        raw_response: str | None = None,
        parse_status: str = "SUCCESS",
        prompt_version: str | None = None,
        model_id: str | None = None,
        embedding_model_id: str | None = None,
        evidence_refs: str | None = None,
    ) -> Stage3ResolvedCard:
        existing = session.execute(
            select(Stage3ResolvedCard).where(
                Stage3ResolvedCard.run_id == run_id,
                Stage3ResolvedCard.canonical_claim_id == canonical_claim_id,
                Stage3ResolvedCard.kind == kind,
            )
        ).scalar_one_or_none()
        if existing:
            existing.resolved_json = resolved_json
            existing.raw_response = raw_response
            existing.parse_status = parse_status
            existing.prompt_version = prompt_version
            existing.model_id = model_id
            existing.embedding_model_id = embedding_model_id
            existing.evidence_refs = evidence_refs
            session.flush()
            return existing
        row = Stage3ResolvedCard(
            id=str(uuid4()),
            document_id=document_id,
            run_id=run_id,
            canonical_claim_id=canonical_claim_id,
            kind=kind,
            resolved_json=resolved_json,
            raw_response=raw_response,
            parse_status=parse_status,
            prompt_version=prompt_version,
            model_id=model_id,
            embedding_model_id=embedding_model_id,
            evidence_refs=evidence_refs,
            created_at=_utc_now(),
        )
        session.add(row)
        session.flush()
        return row

    def get(
        self,
        session: Session,
        run_id: str,
        canonical_claim_id: str,
        kind: str,
    ) -> Stage3ResolvedCard | None:
        return session.execute(
            select(Stage3ResolvedCard).where(
                Stage3ResolvedCard.run_id == run_id,
                Stage3ResolvedCard.canonical_claim_id == canonical_claim_id,
                Stage3ResolvedCard.kind == kind,
            )
        ).scalar_one_or_none()

    def list_by_run(self, session: Session, run_id: str) -> list[Stage3ResolvedCard]:
        return list(
            session.execute(
                select(Stage3ResolvedCard).where(Stage3ResolvedCard.run_id == run_id)
            ).scalars().all()
        )


class Stage3ObjectBakedRepo:
    """CRUD for stage3_objects_baked; append state summaries in Pass C."""

    def upsert(
        self,
        session: Session,
        document_id: str,
        run_id: str,
        canonical_claim_id: str,
        object_json: str,
    ) -> Stage3ObjectBaked:
        existing = session.execute(
            select(Stage3ObjectBaked).where(
                Stage3ObjectBaked.run_id == run_id,
                Stage3ObjectBaked.canonical_claim_id == canonical_claim_id,
            )
        ).scalar_one_or_none()
        if existing:
            existing.object_json = object_json
            existing.updated_at = _utc_now()
            session.flush()
            return existing
        row = Stage3ObjectBaked(
            id=str(uuid4()),
            document_id=document_id,
            run_id=run_id,
            canonical_claim_id=canonical_claim_id,
            object_json=object_json,
            updated_at=_utc_now(),
        )
        session.add(row)
        session.flush()
        return row

    def get(
        self,
        session: Session,
        run_id: str,
        canonical_claim_id: str,
    ) -> Stage3ObjectBaked | None:
        return session.execute(
            select(Stage3ObjectBaked).where(
                Stage3ObjectBaked.run_id == run_id,
                Stage3ObjectBaked.canonical_claim_id == canonical_claim_id,
            )
        ).scalar_one_or_none()

    def list_by_run(self, session: Session, run_id: str) -> list[Stage3ObjectBaked]:
        return list(
            session.execute(
                select(Stage3ObjectBaked).where(Stage3ObjectBaked.run_id == run_id)
            ).scalars().all()
        )

    def append_state_to_object(
        self,
        session: Session,
        run_id: str,
        canonical_claim_id: str,
        state_summary: str,
    ) -> None:
        """Append a state summary to the object's baked states list."""
        row = self.get(session, run_id, canonical_claim_id)
        if not row:
            return
        try:
            data = json.loads(row.object_json) if isinstance(row.object_json, str) else row.object_json
        except (json.JSONDecodeError, TypeError):
            data = {}
        if "states" not in data:
            data["states"] = []
        if isinstance(data["states"], list):
            data["states"].append(state_summary)
        row.object_json = json.dumps(data)
        row.updated_at = _utc_now()
        session.flush()
