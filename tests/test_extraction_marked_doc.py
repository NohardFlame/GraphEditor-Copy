"""Phase 2 extraction tests: marker round-trip, size splitting, stability, integration."""
import json
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Chunk as ChunkORM,
    Document,
    Source,
    SourceVersion,
    Workspace,
)
from app.db.repositories.chunk_repo import ChunkPayload, ChunkRepo
from app.extraction.chunking import Chunk, DocPart
from app.extraction.marked_doc_builder import (
    CHUNK_CLOSE,
    CHUNK_OPEN_PREFIX,
    build_marked_doc,
    build_marked_doc_for_chunks,
    load_chunks_for_document,
    size_guard_from_chunks,
)


def _parse_marked_doc(marked: str) -> list[tuple[str, int]]:
    """Parse marked document; return list of (chunk_id, chunk_index) in order."""
    results = []
    pattern = re.compile(
        r'<<<CHUNK\s+id="([^"]+)"\s+index=(\d+)\s+section_path="([^"]*)"\s*>>>'
    )
    for line in marked.splitlines():
        m = pattern.match(line.strip())
        if m:
            results.append((m.group(1), int(m.group(2))))
    return results


# --- Unit: round-trip markers ---


def test_marker_round_trip_chunk_ids_and_indices():
    """Build marked doc from synthetic chunks; parse and verify chunk_id and chunk_index match."""
    doc_id = "doc-synth-1"
    chunks = [
        Chunk("c1", doc_id, 0, "1/1", "First paragraph."),
        Chunk("c2", doc_id, 1, "1/2", "Second paragraph."),
        Chunk("c3", doc_id, 2, None, "Third with no path."),
    ]
    marked = build_marked_doc_for_chunks(doc_id, "Title", "source://x", chunks)
    parsed = _parse_marked_doc(marked)
    assert len(parsed) == 3
    assert parsed[0] == ("c1", 0)
    assert parsed[1] == ("c2", 1)
    assert parsed[2] == ("c3", 2)
    assert CHUNK_CLOSE in marked
    assert "# DOC_META" in marked
    assert "# CHUNKS (ordered)" in marked


def test_marker_round_trip_verbatim_text():
    """Chunk text appears verbatim in marked output (no normalization)."""
    doc_id = "doc-synth-2"
    text = "  Line one.\n\n  Line two.  "
    chunks = [Chunk("c1", doc_id, 0, None, text)]
    marked = build_marked_doc_for_chunks(doc_id, "", "", chunks)
    assert text in marked
    assert marked.count(CHUNK_CLOSE) == 1


# --- Unit: size splitting ---


def test_size_guard_single_part_when_under_limit():
    """When total size <= max_bytes, return one DocPart."""
    doc_id = "doc-small"
    chunks = [
        Chunk("c1", doc_id, 0, None, "short"),
        Chunk("c2", doc_id, 1, None, "text"),
    ]
    parts = size_guard_from_chunks(doc_id, chunks, max_bytes=10_000)
    assert len(parts) == 1
    assert parts[0].part_index == 0
    assert parts[0].first_chunk_index == 0
    assert parts[0].last_chunk_index == 1
    assert parts[0].part_id == f"doc::{doc_id}::part::0"


def test_size_guard_multiple_parts_with_overlap():
    """When over limit, split into parts with overlapping chunk indices."""
    doc_id = "doc-big"
    # Chunks large enough that 2–3 chunks exceed 1 KB
    chunk_size = 400
    chunks = [
        Chunk(f"c{i}", doc_id, i, None, "x" * chunk_size)
        for i in range(5)
    ]
    parts = size_guard_from_chunks(
        doc_id, chunks, max_bytes=1024, overlap_chunks=2
    )
    assert len(parts) >= 2
    # All chunk indices should be covered
    all_first = [p.first_chunk_index for p in parts]
    all_last = [p.last_chunk_index for p in parts]
    assert min(all_first) == 0
    assert max(all_last) == 4
    # Overlap: next part's first should be <= previous part's last
    for i in range(len(parts) - 1):
        assert parts[i + 1].first_chunk_index <= parts[i].last_chunk_index + 1


