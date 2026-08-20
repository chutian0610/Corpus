"""Storage 层测试。"""

import json
from pathlib import Path

import pytest

from corpus_bot.storage import (
    init_db,
    stage_source,
    commit_source,
    write_concept,
    update_concept,
    read_concept,
    read_source,
    list_sources,
    list_concepts,
    find_concept_by_link,
    mark_certified,
    unmark_certified,
    list_uncertified_concepts,
    certification_stats,
)
from corpus_bot.errors import ConflictError


@pytest.fixture
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


def test_stage_source_returns_metadata(db: Path):
    result = stage_source(db, raw_path=Path("/tmp/raw/a.md"), content="hello", original_filename="a.md")
    assert result["status"] == "staged"
    assert len(result["source_id"]) == 16
    assert result["size_bytes"] == 5


def test_stage_source_duplicate_rejected(db: Path):
    stage_source(db, raw_path=Path("/tmp/raw/a.md"), content="hello")
    with pytest.raises(ConflictError) as exc_info:
        stage_source(db, raw_path=Path("/tmp/raw/b.md"), content="hello")
    assert "duplicate content" in str(exc_info.value)


def test_commit_source(db: Path):
    result = stage_source(db, raw_path=Path("/tmp/raw/a.md"), content="x")
    sid = result["source_id"]
    commit_source(db, sid)
    item = read_source(db, sid)
    assert item["status"] == "committed"


def test_write_concept_basic(db: Path):
    result = write_concept(
        db,
        slug="postgres-mvcc",
        title="PostgreSQL MVCC",
        body="MVCC explanation",
        source_ids=["abc"],
        links=["postgres-transactions"],
    )
    assert result["slug"] == "postgres-mvcc"


def test_write_concept_slug_conflict(db: Path):
    write_concept(db, slug="x", title="X", body="", source_ids=[], links=[])
    with pytest.raises(ConflictError):
        write_concept(db, slug="x", title="X v2", body="", source_ids=[], links=[])


def test_update_concept_add_source(db: Path):
    write_concept(db, slug="x", title="X", body="", source_ids=["s1"], links=[])
    update_concept(db, "x", add_source_ids=["s2"])
    item = read_concept(db, "x")
    assert set(item["source_ids"]) == {"s1", "s2"}


def test_find_concept_by_link_exact(db: Path):
    # "PostgreSQL" 小写后是 "postgresql"（SQL 全大写），slug 是 "postgresql-mvcc"
    write_concept(db, slug="postgresql-mvcc", title="", body="", source_ids=[], links=[])
    matches = find_concept_by_link(db, "PostgreSQL MVCC")
    assert len(matches) == 1
    assert matches[0]["slug"] == "postgresql-mvcc"


def test_mark_certified(db: Path):
    write_concept(db, slug="x", title="X", body="", source_ids=[], links=[])
    mark_certified(db, slug="x", score=0.85, issues=["i1"], suggestions=["s1"], certified_by="agent")
    item = read_concept(db, "x")
    assert item["certified_score"] == 0.85
    assert item["certified_by"] == "agent"


def test_mark_certified_invalid_score(db: Path):
    write_concept(db, slug="x", title="X", body="", source_ids=[], links=[])
    with pytest.raises(Exception):
        mark_certified(db, slug="x", score=1.5, issues=[], suggestions=[])


def test_list_uncertified(db: Path):
    write_concept(db, slug="a", title="A", body="", source_ids=[], links=[])
    write_concept(db, slug="b", title="B", body="", source_ids=[], links=[])
    mark_certified(db, slug="a", score=0.9, issues=[], suggestions=[])
    uncertified = list_uncertified_concepts(db)
    assert len(uncertified) == 1
    assert uncertified[0]["slug"] == "b"


def test_certification_stats(db: Path):
    write_concept(db, slug="a", title="A", body="", source_ids=[], links=[])
    write_concept(db, slug="b", title="B", body="", source_ids=[], links=[])
    mark_certified(db, slug="a", score=0.95, issues=[], suggestions=[])
    mark_certified(db, slug="b", score=0.55, issues=[], suggestions=[])
    stats = certification_stats(db)
    assert stats["total_concepts"] == 2
    assert stats["certified"] == 2
    assert stats["uncertified"] == 0
    assert stats["score_distribution"][">=0.9"] == 1
    assert stats["score_distribution"]["0.5-0.7"] == 1


def test_unmark_certified(db: Path):
    write_concept(db, slug="x", title="X", body="", source_ids=[], links=[])
    mark_certified(db, slug="x", score=0.9, issues=[], suggestions=[])
    unmark_certified(db, "x")
    item = read_concept(db, "x")
    assert item["certified_at"] is None
