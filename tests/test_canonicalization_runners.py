"""Phase 5 canonicalization runners: deterministic, tournament, ordering, state scoping."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.canonical.comparator import DECISION_DIFFERENT, DECISION_SAME, DecisionResult
from app.canonical.deterministic import (
    build_action_sig,
    build_state_dedupe_key,
    find_canonical_by_dedupe_key,
    find_object_state,
)
from app.canonical.norm import norm
from app.canonical.runners import (
    canonicalize_actors,
    canonicalize_objects,
    canonicalize_object_states,
    canonicalize_actions,
)
from app.canonical.tournament import CreateNewCanonical, LinkToExisting, run_tournament
from app.canonical.models import Candidate
from app.db.base import Base
from app.db.models import (
    Canonical,
    CanonicalAlias,
    CanonicalizationSettings,
    CanonicalMerge,
    Chunk as ChunkORM,
    Document,
    ExtractionRun,
    Frame,
    Mention,
    MentionEvidence,
    MentionToCanonical,
    ObjectState,
    Relation,
    Source,
    SourceVersion,
    Workspace,
)
from app.db.repositories.canonical_repo import CanonicalRepo
from app.db.repositories.canonical_settings_repo import (
    DEDUP_STRATEGY_ALGO_THRESHOLDS,
    get_or_create_for_workspace,
)


@pytest.fixture
def canon_runner_session():
    """In-memory session with workspace, document, chunk, run, frame, mentions, canonicals, relations."""
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
        Canonical.__table__,
        CanonicalAlias.__table__,
        ObjectState.__table__,
        MentionToCanonical.__table__,
        CanonicalMerge.__table__,
        CanonicalizationSettings.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine)
    sess = Session()
    w = Workspace(id="w-run", name="runner_test")
    sess.add(w)
    sv = Source(id="s-run", workspace_id="w-run", source_type="file")
    sess.add(sv)
    ver = SourceVersion(
        id="v-run",
        source_id="s-run",
        content_sha256="x" * 64,
        storage_uri="file:///x",
        ingested_at=datetime.now(timezone.utc),
    )
    sess.add(ver)
    d = Document(
        id="doc-run",
        source_version_id="v-run",
        extractor="d",
        extractor_version="1",
        structure_json_uri="",
        plain_text_uri="",
    )
    sess.add(d)
    chunk = ChunkORM(
        id="chunk-run",
        document_id="doc-run",
        chunk_index=0,
        text="User creates Document.",
        chunk_hash="h",
        meta_json="{}",
    )
    sess.add(chunk)
    run = ExtractionRun(
        id="run-big",
        workspace_id="w-run",
        document_id="doc-run",
        run_kind="BIG_LLM_EXTRACT",
        status="SUCCEEDED",
    )
    sess.add(run)
    frame = Frame(
        id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        frame_index=0,
        frame_text="User creates Document.",
    )
    sess.add(frame)
    sess.flush()
    sess.commit()
    yield sess
    sess.close()


def test_find_canonical_by_dedupe_key_actor(canon_runner_session):
    """Deterministic lookup finds actor by norm_name and by alias."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "ACTOR", "User", "user")
    canon_runner_session.commit()
    c = find_canonical_by_dedupe_key(canon_runner_session, "ACTOR", "actor::user")
    assert c is not None
    assert c.name == "User"
    c2 = find_canonical_by_dedupe_key(canon_runner_session, "ACTOR", "actor::unknown")
    assert c2 is None
    repo.upsert_aliases(canon_runner_session, c.id, ["End user"])
    canon_runner_session.commit()
    c3 = find_canonical_by_dedupe_key(canon_runner_session, "ACTOR", "actor::end user")
    assert c3 is not None
    assert c3.id == c.id


