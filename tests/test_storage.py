"""Storage 层测试 (覆盖 extractions / orphan / soft_delete / index)。"""

import json
from pathlib import Path

import pytest

from corpus_bot.storage import (
    init_db,
    stage_source,
    commit_source,
    soft_delete_source,
    dry_run_delete_source,
    write_concept,
    update_concept,
    add_source_to_concept,
    remove_source_from_concept,
    read_concept,
    read_source,
    list_sources,
    list_concepts,
    list_uncertified_concepts,
    find_concept_by_link,
    mark_certified,
    unmark_certified,
    certification_stats,
    get_concept_evidence,
    get_concept_evidence_summary,
    export_index,
)
from corpus_bot.errors import ConflictError, StorageError


@pytest.fixture
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def staged_source(db: Path) -> str:
    """stage 一个 source 并返回 source_id。"""
    result = stage_source(
        db,
        raw_path=Path("/tmp/raw/a.md"),
        content="hello",
        original_filename="a.md",
    )
    return result["source_id"]


def _make_extraction(source_id: str, quote: str = "Each row carries xmin/xmax") -> dict:
    return {
        "source_id": source_id,
        "quote_span": quote,
        "char_start": 0,
        "char_end": len(quote),
    }


# ---------- sources ----------

def test_stage_source_returns_metadata(db: Path):
    result = stage_source(db, raw_path=Path("/tmp/raw/a.md"), content="hello", original_filename="a.md")
    assert result["status"] == "staged"
    assert len(result["source_id"]) == 16


def test_stage_source_duplicate_rejected(db: Path):
    stage_source(db, raw_path=Path("/tmp/raw/a.md"), content="hello")
    with pytest.raises(ConflictError):
        stage_source(db, raw_path=Path("/tmp/raw/b.md"), content="hello")


def test_commit_source(db: Path, staged_source: str):
    commit_source(db, staged_source)
    assert read_source(db, staged_source)["status"] == "committed"


def test_commit_deleted_source_blocked(db: Path, staged_source: str):
    soft_delete_source(db, staged_source)
    with pytest.raises(ConflictError):
        commit_source(db, staged_source)


def test_soft_delete_marks_status(db: Path, staged_source: str):
    result = soft_delete_source(db, staged_source, deleted_reason="test-cleanup")
    assert result["source_id"] == staged_source
    assert result["deleted_at"]
    item = read_source(db, staged_source)
    assert item["status"] == "deleted"
    assert item["deleted_reason"] == "test-cleanup"


def test_soft_delete_removes_from_concept_sources(db: Path, staged_source: str):
    # 写一个引用这个 source 的 concept
    write_concept(
        db,
        slug="x",
        title="X",
        body="body",
        extractions_data=[_make_extraction(staged_source)],
        links=[],
    )
    # 软删 source
    result = soft_delete_source(db, staged_source)
    assert result["orphans_created"] == 1  # concept 变 orphan

    concept = read_concept(db, "x")
    assert staged_source not in concept["source_ids"]
    assert concept["is_orphan"] is True


def test_soft_delete_double_call_blocked(db: Path, staged_source: str):
    soft_delete_source(db, staged_source)
    with pytest.raises(ConflictError):
        soft_delete_source(db, staged_source)


def test_dry_run_delete_no_concept_impact(db: Path, staged_source: str):
    result = dry_run_delete_source(db, staged_source)
    assert result["affected_concepts_count"] == 0
    assert "safe to delete" in result["recommendation"]


def test_dry_run_delete_with_orphan_warning(db: Path, staged_source: str):
    write_concept(
        db,
        slug="x",
        title="X",
        body="body",
        extractions_data=[_make_extraction(staged_source)],
        links=[],
    )
    result = dry_run_delete_source(db, staged_source)
    assert result["affected_concepts_count"] == 1
    assert "x" in result["would_become_orphans"]


def test_dry_run_delete_multi_source_concept_safe(db: Path):
    sid1 = stage_source(db, raw_path=Path("/tmp/a.md"), content="a").get("source_id") or stage_source(db, raw_path=Path("/tmp/a.md"), content="a")["source_id"]
    # 不同内容 sid2
    sid2 = stage_source(db, raw_path=Path("/tmp/b.md"), content="b")["source_id"]
    write_concept(
        db,
        slug="x",
        title="X",
        body="body",
        extractions_data=[_make_extraction(sid1), _make_extraction(sid2)],
        links=[],
    )
    # 删 sid1，concept 还有 sid2 支撑
    result = dry_run_delete_source(db, sid1)
    assert result["affected_concepts_count"] == 1
    assert result["would_become_orphans"] == []
    assert result["still_supported"][0]["slug"] == "x"
    assert sid2 in result["still_supported"][0]["remaining_sources"]


# ---------- concepts + extractions ----------

def test_write_concept_basic(db: Path, staged_source: str):
    result = write_concept(
        db,
        slug="postgresql-mvcc",
        title="PostgreSQL MVCC",
        body="MVCC explanation",
        extractions_data=[_make_extraction(staged_source)],
        links=["postgres-transactions"],
        prompt_version="extract-v1",
    )
    assert result["slug"] == "postgresql-mvcc"
    assert len(result["extraction_ids"]) == 1
    assert result["source_ids"] == [staged_source]

    # extractions 表有 1 行
    evidence = get_concept_evidence(db, "postgresql-mvcc", staged_source)
    assert evidence is not None
    assert len(evidence["extractions"]) == 1
    assert evidence["extractions"][0]["quote_span"].startswith("Each row")


def test_write_concept_requires_quote_span(db: Path, staged_source: str):
    with pytest.raises(StorageError, match="quote_span"):
        write_concept(
            db,
            slug="x",
            title="X",
            body="",
            extractions_data=[{"source_id": staged_source, "quote_span": ""}],
            links=[],
        )


