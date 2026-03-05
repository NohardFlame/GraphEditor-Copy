"""Pytest config and fixtures for db module tests."""
import json
import os
import tempfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.config import DBConfig
from app.db.engine import create_engine_from_config, create_async_engine_from_config
from app.db.session import init_db

# Import models so Base.metadata has all tables
import app.db.models  # noqa: F401
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
from app.db.models.pipeline_run import PipelineRun


@pytest.fixture
def temp_db_url() -> str:
    """SQLite URL for a temporary file (WAL-friendly)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield f"sqlite:///{path}"
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def db_config(temp_db_url: str) -> DBConfig:
    """DBConfig pointing to temp SQLite file."""
    return DBConfig(db_url=temp_db_url, echo_sql=False)


@pytest.fixture
def sync_engine(db_config: DBConfig):
    """Sync engine for temp DB."""
    return create_engine_from_config(db_config)


@pytest.fixture
def async_engine(db_config: DBConfig):
    """Async engine for temp DB."""
    return create_async_engine_from_config(db_config)


@pytest.fixture
def db_with_tables(sync_engine):
    """Create all tables on the engine (for tests that need schema)."""
    Base.metadata.create_all(sync_engine)
    return sync_engine


@pytest.fixture
def big_extract_session():
    """Session with Workspace, Source, SourceVersion, Document, Chunks, ExtractionRun, Frame, Mention, etc."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Workspace.__table__,
            Source.__table__,
            SourceVersion.__table__,
            Document.__table__,
            ChunkORM.__table__,
            PipelineRun.__table__,
            ExtractionRun.__table__,
            Frame.__table__,
            Mention.__table__,
            MentionEvidence.__table__,
            Relation.__table__,
            LlmCall.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    sess = Session()
    w = Workspace(id="w-big", name="big_extract_test")
    sess.add(w)
    s = Source(id="s-big", workspace_id="w-big", source_type="file")
    sess.add(s)
    v = SourceVersion(
        id="v-big",
        source_id="s-big",
        content_sha256="c" * 64,
        storage_uri="file:///tmp/big",
        ingested_at=datetime.now(timezone.utc),
    )
    sess.add(v)
    d = Document(
        id="doc-big",
        source_version_id="v-big",
        extractor="docling",
        extractor_version="1",
        structure_json_uri="file:///doc/s.json",
        plain_text_uri="file:///doc/p.txt",
    )
    sess.add(d)
    sess.flush()
    for i, (chunk_hash, text, section_path) in enumerate(
        [
            ("h0", "The User creates a new Document.", "1/1"),
            ("h1", "The Admin archives the Document.", "1/2"),
        ]
    ):
        ch = ChunkORM(
            document_id="doc-big",
            chunk_index=i,
            chunk_hash=chunk_hash,
            text=text,
            meta_json=json.dumps({"section_path": section_path}),
        )
        sess.add(ch)
    sess.commit()
    yield sess
    sess.close()