def test_find_canonical_by_dedupe_key_object(canon_runner_session):
    """Deterministic lookup finds object by norm_name."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "OBJECT", "Document", "document")
    canon_runner_session.commit()
    c = find_canonical_by_dedupe_key(canon_runner_session, "OBJECT", "object::document")
    assert c is not None
    assert c.name == "Document"


def test_find_canonical_by_dedupe_key_action(canon_runner_session):
    """Deterministic lookup finds ACTION canonical by action_sig (resolved dedupe key)."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "ACTOR", "User", "user")
    repo.upsert_canonical(canon_runner_session, "OBJECT", "Document", "document")
    canon_runner_session.commit()
    actor_id = repo.get_by_type_norm(canon_runner_session, "ACTOR", "user").id
    object_id = repo.get_by_type_norm(canon_runner_session, "OBJECT", "document").id
    action_sig = build_action_sig(actor_id, "create", object_id)
    repo.upsert_canonical(canon_runner_session, "ACTION", "action create", action_sig)
    canon_runner_session.commit()
    c = find_canonical_by_dedupe_key(canon_runner_session, "ACTION", action_sig)
    assert c is not None
    assert c.canonical_type == "ACTION"
    assert c.norm_name == action_sig
    c2 = find_canonical_by_dedupe_key(canon_runner_session, "ACTION", "action::x::y::z")
    assert c2 is None


def test_find_canonical_by_dedupe_key_state(canon_runner_session):
    """Deterministic lookup finds STATE canonical by state::<canonical_object_id>::<state_norm>."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "OBJECT", "Doc", "doc")
    canon_runner_session.commit()
    obj = repo.get_by_type_norm(canon_runner_session, "OBJECT", "doc")
    state = repo.get_or_create_object_state(canon_runner_session, obj.id, "ARCHIVED", "archived")
    canon_runner_session.commit()
    resolved_key = build_state_dedupe_key(obj.id, "archived")
    c = find_canonical_by_dedupe_key(canon_runner_session, "STATE", resolved_key)
    assert c is not None
    assert c.canonical_type == "STATE"
    assert c.id == state.id
    c2 = find_canonical_by_dedupe_key(canon_runner_session, "STATE", "state::nonexistent::archived")
    assert c2 is None


def test_build_action_sig():
    assert build_action_sig("aid", "create", "oid") == "action::aid::create::oid"


def test_find_object_state(canon_runner_session):
    """find_object_state returns state row by (canonical_object_id, state_norm)."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "OBJECT", "Doc", "doc")
    canon_runner_session.commit()
    obj = repo.get_by_type_norm(canon_runner_session, "OBJECT", "doc")
    state = repo.get_or_create_object_state(canon_runner_session, obj.id, "ARCHIVED", "archived")
    canon_runner_session.commit()
    found = find_object_state(canon_runner_session, obj.id, "archived")
    assert found is not None
    assert found.id == state.id


class MockEmbedClient:
    async def embed_texts(self, texts):
        return [[0.1] * 768 for _ in texts]


