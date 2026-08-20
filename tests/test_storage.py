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


# ---------- stage_source revive (同 hash 已 deleted) ----------

def test_stage_source_revives_deleted_when_flag_set(db: Path):
    """soft_delete 后再 stage 同内容 + revive_on_deleted=True → 复用 sid, status=staged."""
    from corpus_bot.storage import soft_delete_source
    raw = Path("/tmp/raw/rev.md")
    result1 = stage_source(db, raw_path=raw, content="same content", original_filename="rev.md")
    sid1 = result1["source_id"]
    soft_delete_source(db, sid1, deleted_reason="test")
    # 同内容再 stage, 不带 flag → 报错
    with pytest.raises(ConflictError) as exc:
        stage_source(db, raw_path=raw, content="same content", original_filename="rev.md")
    assert "deleted" in str(exc.value).lower()
    assert exc.value.hint and "force-revive" in exc.value.hint
    # 带 flag → 复活
    result2 = stage_source(
        db, raw_path=raw, content="same content", original_filename="rev.md",
        revive_on_deleted=True,
    )
    assert result2["source_id"] == sid1  # 保留 sid
    assert result2["status"] == "staged"
    assert result2["revived"] is True
    row = read_source(db, sid1)
    assert row["status"] == "staged"
    assert row["deleted_at"] is None
    assert row["deleted_reason"] is None


def test_stage_source_deleted_without_flag_raises(db: Path):
    """deleted 同 hash 不带 revive flag → ConflictError, hint 提示 --force-revive."""
    from corpus_bot.storage import soft_delete_source
    result = stage_source(db, raw_path=Path("/tmp/raw/x.md"), content="foo", original_filename="x.md")
    soft_delete_source(db, result["source_id"], deleted_reason="oops")
    with pytest.raises(ConflictError) as exc:
        stage_source(db, raw_path=Path("/tmp/raw/x.md"), content="foo", original_filename="x.md")
    assert "deleted" in str(exc.value).lower()
    assert exc.value.hint and "--force-revive" in exc.value.hint


def test_stage_source_active_duplicate_still_rejected(db: Path):
    """active (staged/committed) 同 hash → 仍报 ConflictError (即使 revive_on_deleted=True)."""
    result = stage_source(db, raw_path=Path("/tmp/raw/d.md"), content="dup", original_filename="d.md")
    with pytest.raises(ConflictError) as exc:
        stage_source(
            db, raw_path=Path("/tmp/raw/d.md"), content="dup", original_filename="d.md",
            revive_on_deleted=True,  # 即使 flag 为真也不能复活 active 行
        )
    assert "duplicate" in str(exc.value).lower()


# ---------- init_db migration v1 -> v2 ----------

def test_init_db_upgrades_v1_to_v2(tmp_path: Path):
    """模拟老库 (v1 DDL 含 UNIQUE(content_hash)) → init_db 应无错升级到 v2."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            raw_path TEXT NOT NULL,
            original_filename TEXT,
            size_bytes INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'staged',
            created_at TEXT NOT NULL,
            committed_at TEXT,
            deleted_at TEXT,
            deleted_reason TEXT,
            UNIQUE(content_hash)
        );
        INSERT INTO sources VALUES
            ('abc1234567890123', '/raw/old.md', 'old.md', 5,
             'abcdef0000000000000000000000000000000000000000000000000000000000',
             'staged', '2025-01-01T00:00:00+00:00', NULL, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()

    # 升级
    init_db(db_path)

    # 验证: UNIQUE 已移除, content_hash 索引已加, 旧行还在, version=2
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        # 没 UNIQUE 了 -> 允许重复 hash 行
        c.execute(
            "INSERT INTO sources (source_id, raw_path, original_filename, size_bytes, content_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("abc1234567890124", "/raw/old2.md", "old.md", 5,
             "abcdef0000000000000000000000000000000000000000000000000000000000",
             "staged", "2025-01-01T00:00:00+00:00"),
        )
        rows = c.execute("SELECT * FROM sources").fetchall()
        assert len(rows) == 2
        ver = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(ver["value"]) == 2
        # 索引存在
        idx = c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sources_content_hash'"
        ).fetchone()
        assert idx is not None


def test_init_db_idempotent_on_v2(tmp_path: Path):
    """已 v2 的库 init_db 多次也幂等, version 不漂."""
    db_path = tmp_path / "v2.db"
    init_db(db_path)
    init_db(db_path)
    init_db(db_path)
    import sqlite3
    with sqlite3.connect(str(db_path)) as c:
        ver = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(ver[0]) == 2
