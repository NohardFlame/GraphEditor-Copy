"""Stage 1 repositories: run, cache (chunk_extractions), llm_calls, claims."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models.chunk import Chunk
from app.db.models.claim import Claim, ClaimEvidence, LlmCall
from app.db.models.document import Document
from app.db.models.pipeline_run import ChunkExtraction, ChunkRun, PipelineRun
from app.db.models.source import Source, SourceVersion


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_workspace_id_for_document(session: Session, document_id: str) -> str | None:
    """Resolve document_id -> workspace_id via source_version -> source."""
    doc = session.get(Document, document_id)
    if not doc:
        return None
    sv = session.get(SourceVersion, doc.source_version_id)
    if not sv:
        return None
    source = session.get(Source, sv.source_id)
    return source.workspace_id if source else None


class Stage1RunRepo:
    """Create and finalize Stage 1 pipeline runs."""

    def create(
        self,
        session: Session,
        workspace_id: str,
        document_id: str,
        config: dict,
        *,
        prompt_version: str,
        extractor_version: str,
        model_id: str,
        run_kind: str = "STAGE1_EXTRACT",
    ) -> PipelineRun:
        run = PipelineRun(
            id=str(uuid4()),
            workspace_id=workspace_id,
            document_id=document_id,
            status="RUNNING",
            run_kind=run_kind,
            config_json=json.dumps(config),
            stats_json=None,
            prompt_name=prompt_version,
            llm_profile=model_id,
            started_at=_utc_now(),
        )
        session.add(run)
        session.flush()
        return run

    def finalize(
        self,
        session: Session,
        run_id: str,
        status: str,
        stats: dict,
        *,
        error_summary: str | None = None,
    ) -> None:
        run = session.get(PipelineRun, run_id)
        if not run:
            return
        run.status = status
        run.finished_at = _utc_now()
        run.stats_json = json.dumps(stats)
        if error_summary is not None:
            run.error_summary = error_summary
        session.flush()

    def list_runs(
        self,
        session: Session,
        *,
        document_id: str | None = None,
        run_kind: str | None = "STAGE1_EXTRACT",
        limit: int = 20,
    ) -> list[PipelineRun]:
        """List pipeline runs, newest first. Optionally filter by document_id and run_kind."""
        q = select(PipelineRun).order_by(PipelineRun.started_at.desc())
        if document_id is not None:
            q = q.where(PipelineRun.document_id == document_id)
        if run_kind is not None:
            q = q.where(PipelineRun.run_kind == run_kind)
        q = q.limit(limit)
        return list(session.execute(q).scalars().all())


class Stage1ChunkRunRepo:
    """Ensure chunk_runs for a run and update status."""

    def bulk_ensure(self, session: Session, run_id: str, chunk_ids: list[str]) -> None:
        for cid in chunk_ids:
            existing = session.execute(
                select(ChunkRun).where(ChunkRun.run_id == run_id, ChunkRun.chunk_id == cid)
            ).scalar_one_or_none()
            if not existing:
                session.add(
                    ChunkRun(
                        id=str(uuid4()),
                        run_id=run_id,
                        chunk_id=cid,
                        status="PENDING",
                        attempts=0,
                    )
                )
        session.flush()

    def mark_cached(
        self,
        session: Session,
        run_id: str,
        chunk_id: str,
        chunk_extraction_id: str,
    ) -> None:
        row = session.execute(
            select(ChunkRun).where(ChunkRun.run_id == run_id, ChunkRun.chunk_id == chunk_id)
        ).scalar_one_or_none()
        if row:
            row.status = "CACHED"
            row.cache_status = "CACHED"
            row.chunk_extraction_id = chunk_extraction_id
            session.flush()

    def mark_success(
        self,
        session: Session,
        run_id: str,
        chunk_id: str,
        chunk_extraction_id: str,
        *,
        latency_ms: int | None = None,
        success_with_warnings: bool = False,
    ) -> None:
        row = session.execute(
            select(ChunkRun).where(ChunkRun.run_id == run_id, ChunkRun.chunk_id == chunk_id)
        ).scalar_one_or_none()
        if row:
            row.status = "SUCCESS_WITH_WARNINGS" if success_with_warnings else "SUCCESS"
            row.cache_status = "FRESH"
            row.chunk_extraction_id = chunk_extraction_id
            if latency_ms is not None:
                row.latency_ms = latency_ms
            session.flush()

    def mark_failed(
        self,
        session: Session,
        run_id: str,
        chunk_id: str,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        row = session.execute(
            select(ChunkRun).where(ChunkRun.run_id == run_id, ChunkRun.chunk_id == chunk_id)
        ).scalar_one_or_none()
        if row:
            row.status = "FAILED"
            row.error_type = error_type
            row.error_message = (error_message or "")[:4096]
            row.attempts = (row.attempts or 0) + 1
            session.flush()


class Stage1CacheRepo:
    """Chunk extraction cache: lookup by (chunk_id, signature_hash), insert on miss."""

    def get_successful(
        self,
        session: Session,
        chunk_id: str,
        signature_hash: str,
    ) -> ChunkExtraction | None:
        row = session.execute(
            select(ChunkExtraction).where(
                ChunkExtraction.chunk_id == chunk_id,
                ChunkExtraction.signature_hash == signature_hash,
                ChunkExtraction.extraction_status == "SUCCESS",
            )
        ).scalar_one_or_none()
        return row

    def insert(
        self,
        session: Session,
        chunk_id: str,
        run_id: str,
        signature_hash: str,
        chunk_content_hash: str,
        prompt_version: str,
        extractor_version: str,
        model_id: str,
        *,
        status: str = "SUCCESS",
        produced_run_id: str | None = None,
        llm_call_id: str | None = None,
        raw_text: str | None = None,
        parsed_json: str | None = None,
        usage_json: str | None = None,
        validation_error: str | None = None,
    ) -> ChunkExtraction:
        ext = ChunkExtraction(
            id=str(uuid4()),
            chunk_id=chunk_id,
            run_id=run_id,
            prompt_name=prompt_version,
            model=model_id,
            signature_hash=signature_hash,
            chunk_content_hash=chunk_content_hash,
            produced_run_id=produced_run_id or run_id,
            llm_call_id=llm_call_id,
            extraction_status=status,
            raw_text=raw_text,
            parsed_json=parsed_json,
            usage_json=usage_json,
            validation_error=validation_error,
        )
        session.add(ext)
        session.flush()
        return ext


class Stage1LlmCallRepo:
    """Persist LLM call audit records."""

    def create(
        self,
        session: Session,
        run_id: str,
        *,
        chunk_id: str | None = None,
        signature_hash: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        request_json: str | None = None,
        response_text: str | None = None,
        response_json: str | None = None,
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        status: str = "SUCCESS",
        error_message: str | None = None,
    ) -> LlmCall:
        row = LlmCall(
            id=str(uuid4()),
            run_id=run_id,
            chunk_id=chunk_id,
            signature_hash=signature_hash,
            provider=provider,
            model=model,
            request_json=request_json,
            response_text=response_text,
            response_json=response_json,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=status,
            error_message=error_message,
            created_at=_utc_now(),
        )
        session.add(row)
        session.flush()
        return row


class Stage1ClaimRepo:
    """Persist claims and evidence."""

    def create_claim(
        self,
        session: Session,
        run_id: str,
        chunk_id: str,
        chunk_extraction_id: str | None,
        claim_type: str,
        value_json: str,
        epistemic_tag: str,
        *,
        subject_type: str | None = None,
        subject_text: str | None = None,
        predicate: str | None = None,
        rule_id: str | None = None,
        confidence: float | None = None,
    ) -> Claim:
        claim = Claim(
            id=str(uuid4()),
            run_id=run_id,
            chunk_id=chunk_id,
            chunk_extraction_id=chunk_extraction_id,
            claim_type=claim_type,
            value_json=value_json,
            epistemic_tag=epistemic_tag,
            subject_type=subject_type,
            subject_text=subject_text,
            predicate=predicate,
            rule_id=rule_id,
            confidence=confidence,
            review_status="UNREVIEWED",
            embedding_status="PENDING",
            created_at=_utc_now(),
        )
        session.add(claim)
        session.flush()
        return claim

    def create_evidence(
        self,
        session: Session,
        claim_id: str,
        chunk_id: str,
        snippet_text: str,
        *,
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> ClaimEvidence:
        ev = ClaimEvidence(
            id=str(uuid4()),
            claim_id=claim_id,
            chunk_id=chunk_id,
            snippet_text=snippet_text[:65535],
            char_start=char_start,
            char_end=char_end,
            created_at=_utc_now(),
        )
        session.add(ev)
        session.flush()
        return ev

    def list_claims_by_run(self, session: Session, run_id: str) -> list[Claim]:
        rows = session.execute(select(Claim).where(Claim.run_id == run_id)).scalars().all()
        return list(rows)

    def list_claims_pending_embedding(
        self,
        session: Session,
        run_id: str | None = None,
    ) -> list[Claim]:
        q = select(Claim).where(Claim.embedding_status != "EMBEDDED")
        if run_id:
            q = q.where(Claim.run_id == run_id)
        rows = session.execute(q).scalars().all()
        return list(rows)

    # --- Stage-2 helpers ---

    def list_claims_unreviewed_by_type(
        self,
        session: Session,
        run_id: str,
        claim_type: str,
        *,
        embedded_only: bool = False,
    ) -> list[Claim]:
        """List claims with review_status=UNREVIEWED and given claim_type, ordered by created_at.
        If embedded_only is True, only return claims with embedding_status == EMBEDDED."""
        q = (
            select(Claim)
            .where(
                Claim.run_id == run_id,
                Claim.review_status == "UNREVIEWED",
                Claim.claim_type == claim_type,
            )
            .order_by(Claim.created_at.asc())
        )
        if embedded_only:
            q = q.where(Claim.embedding_status == "EMBEDDED")
        rows = session.execute(q).scalars().all()
        return list(rows)

    def list_accepted_claim_ids(
        self,
        session: Session,
        run_id: str,
        claim_type: str,
    ) -> list[str]:
        """List claim ids with review_status=ACCEPTED and given claim_type for the run (for Stage-2 closest canonical)."""
        q = select(Claim.id).where(
            Claim.run_id == run_id,
            Claim.review_status == "ACCEPTED",
            Claim.claim_type == claim_type,
        )
        # .scalars().all() returns list of scalar values (the id column), not list of Row
        rows = session.execute(q).scalars().all()
        return [str(r) for r in rows]

    def get_claim_with_evidence(self, session: Session, claim_id: str) -> Claim | None:
        """Load claim by id with evidence eager-loaded."""
        q = (
            select(Claim)
            .where(Claim.id == claim_id)
            .options(joinedload(Claim.evidence))
        )
        return session.execute(q).unique().scalar_one_or_none()

    def update_review_status(
        self,
        session: Session,
        claim_id: str,
        review_status: str,
        *,
        superseded_by_id: str | None = None,
    ) -> bool:
        """Update review_status and optionally superseded_by_id for a claim. Returns True if row was updated."""
        claim = session.get(Claim, claim_id)
        if not claim:
            return False
        claim.review_status = review_status
        if superseded_by_id is not None:
            claim.superseded_by_id = superseded_by_id
        session.flush()
        return True

    def merge_value_json_stage2(
        self,
        session: Session,
        claim_id: str,
        stage2_data: dict,
    ) -> bool:
        """Merge stage2_data into value_json.stage2 for the claim. Returns True if updated."""
        claim = session.get(Claim, claim_id)
        if not claim:
            return False
        try:
            value = json.loads(claim.value_json) if isinstance(claim.value_json, str) else dict(claim.value_json)
        except (json.JSONDecodeError, TypeError):
            value = {}
        if "stage2" not in value:
            value["stage2"] = {}
        value["stage2"].update(stage2_data)
        claim.value_json = json.dumps(value)
        session.flush()
        return True

    def resolve_canonical_claim_id(self, session: Session, claim_id: str) -> str | None:
        """Follow superseded_by_id chain to terminal (ACCEPTED) claim. Returns canonical claim_id or None if not found."""
        seen: set[str] = set()
        current_id = claim_id
        while current_id:
            if current_id in seen:
                return None  # cycle
            seen.add(current_id)
            claim = session.get(Claim, current_id)
            if not claim:
                return None
            if claim.review_status == "ACCEPTED":
                return current_id
            current_id = claim.superseded_by_id
        return None