def test_write_concept_requires_at_least_one_extraction(db: Path):
    with pytest.raises(StorageError, match="cannot be empty"):
        write_concept(
            db,
            slug="x",
            title="X",
            body="",
            extractions_data=[],
            links=[],
        )


def test_write_concept_rejects_unknown_source(db: Path):
    with pytest.raises(StorageError, match="source_id not found"):
        write_concept(
            db,
            slug="x",
            title="X",
            body="",
            extractions_data=[_make_extraction("nonexistent12345678")],
            links=[],
        )


def test_write_concept_rejects_deleted_source(db: Path, staged_source: str):
    soft_delete_source(db, staged_source)
    with pytest.raises(ConflictError, match="deleted source"):
        write_concept(
            db,
            slug="x",
            title="X",
            body="",
            extractions_data=[_make_extraction(staged_source)],
            links=[],
        )


def test_write_concept_slug_conflict(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    with pytest.raises(ConflictError):
        write_concept(
            db, slug="x", title="X v2", body="",
            extractions_data=[_make_extraction(staged_source)], links=[],
        )


def test_concept_not_orphan_when_source_exists(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    assert read_concept(db, "x")["is_orphan"] is False


def test_add_source_to_concept_unmarks_orphan(db: Path):
    sid1 = stage_source(db, raw_path=Path("/tmp/a.md"), content="a")["source_id"]
    sid2 = stage_source(db, raw_path=Path("/tmp/b.md"), content="b")["source_id"]
    # 写概念只引 sid1
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(sid1)], links=[],
    )
    # 删 sid1 → orphan
    soft_delete_source(db, sid1)
    assert read_concept(db, "x")["is_orphan"] is True

    # 加 sid2 → 解除 orphan
    result = add_source_to_concept(db, "x", sid2, quote_span="more text")
    assert result["added"] is True
    assert result["is_orphan"] is False
    assert sid2 in result["source_ids"]
    assert read_concept(db, "x")["is_orphan"] is False


def test_add_source_to_concept_dedup(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    # 重复 add 同一 source
    result = add_source_to_concept(db, "x", staged_source, quote_span="another quote")
    assert result["added"] is False
    # 但 extractions 表加了 1 行（audit history）
    evidence = get_concept_evidence(db, "x", staged_source)
    assert len(evidence["extractions"]) == 2


def test_remove_source_from_concept(db: Path):
    sid1 = stage_source(db, raw_path=Path("/tmp/a.md"), content="a")["source_id"]
    sid2 = stage_source(db, raw_path=Path("/tmp/b.md"), content="b")["source_id"]
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(sid1), _make_extraction(sid2)], links=[],
    )
    result = remove_source_from_concept(db, "x", sid1)
    assert result["removed"] is True
    assert result["is_orphan"] is False
    assert sid1 not in result["source_ids"]


def test_remove_only_source_makes_orphan(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    result = remove_source_from_concept(db, "x", staged_source)
    assert result["removed"] is True
    assert result["is_orphan"] is True


def test_find_concept_by_link(db: Path, staged_source: str):
    write_concept(
        db, slug="postgresql-mvcc", title="PostgreSQL MVCC", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    matches = find_concept_by_link(db, "PostgreSQL MVCC")
    assert len(matches) == 1
    assert matches[0]["slug"] == "postgresql-mvcc"


# ---------- certification ----------

def test_mark_certified(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    mark_certified(db, slug="x", score=0.85, issues=["i1"], suggestions=["s1"], certified_by="agent")
    item = read_concept(db, "x")
    assert item["certified_score"] == 0.85


def test_mark_certified_invalid_score(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    with pytest.raises(Exception):
        mark_certified(db, slug="x", score=1.5, issues=[], suggestions=[])


def test_list_uncertified(db: Path, staged_source: str):
    write_concept(
        db, slug="a", title="A", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    write_concept(
        db, slug="b", title="B", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    mark_certified(db, slug="a", score=0.9, issues=[], suggestions=[])
    uncertified = list_uncertified_concepts(db)
    assert len(uncertified) == 1
    assert uncertified[0]["slug"] == "b"


def test_certification_stats_includes_orphans(db: Path, staged_source: str):
    write_concept(
        db, slug="a", title="A", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    write_concept(
        db, slug="b", title="B", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    soft_delete_source(db, staged_source)  # both become orphan
    stats = certification_stats(db)
    assert stats["orphans"] == 2


def test_unmark_certified(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    mark_certified(db, slug="x", score=0.9, issues=[], suggestions=[])
    unmark_certified(db, "x")
    assert read_concept(db, "x")["certified_at"] is None


# ---------- index sync ----------

def test_export_index(db: Path, staged_source: str, tmp_path: Path):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    index_dir = tmp_path / "wiki_index"
    result = export_index(db, index_dir)
    assert Path(result["concepts_json"]).exists()
    assert Path(result["sources_json"]).exists()
    concepts_data = json.loads(Path(result["concepts_json"]).read_text())
    assert concepts_data["total"] == 1
    sources_data = json.loads(Path(result["sources_json"]).read_text())
    assert sources_data["total"] == 1


# ---------- update_concept (legacy API)----------

def test_update_concept_add_extractions(db: Path):
    sid1 = stage_source(db, raw_path=Path("/tmp/a.md"), content="a")["source_id"]
    sid2 = stage_source(db, raw_path=Path("/tmp/b.md"), content="b")["source_id"]
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(sid1)], links=[],
    )
    result = update_concept(
        db, "x",
        add_extractions=[_make_extraction(sid2, "more evidence")],
    )
    assert sid2 in result["added_source_ids"]
    assert sid1 in result["source_ids"]
    assert sid2 in result["source_ids"]
