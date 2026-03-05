"""Phase 6 tests: stats, debug views, replay (rerun_big_extract, rerun_canonicalization)."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Chunk as ChunkORM,
    Document,
    ExtractionRun,
    Frame,
    Mention,
    MentionEvidence,
    Relation,
    Source,
    SourceVersion,
    Workspace,
)
from app.db.models.claim import LlmCall
from app.db.models.extraction import (
    Canonical,
    CanonicalAlias,
    MentionToCanonical,
    ObjectState,
)
from app.extraction.big_extract_runner import (
    BIG_LLM_EXTRACT,
    rerun_big_extract,
    run_big_llm_extract,
)
from app.observability.debug_views import (
    canonical_view,
    chunk_view,
    doc_debug,
    mention_view,
)
from app.observability.prompt_registry import get_prompt_template
from app.observability.stats import compute_big_extract_stats


@pytest.fixture
def observability_session():
    """Session with workspace, document, chunks, one extraction run, frames, mentions."""
    from sqlalchemy import create_engine
    engine = create_engine("sqlite:///:memory:")
    tables = [
        Workspace.__table__,
        Source.__table__,
        SourceVersion.__table__,
        Document.__table__,
        ChunkORM.__table__,
        ExtractionRun.__table__,
        Frame.__table__,
        Mention.__table__,
        MentionEvidence.__table__,
        Relation.__table__,
        LlmCall.__table__,
        Canonical.__table__,
        CanonicalAlias.__table__,
        ObjectState.__table__,
        MentionToCanonical.__table__,
    ]
    for t in tables:
        if t is not None:
            t.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    sess = Session()
    w = Workspace(id="w-obs", name="observability_test")
    sess.add(w)
    s = Source(id="s-obs", workspace_id="w-obs", source_type="file")
    sess.add(s)
    from datetime import datetime, timezone
    v = SourceVersion(
        id="v-obs",
        source_id="s-obs",
        content_sha256="c" * 64,
        storage_uri="file:///tmp/obs",
        ingested_at=datetime.now(timezone.utc),
    )
    sess.add(v)
    d = Document(
        id="doc-obs",
        source_version_id="v-obs",
        extractor="docling",
        extractor_version="1",
        structure_json_uri="file:///doc/s.json",
        plain_text_uri="file:///doc/p.txt",
    )
    sess.add(d)
    sess.flush()
    for i in range(2):
        ch = ChunkORM(
            id=str(uuid4()),
            document_id="doc-obs",
            chunk_index=i,
            chunk_hash=f"h{i}",
            text=f"Chunk {i} text.",
            meta_json=json.dumps({"section_path": f"1/{i}"}),
        )
        sess.add(ch)
    sess.flush()
    run = ExtractionRun(
        id=str(uuid4()),
        workspace_id="w-obs",
        document_id="doc-obs",
        run_kind=BIG_LLM_EXTRACT,
        prompt_version="big_extract_v1",
        model_id="test-model",
        status="SUCCEEDED",
        input_hash="abc123",
        stats_json=json.dumps({
            "frames_count": 1,
            "mentions_count_total": 1,
            "mentions_count_by_type": {"ACTOR": 1},
            "relations_count": 0,
            "evidence_valid_count": 1,
            "evidence_invalid_count": 0,
        }),
    )
    sess.add(run)
    sess.flush()
    chunks = list(sess.execute(select(ChunkORM).where(ChunkORM.document_id == "doc-obs")).scalars().all())
    chunk_id = chunks[0].id
    frame = Frame(
        id=str(uuid4()),
        document_id="doc-obs",
        chunk_id=chunk_id,
        frame_index=0,
        frame_text="Chunk 0 text.",
        section_path="1/0",
    )
    sess.add(frame)
    sess.flush()
    mention = Mention(
        id=str(uuid4()),
        frame_id=frame.id,
        document_id="doc-obs",
        chunk_id=chunk_id,
        type="ACTOR",
        fields_json='{"name": "User"}',
        epistemic="EXPLICIT",
        confidence=0.9,
        created_run_id=run.id,
    )
    sess.add(mention)
    sess.flush()
    ev = MentionEvidence(
        id=str(uuid4()),
        mention_id=mention.id,
        snippet_text="Chunk 0",
        validation_status="VALID",
    )
    sess.add(ev)
    sess.commit()
    return sess


def test_compute_big_extract_stats():
    """compute_big_extract_stats returns expected keys and counts."""
    from app.extraction.big_extract_models import BigExtractResult, EvidenceResult, FrameResult, MentionResult
    results = [
        BigExtractResult(
            doc_id="d1",
            prompt_version="v1",
            frames=[
                FrameResult(
                    frame_id="f1",
                    chunk_id="c1",
                    frame_index=0,
                    frame_text="Hi",
                    mentions=[
                        MentionResult(
                            mention_id="m1",
                            type="ACTOR",
                            fields={"name": "User"},
                            epistemic="EXPLICIT",
                            evidence=EvidenceResult(snippet="Hi", char_start=0, char_end=2),
                            confidence=0.9,
                        ),
                        MentionResult(
                            mention_id="m2",
                            type="OBJECT",
                            fields={"name": "Doc"},
                            epistemic="EXPLICIT",
                            evidence=EvidenceResult(snippet="Doc", char_start=0, char_end=3),
                            confidence=0.8,
                        ),
                    ],
                    relations=[],
                ),
            ],
            references=[],
            errors=[],
        ),
    ]
    stats = compute_big_extract_stats(results, [])
    assert stats["frames_count"] == 1
    assert stats["mentions_count_total"] == 2
    assert stats["mentions_count_by_type"]["ACTOR"] == 1
    assert stats["mentions_count_by_type"]["OBJECT"] == 1
    assert stats["relations_count"] == 0
    assert stats["evidence_valid_count"] == 2
    assert stats["evidence_invalid_count"] == 0

    stats2 = compute_big_extract_stats(results, ["err1"])
    assert stats2["evidence_invalid_count"] == 1
    assert stats2["evidence_valid_count"] == 1


def test_doc_debug(observability_session):
    """doc_debug returns document meta and runs with stats."""
    out = doc_debug("doc-obs", observability_session)
    assert "error" not in out or out.get("error") != "document_not_found"
    assert out["doc_id"] == "doc-obs"
    assert "runs" in out
    assert len(out["runs"]) >= 1
    run_summary = out["runs"][0]
    assert run_summary["run_kind"] == BIG_LLM_EXTRACT
    assert run_summary["status"] == "SUCCEEDED"
    assert run_summary["prompt_version"] == "big_extract_v1"
    assert "stats" in run_summary
    assert run_summary["stats"].get("frames_count") == 1


def test_doc_debug_not_found(observability_session):
    """doc_debug returns error for unknown doc_id."""
    out = doc_debug("nonexistent-doc", observability_session)
    assert out.get("error") == "document_not_found"


def test_chunk_view(observability_session):
    """chunk_view returns chunk text, frames, mentions."""
    out = chunk_view("doc-obs", 0, observability_session)
    assert "error" not in out or out.get("error") != "chunk_not_found"
    assert out["doc_id"] == "doc-obs"
    assert out["chunk_index"] == 0
    assert "text_preview" in out
    assert "frames" in out
    assert len(out["frames"]) >= 1
    assert len(out["frames"][0]["mentions"]) >= 1


def test_mention_view(observability_session):
    """mention_view returns mention details and context."""
    mentions = list(observability_session.execute(
        select(Mention).where(Mention.document_id == "doc-obs")
    ).scalars().all())
    assert len(mentions) >= 1
    mention_id = mentions[0].id
    out = mention_view(mention_id, observability_session)
    assert out.get("error") != "mention_not_found"
    assert out["mention_id"] == mention_id
    assert out["type"] == "ACTOR"
    assert "evidence" in out
    assert "frame_context" in out


def test_canonical_view(observability_session):
    """canonical_view returns canonical details and mapped mentions (or not_found)."""
    out = canonical_view("nonexistent-canonical-id", observability_session)
    assert out.get("error") == "canonical_not_found"

    # Create a canonical and a mapping
    can = Canonical(
        id=str(uuid4()),
        canonical_type="ACTOR",
        name="User",
        norm_name="user",
        created_run_id=None,
    )
    observability_session.add(can)
    observability_session.flush()
    mention = observability_session.execute(
        select(Mention).where(Mention.document_id == "doc-obs")
    ).scalars().first()
    assert mention is not None
    mtc = MentionToCanonical(
        mention_id=mention.id,
        canonical_id=can.id,
        decision="ACCEPT",
        decided_by="RULE",
        run_id=None,
    )
    observability_session.add(mtc)
    observability_session.commit()

    out2 = canonical_view(can.id, observability_session)
    assert out2.get("error") != "canonical_not_found"
    assert out2["canonical_id"] == can.id
    assert out2["name"] == "User"
    assert len(out2["mentions_mapped"]) >= 1


def test_get_prompt_template():
    """get_prompt_template returns text for known versions."""
    assert get_prompt_template("big_extract_v1") is not None
    assert get_prompt_template("local_compare_v1") is not None
    assert get_prompt_template("unknown_version") is None


def test_rerun_big_extract_returns_existing_run_when_not_force(big_extract_session):
    """rerun_big_extract with force=False returns existing SUCCEEDED run when same input_hash exists."""
    from app.extraction.big_extract_runner import run_big_llm_extract
    from tests.test_big_extract_pipeline import _chunk_ids, _canned_big_extract_json
    from app.db.models import Chunk as ChunkORM

    chunk_ids = _chunk_ids(big_extract_session, "doc-big")
    chunk_id = chunk_ids[0]
    chunk_row = big_extract_session.get(ChunkORM, chunk_id)
    canned = _canned_big_extract_json("doc-big", chunk_id, chunk_row.text or "")

    class MockClient:
        def complete(self, system_prompt: str, user_prompt: str, model_id: str) -> str:
            return json.dumps(canned)

    first_id = run_big_llm_extract(
        "doc-big",
        "test-model",
        session=big_extract_session,
        llm_client=MockClient(),
    )
    second_id = rerun_big_extract(
        "doc-big",
        "test-model",
        session=big_extract_session,
        llm_client=MockClient(),
        force=False,
    )
    assert first_id == second_id
    runs = list(big_extract_session.execute(
        select(ExtractionRun).where(
            ExtractionRun.document_id == "doc-big",
            ExtractionRun.run_kind == BIG_LLM_EXTRACT,
        )
    ).scalars().all())
    assert len(runs) == 1


def test_rerun_big_extract_creates_new_run_when_force(big_extract_session):
    """rerun_big_extract with force=True creates a new run."""
    from app.extraction.big_extract_runner import run_big_llm_extract
    from tests.test_big_extract_pipeline import _chunk_ids, _canned_big_extract_json
    from app.db.models import Chunk as ChunkORM

    chunk_ids = _chunk_ids(big_extract_session, "doc-big")
    chunk_id = chunk_ids[0]
    chunk_row = big_extract_session.get(ChunkORM, chunk_id)
    canned = _canned_big_extract_json("doc-big", chunk_id, chunk_row.text or "")

    class MockClient:
        def complete(self, system_prompt: str, user_prompt: str, model_id: str) -> str:
            return json.dumps(canned)

    first_id = run_big_llm_extract(
        "doc-big",
        "test-model",
        session=big_extract_session,
        llm_client=MockClient(),
    )
    second_id = rerun_big_extract(
        "doc-big",
        "test-model",
        session=big_extract_session,
        llm_client=MockClient(),
        force=True,
    )
    assert first_id != second_id
    runs = list(big_extract_session.execute(
        select(ExtractionRun).where(
            ExtractionRun.document_id == "doc-big",
            ExtractionRun.run_kind == BIG_LLM_EXTRACT,
        )
    ).scalars().all())
    assert len(runs) == 2
