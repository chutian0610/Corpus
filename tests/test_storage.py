"""Storage 层测试 (覆盖 extractions / orphan / soft_delete / index)。"""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from corpus.storage import (
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
from corpus.errors import ConflictError, StorageError


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


def test_write_concept_upsert_merges_sources(db: Path, staged_source: str):
    """write_concept 改 idempotent upsert: 重复 slug 不报错, 合并 source_ids."""
    res1 = write_concept(
        db, slug="x", title="X v1", body="first",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    assert res1["action"] == "created"
    assert res1["source_ids"] == [staged_source]
    # 准备另一个 source 用来测合并
    from corpus.storage import stage_source
    s2 = stage_source(
        db, raw_path=Path("/tmp/raw/other.md"),
        content="other content", original_filename="other.md",
    )
    res2 = write_concept(
        db, slug="x", title="X v2", body="second",
        extractions_data=[{"source_id": s2["source_id"], "quote_span": "other content"}],
        links=[],
    )
    assert res2["action"] == "updated"
    # source_ids 合并: 旧的 + 新的 (sorted)
    assert sorted(res2["source_ids"]) == sorted([staged_source, s2["source_id"]])
    # title/body 覆盖
    info = read_concept(db, "x")
    assert info["title"] == "X v2"
    assert info["body"] == "second"
    # extractions 有 2 行 (audit history)
    exts = get_concept_evidence_summary(db, "x")
    total = sum(s["n_extractions"] for s in exts["by_source"])
    assert total == 2


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
    from corpus.storage import soft_delete_source
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
    from corpus.storage import soft_delete_source
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

def test_init_db_upgrades_v1_to_v3(tmp_path: Path):
    """模拟老库 (v1 DDL) → init_db 升级到 v3 (经过 v2 + v3 migration)."""
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
        assert int(ver["value"]) == 3
        # 索引存在
        idx = c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sources_content_hash'"
        ).fetchone()
        assert idx is not None


def test_init_db_idempotent_on_v3(tmp_path: Path):
    """已 v3 的库 init_db 多次也幂等, version 不漂."""
    db_path = tmp_path / "v3.db"
    init_db(db_path)
    init_db(db_path)
    init_db(db_path)
    import sqlite3
    with sqlite3.connect(str(db_path)) as c:
        ver = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(ver[0]) == 3


# ---------- delete_concept ----------

def test_delete_concept_removes_concept_and_extractions_and_links(db: Path, staged_source: str):
    from corpus.storage import delete_concept, write_concept, read_concept
    write_concept(
        db, slug="kill-me", title="K", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "quote here"}],
        links=["other-slug"],
    )
    # write_concept 已经 INSERT 了 links (kill-me -> other-slug); 测删除时清掉
    res = delete_concept(db, "kill-me")
    assert res["deleted_concept"] is True
    assert res["deleted_extractions_count"] == 1
    assert res["deleted_links_count"] == 1
    assert read_concept(db, "kill-me") is None
    # extractions 表里也没了
    with sqlite3.connect(str(db)) as c:
        ext_n = c.execute("SELECT COUNT(*) AS n FROM extractions WHERE concept_slug='kill-me'").fetchone()[0]
        link_n = c.execute("SELECT COUNT(*) AS n FROM links WHERE from_slug='kill-me'").fetchone()[0]
    # extractions 和 links 表里都没了 (sqlite3 已在文件顶部 import)
    assert ext_n == 0
    assert link_n == 0


def test_delete_concept_raises_on_unknown_slug(db: Path):
    from corpus.storage import delete_concept
    with pytest.raises(StorageError) as exc:
        delete_concept(db, "nope")
    assert "concept not found" in str(exc.value)


def test_delete_concept_does_not_touch_sources(db: Path, staged_source: str):
    """删 concept 不应影响 source 表 (source 仍可被其它 concept 引用)."""
    from corpus.storage import delete_concept, write_concept, read_source
    write_concept(
        db, slug="a", title="A", body="a",
        extractions_data=[{"source_id": staged_source, "quote_span": "x"}],
        links=[],
    )
    delete_concept(db, "a")
    assert read_source(db, staged_source) is not None


# ---------- list_concepts is_certified filter ----------