class MockQdrantClient:
    """Minimal mock: empty search results; no-op upsert/set_payload for state and canonical upserts."""

    async def query_points(self, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(points=[])

    async def upsert(self, **kwargs):
        pass

    async def set_payload(self, **kwargs):
        pass


@pytest.mark.asyncio
async def test_canonicalize_actors_deterministic(canon_runner_session):
    """With existing Actor canonical and matching mention dedupe_key, no LLM is called; RULE decision."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "ACTOR", "User", "user")
    canon_runner_session.commit()
    actor_canonical_id = repo.get_by_type_norm(canon_runner_session, "ACTOR", "user").id

    mention = Mention(
        id="m-act-1",
        frame_id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        type="ACTOR",
        fields_json=json.dumps({"name": "User"}),
        epistemic="EXPLICIT",
        dedupe_key="actor::user",
        created_run_id="run-big",
    )
    canon_runner_session.add(mention)
    canon_runner_session.commit()

    call_count = 0

    async def mock_llm_chat(req, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("LLM should not be called in deterministic path")

    run_id = await canonicalize_actors(
        "doc-run",
        session=canon_runner_session,
        embed_client=MockEmbedClient(),
        qdrant_client=MockQdrantClient(),
        llm_client=mock_llm_chat,
        workspace_id="w-run",
    )
    assert call_count == 0
    mtc = canon_runner_session.execute(
        select(MentionToCanonical).where(MentionToCanonical.mention_id == "m-act-1")
    ).scalar_one_or_none()
    assert mtc is not None
    assert mtc.canonical_id == actor_canonical_id
    assert mtc.decision == "ACCEPT"
    assert mtc.decided_by == "RULE"


@pytest.mark.asyncio
async def test_canonicalize_objects_deterministic(canon_runner_session):
    """With existing Object canonical and matching mention, resolution by RULE."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "OBJECT", "Document", "document")
    canon_runner_session.commit()
    obj_canonical_id = repo.get_by_type_norm(canon_runner_session, "OBJECT", "document").id

    mention = Mention(
        id="m-obj-1",
        frame_id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        type="OBJECT",
        fields_json=json.dumps({"name": "Document"}),
        epistemic="EXPLICIT",
        dedupe_key="object::document",
        created_run_id="run-big",
    )
    canon_runner_session.add(mention)
    canon_runner_session.commit()

    async def no_llm(req, **kwargs):
        raise RuntimeError("LLM should not be called")

    run_id = await canonicalize_objects(
        "doc-run",
        session=canon_runner_session,
        embed_client=MockEmbedClient(),
        qdrant_client=MockQdrantClient(),
        llm_client=no_llm,
        workspace_id="w-run",
    )
    mtc = canon_runner_session.execute(
        select(MentionToCanonical).where(MentionToCanonical.mention_id == "m-obj-1")
    ).scalar_one_or_none()
    assert mtc is not None
    assert mtc.canonical_id == obj_canonical_id
    assert mtc.decided_by == "RULE"


@pytest.mark.asyncio
async def test_tournament_link_to_existing():
    """Tournament returns LinkToExisting when comparator returns SAME above threshold."""
    class FakeMention:
        id = "m1"
        type = "ACTOR"
        fields_json = "{}"
        evidence = None
    cand1 = Candidate(canonical_id="c1", name="User", norm_name="user", aliases=[], score=0.9, payload={})
    cand2 = Candidate(canonical_id="c2", name="Admin", norm_name="admin", aliases=[], score=0.7, payload={})
    same_result = DecisionResult(decision=DECISION_SAME, confidence="HIGH", reason="same")

    async def compare_same_first(m, c):
        if c.canonical_id == "c1":
            return same_result
        return DecisionResult(decision=DECISION_DIFFERENT, confidence="HIGH", reason="")

    outcome = await run_tournament(
        FakeMention(),
        [cand1, cand2],
        compare_fn=compare_same_first,
        top_n=3,
        confidence_threshold="MEDIUM",
    )
    assert isinstance(outcome, LinkToExisting)
    assert outcome.canonical_id == "c1"


@pytest.mark.asyncio
async def test_tournament_create_new_when_all_different():
    """Tournament returns CreateNewCanonical when all candidates are DIFFERENT."""
    class FakeMention:
        id = "m1"
        type = "OBJECT"
        fields_json = "{}"
        evidence = None
    cand = Candidate(canonical_id="c1", name="Other", norm_name="other", aliases=[], score=0.5, payload={})

    async def compare_diff(m, c):
        return DecisionResult(decision=DECISION_DIFFERENT, confidence="HIGH", reason="")

    outcome = await run_tournament(
        FakeMention(),
        [cand],
        compare_fn=compare_diff,
        top_n=3,
    )
    assert isinstance(outcome, CreateNewCanonical)


@pytest.mark.asyncio
async def test_state_scoping_two_objects(canon_runner_session):
    """State canonicalization creates distinct state rows per object (no cross-object merge)."""
    repo = CanonicalRepo()
    repo.upsert_canonical(canon_runner_session, "OBJECT", "Document", "document")
    repo.upsert_canonical(canon_runner_session, "OBJECT", "Order", "order")
    canon_runner_session.commit()
    doc_canonical = repo.get_by_type_norm(canon_runner_session, "OBJECT", "document")
    order_canonical = repo.get_by_type_norm(canon_runner_session, "OBJECT", "order")

    m_obj_doc = Mention(
        id="m-obj-doc",
        frame_id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        type="OBJECT",
        fields_json=json.dumps({"name": "Document"}),
        epistemic="EXPLICIT",
        dedupe_key="object::document",
        created_run_id="run-big",
    )
    m_obj_order = Mention(
        id="m-obj-order",
        frame_id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        type="OBJECT",
        fields_json=json.dumps({"name": "Order"}),
        epistemic="EXPLICIT",
        dedupe_key="object::order",
        created_run_id="run-big",
    )
    m_state_doc = Mention(
        id="m-state-doc",
        frame_id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        type="STATE",
        fields_json=json.dumps({"state_name": "ARCHIVED", "object_name": "Document"}),
        epistemic="EXPLICIT",
        dedupe_key="state::document::archived",
        created_run_id="run-big",
    )
    m_state_order = Mention(
        id="m-state-order",
        frame_id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        type="STATE",
        fields_json=json.dumps({"state_name": "ARCHIVED", "object_name": "Order"}),
        epistemic="EXPLICIT",
        dedupe_key="state::order::archived",
        created_run_id="run-big",
    )
    canon_runner_session.add_all([m_obj_doc, m_obj_order, m_state_doc, m_state_order])
    canon_runner_session.flush()
    canon_runner_session.add(Relation(frame_id="frame-run", src_mention_id="m-obj-doc", rel_type="OBJECT_HAS_STATE", dst_mention_id="m-state-doc"))
    canon_runner_session.add(Relation(frame_id="frame-run", src_mention_id="m-obj-order", rel_type="OBJECT_HAS_STATE", dst_mention_id="m-state-order"))
    canon_runner_session.commit()

    _link = MentionToCanonical(mention_id="m-obj-doc", canonical_id=doc_canonical.id, decision="ACCEPT", decided_by="RULE", run_id="run-big")
    canon_runner_session.add(_link)
    _link2 = MentionToCanonical(mention_id="m-obj-order", canonical_id=order_canonical.id, decision="ACCEPT", decided_by="RULE", run_id="run-big")
    canon_runner_session.add(_link2)
    canon_runner_session.commit()

    async def no_llm(req, **kw):
        raise RuntimeError("LLM should not be called in state deterministic path")

    await canonicalize_object_states(
        "doc-run",
        session=canon_runner_session,
        embed_client=MockEmbedClient(),
        qdrant_client=MockQdrantClient(),
        llm_client=no_llm,
        workspace_id="w-run",
    )
    states = list(canon_runner_session.execute(select(ObjectState)).scalars().all())
    assert len(states) == 2
    obj_ids = {s.canonical_object_id for s in states}
    assert doc_canonical.id in obj_ids
    assert order_canonical.id in obj_ids
    norms = {s.state_norm for s in states}
    assert norms == {"archived"}
    mtc_doc = canon_runner_session.execute(select(MentionToCanonical).where(MentionToCanonical.mention_id == "m-state-doc")).scalar_one_or_none()
    mtc_order = canon_runner_session.execute(select(MentionToCanonical).where(MentionToCanonical.mention_id == "m-state-order")).scalar_one_or_none()
    assert mtc_doc is not None and mtc_order is not None
    assert mtc_doc.canonical_id != mtc_order.canonical_id


def test_get_or_create_for_workspace_creates_global_default(canon_runner_session):
    """When no row exists, get_or_create_for_workspace creates and returns a global default row."""
    settings = get_or_create_for_workspace(canon_runner_session, "w-run")
    canon_runner_session.commit()
    assert settings.workspace_id is None
    assert settings.dedup_strategy == "LLM_TOURNAMENT"
    assert settings.min_mentions_for_canonical == 5
    assert settings.cosine_high_merge_threshold == 0.9
    assert settings.cosine_low_new_threshold == 0.2


def test_get_or_create_for_workspace_returns_workspace_row(canon_runner_session):
    """When workspace-specific row exists, it is returned."""
    canon_runner_session.add(
        CanonicalizationSettings(
            workspace_id="w-run",
            dedup_strategy=DEDUP_STRATEGY_ALGO_THRESHOLDS,
            min_mentions_for_canonical=3,
            cosine_high_merge_threshold=0.85,
            cosine_low_new_threshold=0.15,
        )
    )
    canon_runner_session.commit()
    settings = get_or_create_for_workspace(canon_runner_session, "w-run")
    assert settings.workspace_id == "w-run"
    assert settings.dedup_strategy == DEDUP_STRATEGY_ALGO_THRESHOLDS
    assert settings.min_mentions_for_canonical == 3


@pytest.mark.asyncio
async def test_canonicalize_actors_algo_freq(canon_runner_session):
    """With ALGO_THRESHOLDS and min_mentions_for_canonical=2, two mentions with same dedupe_key become one canonical (ALGO_FREQ), no LLM."""
    canon_runner_session.add(
        CanonicalizationSettings(
            workspace_id="w-run",
            dedup_strategy=DEDUP_STRATEGY_ALGO_THRESHOLDS,
            min_mentions_for_canonical=2,
            cosine_high_merge_threshold=0.9,
            cosine_low_new_threshold=0.2,
        )
    )
    canon_runner_session.commit()

    for i in range(2):
        m = Mention(
            id=f"m-algo-{i}",
            frame_id="frame-run",
            document_id="doc-run",
            chunk_id="chunk-run",
            type="ACTOR",
            fields_json=json.dumps({"name": "Admin"}),
            epistemic="EXPLICIT",
            dedupe_key="actor::admin",
            created_run_id="run-big",
        )
        canon_runner_session.add(m)
    canon_runner_session.commit()

    async def no_llm(req, **kwargs):
        raise RuntimeError("LLM should not be called in algo path")

    run_id = await canonicalize_actors(
        "doc-run",
        session=canon_runner_session,
        embed_client=MockEmbedClient(),
        qdrant_client=MockQdrantClient(),
        llm_client=no_llm,
        workspace_id="w-run",
    )
    canon_runner_session.commit()

    mtcs = list(
        canon_runner_session.execute(
            select(MentionToCanonical).where(
                MentionToCanonical.mention_id.in_(["m-algo-0", "m-algo-1"])
            )
        ).scalars().all()
    )
    assert len(mtcs) == 2
    assert mtcs[0].canonical_id == mtcs[1].canonical_id
    assert mtcs[0].decided_by == "ALGO_FREQ"
    assert mtcs[1].decided_by == "ALGO_FREQ"
    canonicals = list(canon_runner_session.execute(select(Canonical).where(Canonical.canonical_type == "ACTOR")).scalars().all())
    assert len(canonicals) == 1
    assert canonicals[0].norm_name == "admin"


@pytest.mark.asyncio
async def test_canonicalize_actors_algo_new_no_candidates(canon_runner_session):
    """With ALGO_THRESHOLDS, one mention and no Qdrant candidates -> new canonical (ALGO_NEW), no LLM."""
    canon_runner_session.add(
        CanonicalizationSettings(
            workspace_id="w-run",
            dedup_strategy=DEDUP_STRATEGY_ALGO_THRESHOLDS,
            min_mentions_for_canonical=5,
            cosine_high_merge_threshold=0.9,
            cosine_low_new_threshold=0.2,
        )
    )
    canon_runner_session.commit()

    m = Mention(
        id="m-algo-solo",
        frame_id="frame-run",
        document_id="doc-run",
        chunk_id="chunk-run",
        type="ACTOR",
        fields_json=json.dumps({"name": "CustomRole"}),
        epistemic="EXPLICIT",
        dedupe_key="actor::customrole",
        created_run_id="run-big",
    )
    canon_runner_session.add(m)
    canon_runner_session.commit()

    async def no_llm(req, **kwargs):
        raise RuntimeError("LLM should not be called")

    run_id = await canonicalize_actors(
        "doc-run",
        session=canon_runner_session,
        embed_client=MockEmbedClient(),
        qdrant_client=MockQdrantClient(),
        llm_client=no_llm,
        workspace_id="w-run",
    )
    canon_runner_session.commit()

    mtc = canon_runner_session.execute(
        select(MentionToCanonical).where(MentionToCanonical.mention_id == "m-algo-solo")
    ).scalar_one_or_none()
    assert mtc is not None
    assert mtc.decision == "ACCEPT"
    assert mtc.decided_by == "ALGO_NEW"
    canonicals = list(canon_runner_session.execute(select(Canonical).where(Canonical.canonical_type == "ACTOR")).scalars().all())
    assert len(canonicals) == 1
    assert canonicals[0].norm_name == "customrole"
