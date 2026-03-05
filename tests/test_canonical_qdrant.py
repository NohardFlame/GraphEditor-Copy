"""Phase 4 canonical registries and Qdrant tests: unit + integration (smoke, idempotency, merge, bootstrap)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.canonical.embedding_text import (
    build_action_embedding_text,
    build_actor_embedding_text,
    build_object_embedding_text,
    build_state_embedding_text,
)
from app.canonical.norm import norm
from app.canonical.service import (
    build_mention_card_text,
    get_canonical_candidates_for_mention,
    record_canonical_merge,
    upsert_canonical_and_qdrant,
    upsert_state_and_qdrant,
)
from app.canonical.models import CanonicalData
from app.db.base import Base
from app.db.models import (
    Canonical,
    CanonicalAlias,
    CanonicalMerge,
    Chunk as ChunkORM,
    Document,
    ExtractionRun,
    Frame,
    Mention,
    MentionEvidence,
    ObjectState,
    MentionToCanonical,
    Relation,
    Workspace,
    Source,
    SourceVersion,
)
from app.db.repositories.canonical_repo import CanonicalRepo

# Optional Qdrant for integration tests
try:
    from qdrant_client import AsyncQdrantClient
    _client = AsyncQdrantClient(url="http://localhost:6333", timeout=2.0)
    import asyncio
    asyncio.run(_client.get_collections())
    del _client
    QDRANT_AVAILABLE = True
except Exception:
    QDRANT_AVAILABLE = False


# --- Unit tests (no Qdrant) ---


def test_norm():
    assert norm("  User  ") == "user"
    assert norm("End  User") == "end user"
    assert norm("") == ""


def test_build_actor_embedding_text():
    class C:
        name = "User"
        aliases = []
    class A:
        alias_text = "End user"
    assert "ACTOR: User." in build_actor_embedding_text(C(), [type("A", (), {"alias_text": "End user"})()])
    assert "Aliases: End user" in build_actor_embedding_text(C(), [type("A", (), {"alias_text": "End user"})()])


def test_build_object_embedding_text():
    class C:
        name = "Document"
    assert "OBJECT: Document." in build_object_embedding_text(C(), None)


def test_build_state_embedding_text():
    class Obj:
        name = "Document"
    class St:
        state_name = "Archived"
    assert "STATE of Document: Archived." in build_state_embedding_text(Obj(), St())


def test_build_mention_card_text():
    class M:
        type = "ACTOR"
        fields_json = json.dumps({"name": "User"})
        evidence = None
    class E:
        snippet_text = "the user clicks"
    m = M()
    m.evidence = E()
    text = build_mention_card_text(m, None)
    assert "ACTOR MENTION" in text
    assert "User" in text
    assert "the user clicks" in text


@pytest.fixture
def canonical_session():
    """Session with Workspace, Document, Chunk, ExtractionRun, Frame, Mention, Canonical tables (own in-memory DB)."""
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
    ]
    Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine)
    sess = Session()
    w = Workspace(id="w-can", name="canonical_test")
    sess.add(w)
    sv = Source(id="s-can", workspace_id="w-can", source_type="file")
    sess.add(sv)
    ver = SourceVersion(id="v-can", source_id="s-can", content_sha256="x" * 64, storage_uri="file:///x", ingested_at=datetime.now(timezone.utc))
    sess.add(ver)
    d = Document(id="doc-can", source_version_id="v-can", extractor="d", extractor_version="1", structure_json_uri="", plain_text_uri="")
    sess.add(d)
    chunk = ChunkORM(id="chunk-can", document_id="doc-can", chunk_index=0, text="User creates Document.", chunk_hash="h", meta_json="{}")
    sess.add(chunk)
    run = ExtractionRun(id="run-can", workspace_id="w-can", document_id="doc-can", run_kind="BIG_LLM_EXTRACT", status="SUCCEEDED")
    sess.add(run)
    frame = Frame(
        id="frame-can",
        document_id="doc-can",
        chunk_id="chunk-can",
        frame_index=0,
        frame_text="User creates Document.",
    )
    sess.add(frame)
    sess.flush()
    sess.commit()
    yield sess
    sess.close()


def test_canonical_repo_upsert_and_get(canonical_session):
    repo = CanonicalRepo()
    c = repo.upsert_canonical(canonical_session, "ACTOR", "User", "user")
    assert c.id
    assert c.name == "User"
    assert c.norm_name == "user"
    repo.upsert_aliases(canonical_session, c.id, ["End user", "Customer"])
    canonical_session.commit()
    same = repo.get_by_type_norm(canonical_session, "ACTOR", "user")
    assert same.id == c.id
    assert len(same.aliases) == 2


def test_canonical_repo_idempotent_upsert(canonical_session):
    repo = CanonicalRepo()
    c1 = repo.upsert_canonical(canonical_session, "OBJECT", "Document", "document")
    c2 = repo.upsert_canonical(canonical_session, "OBJECT", "Document", "document")
    canonical_session.commit()
    assert c1.id == c2.id
    count = canonical_session.execute(select(Canonical).where(Canonical.canonical_type == "OBJECT")).scalars().all()
    assert len(count) == 1


# --- Mock embed client ---


class MockEmbedClient:
    """Returns fixed vector so stored canonical and mention card are identical for search."""
    def __init__(self, dims: int = 768):
        self.dims = dims
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dims for _ in texts]


# --- Integration tests (Qdrant required) ---


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not QDRANT_AVAILABLE, reason="Qdrant not available at localhost:6333")
async def test_ensure_canonical_collections():
    from qdrant_client import AsyncQdrantClient
    from app.canonical.qdrant_collections import ensure_canonical_collections, CANONICAL_COLLECTION_NAMES
    from app.vectorstore import build_qdrant_client
    client = build_qdrant_client()
    await ensure_canonical_collections(client, vector_size=768)
    for name in CANONICAL_COLLECTION_NAMES:
        info = await client.get_collection(name)
        assert info.config.params.vectors.size == 768


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not QDRANT_AVAILABLE, reason="Qdrant not available")
async def test_upsert_canonical_and_qdrant_idempotency(canonical_session):
    from qdrant_client import AsyncQdrantClient
    from app.vectorstore import build_qdrant_client
    from app.canonical.qdrant_collections import ensure_canonical_collections, COLLECTION_ACTORS
    client = build_qdrant_client()
    await ensure_canonical_collections(client, 768)
    embed = MockEmbedClient(768)
    data = CanonicalData(canonical_type="ACTOR", name="User", aliases=["End user", "Customer"])
    c1 = await upsert_canonical_and_qdrant(
        canonical_session, data, embed_client=embed, qdrant_client=client
    )
    c2 = await upsert_canonical_and_qdrant(
        canonical_session, data, embed_client=embed, qdrant_client=client
    )
    canonical_session.commit()
    assert c1.id == c2.id
    from app.canonical.qdrant_ops import search_canonical_collection
    hits = await search_canonical_collection(client, COLLECTION_ACTORS, [0.1] * 768, top_k=10)
    assert len(hits) >= 1
    ids = {h.point_id for h in hits}
    assert c1.id in ids


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not QDRANT_AVAILABLE, reason="Qdrant not available")
async def test_smoke_upsert_and_search(canonical_session):
    from app.vectorstore import build_qdrant_client
    from app.canonical.qdrant_collections import ensure_canonical_collections
    client = build_qdrant_client()
    await ensure_canonical_collections(client, 768)
    embed = MockEmbedClient(768)
    await upsert_canonical_and_qdrant(
        canonical_session,
        CanonicalData(canonical_type="ACTOR", name="User", aliases=["End user", "Customer"]),
        embed_client=embed,
        qdrant_client=client,
    )
    await upsert_canonical_and_qdrant(
        canonical_session,
        CanonicalData(canonical_type="OBJECT", name="Document", aliases=["File", "Record"]),
        embed_client=embed,
        qdrant_client=client,
    )
    canonical_session.commit()
    # Create a mention (actor "User") and frame so we can call get_canonical_candidates_for_mention
    frame = canonical_session.execute(select(Frame).where(Frame.id == "frame-can")).scalar_one()
    mention = Mention(
        id="m-actor",
        frame_id=frame.id,
        document_id="doc-can",
        chunk_id="chunk-can",
        type="ACTOR",
        fields_json=json.dumps({"name": "User"}),
        epistemic="EXPLICIT",
    )
    canonical_session.add(mention)
    canonical_session.commit()
    canonical_session.refresh(mention)
    candidates = await get_canonical_candidates_for_mention(
        mention, frame, "ACTOR", k=5, embed_client=embed, qdrant_client=client
    )
    assert len(candidates) >= 1
    assert candidates[0].name == "User"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not QDRANT_AVAILABLE, reason="Qdrant not available")
async def test_merge(canonical_session):
    from app.vectorstore import build_qdrant_client
    from app.canonical.qdrant_collections import ensure_canonical_collections, COLLECTION_OBJECTS
    from app.canonical.qdrant_ops import search_canonical_collection
    client = build_qdrant_client()
    await ensure_canonical_collections(client, 768)
    embed = MockEmbedClient(768)
    c1 = await upsert_canonical_and_qdrant(
        canonical_session,
        CanonicalData(canonical_type="OBJECT", name="Invoice", aliases=[]),
        embed_client=embed,
        qdrant_client=client,
    )
    c2 = await upsert_canonical_and_qdrant(
        canonical_session,
        CanonicalData(canonical_type="OBJECT", name="Billing invoice", aliases=[]),
        embed_client=embed,
        qdrant_client=client,
    )
    canonical_session.commit()
    await record_canonical_merge(
        canonical_session, c2.id, c1.id, client, reason="same concept"
    )
    canonical_session.commit()
    from_c = canonical_session.execute(select(Canonical).where(Canonical.id == c2.id)).scalar_one()
    assert from_c.superseded_by_id == c1.id
    merge_rows = canonical_session.execute(
        select(CanonicalMerge).where(CanonicalMerge.from_canonical_id == c2.id)
    ).scalars().all()
    assert len(merge_rows) == 1
    hits = await search_canonical_collection(client, COLLECTION_OBJECTS, [0.1] * 768, top_k=10, exclude_superseded=True)
    payload_from = next((h for h in hits if h.point_id == c2.id), None)
    if payload_from:
        assert payload_from.payload.get("superseded_by") == c1.id


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not QDRANT_AVAILABLE, reason="Qdrant not available")
async def test_bootstrap_canonical_collections():
    from app.canonical.bootstrap import bootstrap_canonical_collections
    await bootstrap_canonical_collections()