def test_list_concepts_is_certified_filter(db: Path, staged_source: str):
    from corpus.storage import write_concept, mark_certified, list_concepts
    write_concept(
        db, slug="certed", title="C", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}],
        links=[],
    )
    write_concept(
        db, slug="raw", title="R", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}],
        links=[],
    )
    mark_certified(db, slug="certed", score=0.9, issues=[], suggestions=[], certified_by="t")

    only_cert = list_concepts(db, is_certified=True)
    only_uncert = list_concepts(db, is_certified=False)
    all_ = list_concepts(db)
    assert {c["slug"] for c in only_cert} == {"certed"}
    assert {c["slug"] for c in only_uncert} == {"raw"}
    assert {c["slug"] for c in all_} == {"certed", "raw"}


# ---------- _validate_links ----------

def test_validate_links_dedups(db: Path):
    from corpus.storage import _validate_links
    out = _validate_links("self", ["a", "b", "a", "c", "b"])
    assert out == ["a", "b", "c"]


def test_validate_links_rejects_self_reference(db: Path):
    from corpus.storage import _validate_links
    with pytest.raises(StorageError) as exc:
        _validate_links("foo", ["bar", "foo"])
    assert "self-reference" in str(exc.value).lower()
    assert exc.value.hint and "self" in exc.value.hint.lower()


def test_validate_links_rejects_non_slug_safe(db: Path):
    from corpus.storage import _validate_links
    with pytest.raises(StorageError) as exc:
        _validate_links("a", ["Bad_Slug", "ok-slug"])
    assert "slug-safe" in str(exc.value).lower()
    assert "Bad_Slug" in str(exc.value)


def test_validate_links_skips_empty(db: Path):
    from corpus.storage import _validate_links
    assert _validate_links("a", ["", "  ", "ok"]) == ["ok"]


# ---------- update_concept quote_span 校验 ----------

def test_update_concept_requires_quote_span(db: Path, staged_source: str):
    """与 write_concept 一致: add_extractions 必须带 quote_span."""
    from corpus.storage import update_concept, write_concept
    write_concept(
        db, slug="u", title="U", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}],
        links=[],
    )
    with pytest.raises(StorageError) as exc:
        update_concept(
            db, slug="u",
            add_extractions=[{"source_id": staged_source}],  # 缺 quote_span
        )
    assert "missing quote_span" in str(exc.value)
    assert exc.value.hint and "quote_span" in exc.value.hint


def test_update_concept_add_links_validates(db: Path, staged_source: str):
    from corpus.storage import update_concept, write_concept
    write_concept(
        db, slug="v", title="V", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}],
        links=[],
    )
    with pytest.raises(StorageError) as exc:
        update_concept(db, slug="v", add_links=["Bad_Slug"])
    assert "slug-safe" in str(exc.value).lower()


def test_write_concept_validates_links(db: Path, staged_source: str):
    from corpus.storage import write_concept
    with pytest.raises(StorageError) as exc:
        write_concept(
            db, slug="x", title="X", body="b",
            extractions_data=[{"source_id": staged_source, "quote_span": "q"}],
            links=["x"],  # self-ref
        )
    assert "self-reference" in str(exc.value).lower()


# ---------- remove_extraction (P2) ----------

def test_remove_extraction_drops_row_and_syncs_source_ids(db: Path, staged_source: str):
    """删唯一 extraction → concept.source_ids 也移除该 sid, is_orphan=1."""
    from corpus.storage import remove_extraction, write_concept, read_concept
    res = write_concept(
        db, slug="r1", title="R1", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}], links=[],
    )
    ext_id = res["extraction_ids"][0]
    out = remove_extraction(db, ext_id)
    assert out["deleted"] is True
    assert out["concept_slug"] == "r1"
    assert out["source_id"] == staged_source
    assert out["concept_source_ids_after"] == []
    assert out["concept_is_orphan_after"] is True
    info = read_concept(db, "r1")
    assert info["is_orphan"] is True
    assert info["source_ids"] == []