def test_size_guard_determinism():
    """Same inputs produce identical DocPart list (byte-stable)."""
    doc_id = "doc-det"
    chunks = [
        Chunk("c1", doc_id, 0, None, "a" * 300),
        Chunk("c2", doc_id, 1, None, "b" * 300),
        Chunk("c3", doc_id, 2, None, "c" * 300),
    ]
    parts1 = size_guard_from_chunks(doc_id, chunks, max_bytes=1024)
    parts2 = size_guard_from_chunks(doc_id, chunks, max_bytes=1024)
    assert len(parts1) == len(parts2)
    for p1, p2 in zip(parts1, parts2):
        assert p1.part_id == p2.part_id
        assert p1.part_index == p2.part_index
        assert p1.first_chunk_index == p2.first_chunk_index
        assert p1.last_chunk_index == p2.last_chunk_index
        assert p1.text == p2.text


def test_size_guard_empty_chunks_returns_empty():
    """Empty chunk list returns empty DocPart list."""
    parts = size_guard_from_chunks("doc-id", [], max_bytes=1000)
    assert parts == []


# --- Integration: SQL-backed ---


@pytest.fixture
def extraction_session():
    """Session with Workspace, Source, SourceVersion, Document, and Chunks."""
    engine = create_engine("sqlite:///:memory:")
    # Create only the tables needed for this test to avoid index conflicts
    Base.metadata.create_all(
        engine,
        tables=[
            Workspace.__table__,
            Source.__table__,
            SourceVersion.__table__,
            Document.__table__,
            ChunkORM.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    sess = Session()
    w = Workspace(id="w-ext", name="extraction_test")
    sess.add(w)
    s = Source(id="s-ext", workspace_id="w-ext", source_type="file")
    sess.add(s)
    v = SourceVersion(
        id="v-ext",
        source_id="s-ext",
        content_sha256="b" * 64,
        storage_uri="file:///tmp/ext",
        ingested_at=datetime.now(timezone.utc),
    )
    sess.add(v)
    d = Document(
        id="doc-ext",
        source_version_id="v-ext",
        extractor="docling",
        extractor_version="1",
        structure_json_uri="file:///doc/structure.json",
        plain_text_uri="file:///doc/plain.txt",
    )
    sess.add(d)
    sess.flush()
    repo = ChunkRepo()
    payloads = [
        ChunkPayload(
            chunk_hash="h0",
            chunk_index=0,
            text="Chunk zero.",
            meta_json=json.dumps({"section_path": "1/1"}),
        ),
        ChunkPayload(
            chunk_hash="h1",
            chunk_index=1,
            text="Chunk one.",
            meta_json=json.dumps({"section_path": "1/2"}),
        ),
    ]
    repo.bulk_upsert_chunks(sess, "doc-ext", payloads, batch_rows=10)
    sess.commit()
    yield sess
    sess.close()


def test_build_marked_doc_from_sql(extraction_session):
    """build_marked_doc loads chunks from SQL and produces valid markers."""
    marked = build_marked_doc("doc-ext", session=extraction_session)
    assert "# DOC_META" in marked
    assert "doc_id: doc-ext" in marked
    assert "# CHUNKS (ordered)" in marked
    parsed = _parse_marked_doc(marked)
    assert len(parsed) == 2
    assert "Chunk zero." in marked
    assert "Chunk one." in marked


def test_load_chunks_for_document_matches_sql(extraction_session):
    """load_chunks_for_document returns Chunk list with ids matching DB."""
    chunks = load_chunks_for_document("doc-ext", extraction_session)
    assert len(chunks) == 2
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1
    assert chunks[0].text == "Chunk zero."
    assert chunks[1].text == "Chunk one."
    assert chunks[0].section_path == "1/1"
    assert chunks[1].section_path == "1/2"
    # chunk_ids should be the actual PKs from DB
    from sqlalchemy import select
    rows = extraction_session.scalars(
        select(ChunkORM)
        .where(ChunkORM.document_id == "doc-ext")
        .order_by(ChunkORM.chunk_index)
    ).all()
    assert [c.chunk_id for c in chunks] == [r.id for r in rows]


def test_integration_markers_reference_real_chunk_ids(extraction_session):
    """Markers in built doc reference chunk_ids that exist in DB."""
    marked = build_marked_doc("doc-ext", session=extraction_session)
    parsed = _parse_marked_doc(marked)
    for chunk_id, _ in parsed:
        row = extraction_session.get(ChunkORM, chunk_id)
        assert row is not None, f"chunk_id {chunk_id} from marker should exist in DB"