def test_remove_extraction_keeps_source_id_when_others_exist(db: Path, staged_source: str):
    """同 (concept, source) 多次抽取, 删一条 → source_id 仍在 (还有别的 extractions 引用)."""
    from corpus.storage import remove_extraction, update_concept, write_concept, read_concept
    res = write_concept(
        db, slug="r2", title="R2", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q1"}], links=[],
    )
    ext_id_1 = res["extraction_ids"][0]
    # 加第二次抽取 (同 sid, 不同 quote)
    update_concept(db, slug="r2",
        add_extractions=[{"source_id": staged_source, "quote_span": "q2"}])
    out = remove_extraction(db, ext_id_1)
    assert out["deleted"] is True
    assert staged_source in out["concept_source_ids_after"]  # 还有 q2 引用
    info = read_concept(db, "r2")
    assert info["is_orphan"] is False


def test_remove_extraction_404(db: Path):
    from corpus.storage import remove_extraction
    with pytest.raises(StorageError) as exc:
        remove_extraction(db, "nonexistent")
    assert "extraction not found" in str(exc.value)


# ---------- mark_certified partial update (P2) ----------

def test_mark_certified_first_time_requires_score(db: Path, staged_source: str):
    from corpus.storage import write_concept, mark_certified
    write_concept(
        db, slug="p", title="P", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}], links=[],
    )
    # 首次认证不传 score → 应报
    with pytest.raises(StorageError) as exc:
        mark_certified(db, slug="p", issues=["x"])
    assert "first-time" in str(exc.value).lower()


def test_mark_certified_no_fields_raises(db: Path, staged_source: str):
    from corpus.storage import write_concept, mark_certified
    write_concept(
        db, slug="p2", title="P2", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}], links=[],
    )
    mark_certified(db, slug="p2", score=0.5, issues=["a"], suggestions=["b"])  # 首次
    # 全 None → 报
    with pytest.raises(StorageError) as exc:
        mark_certified(db, slug="p2")
    assert "no fields" in str(exc.value).lower()


def test_mark_certified_partial_keeps_old_fields(db: Path, staged_source: str):
    """只传 --issues, 旧 score 和 suggestions 应保留."""
    from corpus.storage import write_concept, mark_certified, read_concept
    write_concept(
        db, slug="p3", title="P3", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}], links=[],
    )
    mark_certified(db, slug="p3", score=0.7, issues=["a"], suggestions=["b"])
    # 部分更新: 只改 issues
    res = mark_certified(db, slug="p3", issues=["new-issue"])
    assert res["score"] == 0.7  # 保留
    assert res["issues"] == ["new-issue"]  # 改
    assert res["suggestions"] == ["b"]  # 保留
    assert res["partial_update"] is True
    info = read_concept(db, "p3")
    assert info["certified_score"] == 0.7
    assert info["certified_issues"] == ["new-issue"]
    assert info["certified_suggestions"] == ["b"]


def test_mark_certified_partial_score_only(db: Path, staged_source: str):
    from corpus.storage import write_concept, mark_certified
    write_concept(
        db, slug="p4", title="P4", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}], links=[],
    )
    mark_certified(db, slug="p4", score=0.5, issues=["i"], suggestions=["s"])
    res = mark_certified(db, slug="p4", score=0.9)
    assert res["score"] == 0.9
    assert res["issues"] == ["i"]
    assert res["suggestions"] == ["s"]


# ---------- find_concept_by_link scoring (P2) ----------

def test_find_concept_by_link_match_score_exact(db: Path, staged_source: str):
    from corpus.storage import write_concept, find_concept_by_link
    write_concept(
        db, slug="postgresql-mvcc", title="PostgreSQL MVCC", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    out = find_concept_by_link(db, "PostgreSQL MVCC")
    assert len(out) == 1
    assert out[0]["match_score"] == 1.0
    assert out[0]["slug"] == "postgresql-mvcc"


def test_find_concept_by_link_match_score_prefix_contains_title(db: Path, staged_source: str):
    from corpus.storage import write_concept, find_concept_by_link
    # slug 短, 是其他 slug 的前缀
    write_concept(
        db, slug="postgres", title="Postgres Overview", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    write_concept(
        db, slug="postgres-mvcc", title="MVCC", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    write_concept(
        db, slug="oracle-postgres", title="Oracle to PG Migration", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    out = find_concept_by_link(db, "postgres")
    slugs = [c["slug"] for c in out]
    # exact "postgres" 排第一, 然后 startswith "postgres-mvcc", 然后 contains "oracle-postgres"
    assert slugs[0] == "postgres"
    assert slugs == sorted(slugs, key=lambda s: (-dict(zip(slugs, [c["match_score"] for c in out]))[s], len(s)))


def test_find_concept_by_link_title_only_match(db: Path, staged_source: str):
    from corpus.storage import write_concept, find_concept_by_link
    write_concept(
        db, slug="mvcc-deep-dive", title="Deep dive into PostgreSQL WAL", body="",
        extractions_data=[_make_staged() if False else _make_extraction(staged_source)], links=[],
    )
    out = find_concept_by_link(db, "WAL")
    # slug 不含 "wal", 但 title 含 (case-insensitive)
    assert len(out) == 1
    assert out[0]["slug"] == "mvcc-deep-dive"
    assert out[0]["match_score"] == 0.4


def test_find_concept_by_link_unrelated_filtered(db: Path, staged_source: str):
    from corpus.storage import write_concept, find_concept_by_link
    write_concept(
        db, slug="kafka", title="Apache Kafka", body="",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    # 搜 "postgres" 完全不相关 → 返回 []
    out = find_concept_by_link(db, "postgres")
    assert out == []


def test_write_concept_upsert_concurrent_subprocess(tmp_path: Path):
    """多 agent 并发 write 同一 slug (3 个 subprocess), 都应成功, source_ids 合并."""
    import subprocess
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    from corpus.vault import ensure_vault, vault_paths
    from corpus.storage import init_db
    ensure_vault(vault)
    init_db(vault_paths(vault)["corpus_db"])

    # 准备 3 个 source
    sources = []
    for i in range(3):
        p = tmp_path / f"src{i}.md"
        p.write_text(f"content {i}")
        sources.append(p)

    import json as _json
    env = {**os.environ, "PYTHONPATH": "src"}
    sids = []
    for p in sources:
        r = subprocess.run(
            ["python3", "-m", "corpus", "sources", "ingest", str(vault), str(p), "--json"],
            cwd="/Users/didi/myprojects/CorpusBot", env=env,
            capture_output=True, text=True, check=True,
        )
        sids.append(_json.loads(r.stdout)["source_id"])

    # 3 个 agent 并发 write 同一 slug (不同 title/body/source)
    import threading
    results = {}
    def worker(name, sid):
        r = subprocess.run(
            ["python3", "-m", "corpus", "concepts", "write", str(vault),
             "--slug", "shared", "--title", f"from-{name}",
             "--body", f"body from {name}",
             "--extractions", _json.dumps([{"source_id": sid, "quote_span": f"content {name}"}]),
             "--json"],
            cwd="/Users/didi/myprojects/CorpusBot", env=env,
            capture_output=True, text=True, timeout=15,
        )
        results[name] = (r.returncode, r.stdout, r.stderr)

    threads = [threading.Thread(target=worker, args=(f"agent-{i}", sids[i])) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()

    # 3 个都成功
    for name, (rc, out, err) in sorted(results.items()):
        assert rc == 0, f"{name} failed: {err[:200]}"

    # 最终 concept 包含 3 个 source
    from corpus.storage import read_concept
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "shared")
    assert info is not None
    assert sorted(info["source_ids"]) == sorted(sids), (
        f"期望 {sorted(sids)}, 实际 {sorted(info['source_ids'])}"
    )
    # title/body 是某个 agent 的 (last-writer-wins)
    assert info["title"].startswith("from-agent-")


# ---------- update_concept CAS (optimistic concurrency control) ----------

def test_update_concept_version_starts_at_zero_and_increments(db: Path, staged_source: str):
    from corpus.storage import write_concept, read_concept
    res = write_concept(
        db, slug="cas", title="t", body="b",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    # write_concept 后 version 应为 1 (或 0? 取决于是否 +1)
    info = read_concept(db, "cas")
    assert "version" in info
    # write_concept 是 INSERT, version 保持 0 (不变); update_concept 才 +1
    assert info["version"] == 0


def test_update_concept_no_expected_version_succeeds(db: Path, staged_source: str):
    """不传 expected_version = last-write-wins 行为 (向后兼容)."""
    from corpus.storage import write_concept, update_concept, read_concept
    write_concept(
        db, slug="cas2", title="v1", body="b",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    res = update_concept(db, slug="cas2", body="v2 body")
    assert res["version"] == 1  # +1 from 0
    assert read_concept(db, "cas2")["body"] == "v2 body"


def test_update_concept_cas_matching_succeeds(db: Path, staged_source: str):
    from corpus.storage import write_concept, update_concept, read_concept
    write_concept(
        db, slug="cas3", title="t", body="b",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    # 读 version, 传 expected_version=0 匹配 → 应成功
    info = read_concept(db, "cas3")
    res = update_concept(db, slug="cas3", body="new", expected_version=info["version"])
    assert res["version"] == 1
    assert read_concept(db, "cas3")["body"] == "new"


def test_update_concept_cas_mismatch_raises(db: Path, staged_source: str):
    """CAS 失败: 另一 agent 已经 update 过, expected_version 不匹配 → OptimisticLockError."""
    from corpus.errors import OptimisticLockError
    from corpus.storage import write_concept, update_concept, read_concept
    write_concept(
        db, slug="cas4", title="t", body="b",
        extractions_data=[_make_extraction(staged_source)], links=[],
    )
    info = read_concept(db, "cas4")
    # 模拟另一 agent 先 update
    update_concept(db, slug="cas4", body="other agent's body")
    # 现在原 agent 用 stale expected_version (0) 再 update → CAS fail
    with pytest.raises(OptimisticLockError) as exc:
        update_concept(db, slug="cas4", body="my body", expected_version=info["version"])
    assert "concurrently" in str(exc.value).lower()
    # hint 提示重新 read
    assert "read_concept" in (exc.value.hint or "")


def test_update_concept_cas_concurrent_subprocess(tmp_path: Path):
    """端到端: 2 agent 同时 update 同一 concept, 第二个 CAS fail."""
    import subprocess, json
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    from corpus.vault import ensure_vault
    from corpus.storage import init_db
    ensure_vault(vault)
    init_db(vault / ".wiki-meta" / "corpus.db")
    src = tmp_path / "x.md"
    src.write_text("x content")
    env = {**os.environ, "PYTHONPATH": "src"}
    sid = json.loads(subprocess.run(
        ["python3", "-m", "corpus", "sources", "ingest", str(vault), str(src), "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env, capture_output=True, text=True, check=True,
    ).stdout)["source_id"]
    # write concept
    subprocess.run(
        ["python3", "-m", "corpus", "concepts", "write", str(vault),
         "--slug", "race", "--title", "t", "--body", "init",
         "--extractions", json.dumps([{"source_id": sid, "quote_span": "x content"}]),
         "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env,
        capture_output=True, text=True, check=True,
    )
    # 读 version
    show = json.loads(subprocess.run(
        ["python3", "-m", "corpus", "concepts", "show", str(vault), "race", "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env, capture_output=True, text=True, check=True,
    ).stdout)
    v = show["version"]

    # agent A 用 v 调 update (成功, version+1)
    # agent B 同时用 v 调 update (CAS fail, OptimisticLockError)
    import threading
    results = {}
    def worker(name, body):
        r = subprocess.run(
            ["python3", "-m", "corpus", "concepts", "update", str(vault), "race",
             "--body", body, "--expected-version", str(v), "--json"],
            cwd="/Users/didi/myprojects/CorpusBot", env=env,
            capture_output=True, text=True, timeout=10,
        )
        results[name] = (r.returncode, r.stdout, r.stderr)

    t_a = threading.Thread(target=worker, args=("A", "agent A body"))
    t_b = threading.Thread(target=worker, args=("B", "agent B body"))
    t_a.start(); t_b.start()
    t_a.join(); t_b.join()

    # 恰好 1 成功 1 CAS fail
    rc_counts = {}
    for name, (rc, out, err) in results.items():
        if rc == 0:
            rc_counts["ok"] = rc_counts.get("ok", 0) + 1
        else:
            assert "concurrently" in err.lower(), f"{name} err: {err}"
            rc_counts["cas_fail"] = rc_counts.get("cas_fail", 0) + 1
    assert rc_counts.get("ok") == 1, f"期望 1 成功, 实际 {rc_counts}"
    assert rc_counts.get("cas_fail") == 1, f"期望 1 CAS fail, 实际 {rc_counts}"
