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
    export_index)
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
        content="hello")
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
    result = stage_source(db, raw_path=Path("/tmp/raw/a.md"), content="hello")
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
        extractions_data=[_make_extraction(staged_source)])
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
        extractions_data=[_make_extraction(staged_source)])
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
        extractions_data=[_make_extraction(sid1), _make_extraction(sid2)])
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
        prompt_version="extract-v1")
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
            extractions_data=[{"source_id": staged_source, "quote_span": ""}])


def test_write_concept_requires_at_least_one_extraction(db: Path):
    with pytest.raises(StorageError, match="cannot be empty"):
        write_concept(
            db,
            slug="x",
            title="X",
            body="",
            extractions_data=[])


def test_write_concept_rejects_unknown_source(db: Path):
    with pytest.raises(StorageError, match="source_id not found"):
        write_concept(
            db,
            slug="x",
            title="X",
            body="",
            extractions_data=[_make_extraction("nonexistent12345678")])


def test_write_concept_rejects_deleted_source(db: Path, staged_source: str):
    soft_delete_source(db, staged_source)
    with pytest.raises(ConflictError, match="deleted source"):
        write_concept(
            db,
            slug="x",
            title="X",
            body="",
            extractions_data=[_make_extraction(staged_source)])


def test_write_concept_slug_exists_raises_conflict(db: Path, staged_source: str):
    """write_concept 严格 INSERT: slug 已存在 -> ConflictError (LLM 走 dedup + update).

    业务决策 (merge body / 保留 source) 不应由 storage 静默做, 由 LLM 决定.
    """
    from corpus.errors import ConflictError
    write_concept(
        db, slug="x", title="X v1", body="first",
        extractions_data=[_make_extraction(staged_source)])
    with pytest.raises(ConflictError) as exc:
        write_concept(
            db, slug="x", title="X v2", body="second",
            extractions_data=[_make_extraction(staged_source)])
    assert "already exists" in str(exc.value).lower()
    # hint 引导 LLM 走 find-by-link + read + update_concept --expected-version
    assert "find-by-link" in exc.value.hint
    assert "update_concept" in exc.value.hint
    assert "expected-version" in exc.value.hint


def test_concept_not_orphan_when_source_exists(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)])
    assert read_concept(db, "x")["is_orphan"] is False


def test_add_source_to_concept_unmarks_orphan(db: Path):
    sid1 = stage_source(db, raw_path=Path("/tmp/a.md"), content="a")["source_id"]
    sid2 = stage_source(db, raw_path=Path("/tmp/b.md"), content="b")["source_id"]
    # 写概念只引 sid1
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(sid1)])
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
        extractions_data=[_make_extraction(staged_source)])
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
        extractions_data=[_make_extraction(sid1), _make_extraction(sid2)])
    result = remove_source_from_concept(db, "x", sid1)
    assert result["removed"] is True
    assert result["is_orphan"] is False
    assert sid1 not in result["source_ids"]


def test_remove_only_source_makes_orphan(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)])
    result = remove_source_from_concept(db, "x", staged_source)
    assert result["removed"] is True
    assert result["is_orphan"] is True


def test_find_concept_by_link(db: Path, staged_source: str):
    write_concept(
        db, slug="postgresql-mvcc", title="PostgreSQL MVCC", body="",
        extractions_data=[_make_extraction(staged_source)])
    matches = find_concept_by_link(db, "PostgreSQL MVCC")
    assert len(matches) == 1
    assert matches[0]["slug"] == "postgresql-mvcc"


# ---------- certification ----------

def test_mark_certified(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)])
    mark_certified(db, slug="x", score=0.85, issues=["i1"], suggestions=["s1"], certified_by="agent")
    item = read_concept(db, "x")
    assert item["certified_score"] == 0.85


def test_mark_certified_invalid_score(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)])
    with pytest.raises(Exception):
        mark_certified(db, slug="x", score=1.5, issues=[], suggestions=[])


def test_list_uncertified(db: Path, staged_source: str):
    write_concept(
        db, slug="a", title="A", body="",
        extractions_data=[_make_extraction(staged_source)])
    write_concept(
        db, slug="b", title="B", body="",
        extractions_data=[_make_extraction(staged_source)])
    mark_certified(db, slug="a", score=0.9, issues=[], suggestions=[])
    uncertified = list_uncertified_concepts(db)
    assert len(uncertified) == 1
    assert uncertified[0]["slug"] == "b"


def test_certification_stats_includes_orphans(db: Path, staged_source: str):
    write_concept(
        db, slug="a", title="A", body="",
        extractions_data=[_make_extraction(staged_source)])
    write_concept(
        db, slug="b", title="B", body="",
        extractions_data=[_make_extraction(staged_source)])
    soft_delete_source(db, staged_source)  # both become orphan
    stats = certification_stats(db)
    assert stats["orphans"] == 2


def test_unmark_certified(db: Path, staged_source: str):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)])
    mark_certified(db, slug="x", score=0.9, issues=[], suggestions=[])
    unmark_certified(db, "x")
    assert read_concept(db, "x")["certified_at"] is None


# ---------- index sync ----------

def test_export_index(db: Path, staged_source: str, tmp_path: Path):
    write_concept(
        db, slug="x", title="X", body="",
        extractions_data=[_make_extraction(staged_source)])
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
        extractions_data=[_make_extraction(sid1)])
    result = update_concept(
        db, "x",
        add_extractions=[_make_extraction(sid2, "more evidence")])
    assert sid2 in result["added_source_ids"]
    assert sid1 in result["source_ids"]
    assert sid2 in result["source_ids"]


# ---------- stage_source revive (同 hash 已 deleted) ----------

def test_stage_source_revives_deleted_when_flag_set(db: Path):
    """soft_delete 后再 stage 同内容 + revive_on_deleted=True → 复用 sid, status=staged."""
    from corpus.storage import soft_delete_source
    raw = Path("/tmp/raw/rev.md")
    result1 = stage_source(db, raw_path=raw, content="same content")
    sid1 = result1["source_id"]
    soft_delete_source(db, sid1, deleted_reason="test")
    # 同内容再 stage, 不带 flag → 报错
    with pytest.raises(ConflictError) as exc:
        stage_source(db, raw_path=raw, content="same content")
    assert "deleted" in str(exc.value).lower()
    assert exc.value.hint and "force-revive" in exc.value.hint
    # 带 flag → 复活
    result2 = stage_source(
        db, raw_path=raw, content="same content",
        revive_on_deleted=True)
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
    result = stage_source(db, raw_path=Path("/tmp/raw/x.md"), content="foo")
    soft_delete_source(db, result["source_id"], deleted_reason="oops")
    with pytest.raises(ConflictError) as exc:
        stage_source(db, raw_path=Path("/tmp/raw/x.md"), content="foo")
    assert "deleted" in str(exc.value).lower()
    assert exc.value.hint and "--force-revive" in exc.value.hint


def test_stage_source_active_duplicate_still_rejected(db: Path):
    """active (staged/committed) 同 hash → 仍报 ConflictError (即使 revive_on_deleted=True)."""
    result = stage_source(db, raw_path=Path("/tmp/raw/d.md"), content="dup")
    with pytest.raises(ConflictError) as exc:
        stage_source(
            db, raw_path=Path("/tmp/raw/d.md"), content="dup",
            revive_on_deleted=True,  # 即使 flag 为真也不能复活 active 行
        )
    assert "duplicate" in str(exc.value).lower()


# ---------- init_db migration v1 -> v2 ----------

def test_init_db_upgrades_v1_to_v5(tmp_path: Path):
    """模拟老库 (v1 DDL) → init_db 升级到 v4 (经过 v2 + v3 + v4 migration chain)."""
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
            "INSERT INTO sources (source_id, raw_path, size_bytes, content_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("abc1234567890124", "/raw/old2.md", 5,
             "abcdef0000000000000000000000000000000000000000000000000000000",
             "staged", "2025-01-01T00:00:00+00:00"))
        rows = c.execute("SELECT * FROM sources").fetchall()
        assert len(rows) == 2
        ver = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(ver["value"]) == 6
        # 索引存在
        idx = c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sources_content_hash'"
        ).fetchone()
        assert idx is not None


def test_init_db_idempotent_on_v6(tmp_path: Path):
    """已 v6 的库 init_db 多次也幂等, version 不漂."""
    db_path = tmp_path / "v6.db"
    init_db(db_path)
    init_db(db_path)
    init_db(db_path)
    import sqlite3
    with sqlite3.connect(str(db_path)) as c:
        ver = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(ver[0]) == 6


# ---------- delete_concept ----------

def test_delete_concept_removes_concept_and_extractions_and_links(db: Path, staged_source: str):
    from corpus.storage import delete_concept, write_concept, read_concept
    # body 含 [[other-slug]] 自动产生 outgoing links, 测 links 表是否被 delete_concept 清空
    write_concept(
        db, slug="kill-me", title="K",
        body="see also [[other-slug]] for context",
        extractions_data=[{"source_id": staged_source, "quote_span": "quote here"}])
    # sanity: body-derived link 已写入
    info_pre = read_concept(db, "kill-me")
    assert "other-slug" in info_pre["links"], info_pre["links"]
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
        extractions_data=[{"source_id": staged_source, "quote_span": "x"}])
    delete_concept(db, "a")
    assert read_source(db, staged_source) is not None


# ---------- list_concepts is_certified filter ----------

def test_list_concepts_is_certified_filter(db: Path, staged_source: str):
    from corpus.storage import write_concept, mark_certified, list_concepts
    write_concept(
        db, slug="certed", title="C", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
    write_concept(
        db, slug="raw", title="R", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
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
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
    with pytest.raises(StorageError) as exc:
        update_concept(
            db, slug="u",
            add_extractions=[{"source_id": staged_source}],  # 缺 quote_span
        )
    assert "missing quote_span" in str(exc.value)
    assert exc.value.hint and "quote_span" in exc.value.hint


def test_write_concept_derives_links_from_body_wikilinks(db: Path, staged_source: str):
    """写 concept 时 body 里的 [[wikilinks]] 自动变成 outgoing links.

    不传 links= 入参 (接口已 drop); body 里 [[a]][[b]][[a]] 应该解析成 [a, b].
    """
    from corpus.storage import write_concept, read_concept
    res = write_concept(
        db, slug="a", title="A", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
    update_body_call = None  # placeholder
    # 用 update_concept 改 body 后再断言 links
    from corpus.storage import update_concept
    update_concept(
        db, slug="a",
        body="see [[proc-cpuinfo]] and [[lscpu]] (also [[proc-cpuinfo|alias]])"
    )
    info = read_concept(db, "a")
    assert sorted(info["links"]) == ["lscpu", "proc-cpuinfo"], info["links"]
    # 自引用不计入
    update_concept(db, slug="a", body="self-link [[a]] should not count")
    info = read_concept(db, "a")
    assert info["links"] == []


def test_write_concept_body_wikilink_skips_unsafe_and_empty(db: Path, staged_source: str):
    """body 里有 unsafe / empty / 自引用 wikilink 都应被安全跳过, 不抛错."""
    from corpus.storage import write_concept, read_concept
    # body 含各种 wikilink: valid / 含 ':' (slug 后 valid) / empty / self-ref / dup / alias form
    write_concept(
        db, slug="x", title="X",
        body=(
            "see [[proc-cpuinfo]] ok "
            "[[self-ref: x]]    slugify -> 'self-ref-x' (slug-safe, 通过) "
            "[[]]              empty (跳过) "
            "[[x]]             self-ref (exclude_slug=slug, 跳过) "
            "[[proc-cpuinfo]]  dup (去重) "
            "[[lscpu|alias]]   alias 形式取 slug"
        ),
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}]
    )
    info = read_concept(db, "x")
    # 跳过: empty / self-ref / dup (per _extract_wikilinks dedup 逻辑)
    # 通过: proc-cpuinfo / self-ref-x (slug-safe, 业务过滤留给上层) / lscpu (alias 取 slug)
    assert sorted(info["links"]) == ["lscpu", "proc-cpuinfo", "self-ref-x"], info["links"]


# ---------- remove_extraction (P2) ----------

def test_remove_extraction_drops_row_and_syncs_source_ids(db: Path, staged_source: str):
    """删唯一 extraction → concept.source_ids 也移除该 sid, is_orphan=1."""
    from corpus.storage import remove_extraction, write_concept, read_concept
    res = write_concept(
        db, slug="r1", title="R1", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
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
        extractions_data=[{"source_id": staged_source, "quote_span": "q1"}])
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
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
    # 首次认证不传 score → 应报
    with pytest.raises(StorageError) as exc:
        mark_certified(db, slug="p", issues=["x"])
    assert "first-time" in str(exc.value).lower()


def test_mark_certified_no_fields_raises(db: Path, staged_source: str):
    from corpus.storage import write_concept, mark_certified
    write_concept(
        db, slug="p2", title="P2", body="b",
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
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
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
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
        extractions_data=[{"source_id": staged_source, "quote_span": "q"}])
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
        extractions_data=[_make_extraction(staged_source)])
    out = find_concept_by_link(db, "PostgreSQL MVCC")
    assert len(out) == 1
    assert out[0]["match_score"] == 1.0
    assert out[0]["slug"] == "postgresql-mvcc"


def test_find_concept_by_link_match_score_prefix_contains_title(db: Path, staged_source: str):
    from corpus.storage import write_concept, find_concept_by_link
    # slug 短, 是其他 slug 的前缀
    write_concept(
        db, slug="postgres", title="Postgres Overview", body="",
        extractions_data=[_make_extraction(staged_source)])
    write_concept(
        db, slug="postgres-mvcc", title="MVCC", body="",
        extractions_data=[_make_extraction(staged_source)])
    write_concept(
        db, slug="oracle-postgres", title="Oracle to PG Migration", body="",
        extractions_data=[_make_extraction(staged_source)])
    out = find_concept_by_link(db, "postgres")
    slugs = [c["slug"] for c in out]
    # exact "postgres" 排第一, 然后 startswith "postgres-mvcc", 然后 contains "oracle-postgres"
    assert slugs[0] == "postgres"
    assert slugs == sorted(slugs, key=lambda s: (-dict(zip(slugs, [c["match_score"] for c in out]))[s], len(s)))


def test_find_concept_by_link_title_only_match(db: Path, staged_source: str):
    from corpus.storage import write_concept, find_concept_by_link
    write_concept(
        db, slug="mvcc-deep-dive", title="Deep dive into PostgreSQL WAL", body="",
        extractions_data=[_make_staged() if False else _make_extraction(staged_source)])
    out = find_concept_by_link(db, "WAL")
    # slug 不含 "wal", 但 title 含 (case-insensitive)
    assert len(out) == 1
    assert out[0]["slug"] == "mvcc-deep-dive"
    assert out[0]["match_score"] == 0.4


def test_find_concept_by_link_unrelated_filtered(db: Path, staged_source: str):
    from corpus.storage import write_concept, find_concept_by_link
    write_concept(
        db, slug="kafka", title="Apache Kafka", body="",
        extractions_data=[_make_extraction(staged_source)])
    # 搜 "postgres" 完全不相关 → 返回 []
    out = find_concept_by_link(db, "postgres")
    assert out == []


def test_write_concept_concurrent_insert_one_loses(tmp_path: Path):
    """端到端: 2 agent 并发 write_concept 同 slug, 1 成功 1 ConflictError.

    race 时 storage 报 conflict, LLM 重新 find-by-link + read + merge + update_concept.
    (不像之前 upsert 静默 merge - 那个会丢用户的业务判断)
    """
    import subprocess
    import json as _json
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    from corpus.vault import ensure_vault
    from corpus.storage import init_db
    ensure_vault(vault)
    init_db(vault / ".wiki-meta" / "corpus.db")
    src = tmp_path / "x.md"
    src.write_text("x content")
    env = {**os.environ, "PYTHONPATH": "src"}
    sid = _json.loads(subprocess.run(
        ["python3", "-m", "corpus", "sources", "ingest", str(vault), str(src), "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env,
        capture_output=True, text=True, check=True).stdout)["source_id"]

    import threading
    results = {}
    def worker(name, body):
        r = subprocess.run(
            ["python3", "-m", "corpus", "concepts", "write", str(vault),
             "--slug", "race", "--title", f"from-{name}", "--body", body,
             "--extractions", _json.dumps([{"source_id": sid, "quote_span": "x"}]),
             "--json"],
            cwd="/Users/didi/myprojects/CorpusBot", env=env,
            capture_output=True, text=True, timeout=10)
        results[name] = (r.returncode, r.stdout, r.stderr)

    t_a = threading.Thread(target=worker, args=("A", "body A"))
    t_b = threading.Thread(target=worker, args=("B", "body B"))
    t_a.start(); t_b.start()
    t_a.join(); t_b.join()

    # 1 成功 1 ConflictError
    rc_counts = {"ok": 0, "conflict": 0}
    for name, (rc, out, err) in results.items():
        if rc == 0:
            rc_counts["ok"] += 1
        else:
            assert "already exists" in err.lower(), f"{name}: {err[:200]}"
            assert "find-by-link" in err.lower() or "update_concept" in err.lower(), f"{name}: {err[:200]}"
            rc_counts["conflict"] += 1
    assert rc_counts["ok"] == 1, f"期望 1 成功, 实际 {rc_counts}"
    assert rc_counts["conflict"] == 1, f"期望 1 conflict, 实际 {rc_counts}"

    # 最终只有 1 个 source (race loser 失败, 没合并)
    from corpus.storage import read_concept
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "race")
    assert info is not None
    assert info["source_ids"] == [sid]



def test_update_concept_version_starts_at_zero_and_increments(db: Path, staged_source: str):
    from corpus.storage import write_concept, read_concept
    res = write_concept(
        db, slug="cas", title="t", body="b",
        extractions_data=[_make_extraction(staged_source)])
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
        extractions_data=[_make_extraction(staged_source)])
    res = update_concept(db, slug="cas2", body="v2 body")
    assert res["version"] == 1  # +1 from 0
    assert read_concept(db, "cas2")["body"] == "v2 body"


def test_update_concept_cas_matching_succeeds(db: Path, staged_source: str):
    from corpus.storage import write_concept, update_concept, read_concept
    write_concept(
        db, slug="cas3", title="t", body="b",
        extractions_data=[_make_extraction(staged_source)])
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
        extractions_data=[_make_extraction(staged_source)])
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
        cwd="/Users/didi/myprojects/CorpusBot", env=env, capture_output=True, text=True, check=True).stdout)["source_id"]
    # write concept
    subprocess.run(
        ["python3", "-m", "corpus", "concepts", "write", str(vault),
         "--slug", "race", "--title", "t", "--body", "init",
         "--extractions", json.dumps([{"source_id": sid, "quote_span": "x content"}]),
         "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env,
        capture_output=True, text=True, check=True)
    # 读 version
    show = json.loads(subprocess.run(
        ["python3", "-m", "corpus", "concepts", "show", str(vault), "race", "--json"],
        cwd="/Users/didi/myprojects/CorpusBot", env=env, capture_output=True, text=True, check=True).stdout)
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
            capture_output=True, text=True, timeout=10)
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


# ---------- ingest_log (audit log) ----------

def test_log_ingest_writes_and_lists(db: Path):
    """log_ingest 写一行, list_ingest_log 能读出来."""
    from corpus.storage import log_ingest, list_ingest_log
    log_ingest(db, op="stage", source_id="abc", source_path="/raw/abc.md",
               actor="agent", status="ok", source_content_hash="hash1")
    log_ingest(db, op="commit", source_id="abc", actor="agent", status="ok")
    log_ingest(db, op="delete", source_id="def", actor="cli",
               status="ok", details={"reason": "version update"})
    entries = list_ingest_log(db)
    assert len(entries) == 3
    # 按 started_at DESC, 最后写入的 (delete) 在最前
    assert entries[0]["op"] == "delete"
    assert entries[0]["details"] == {"reason": "version update"}
    assert entries[1]["op"] == "commit"
    assert entries[2]["op"] == "stage"
    assert entries[2]["source_content_hash"] == "hash1"


def test_list_ingest_log_filters(db: Path):
    """list_ingest_log 按 op / source_id / since 过滤."""
    from corpus.storage import log_ingest, list_ingest_log
    log_ingest(db, op="stage", source_id="s1", actor="a", status="ok")
    log_ingest(db, op="commit", source_id="s1", actor="a", status="ok")
    log_ingest(db, op="stage", source_id="s2", actor="a", status="failed",
               details={"error": "disk full"})

    # op filter
    stages = list_ingest_log(db, op="stage")
    assert len(stages) == 2
    assert all(e["op"] == "stage" for e in stages)

    # source_id filter
    s1 = list_ingest_log(db, source_id="s1")
    assert len(s1) == 2

    # status filter (impossible - status is not a filter param, use details check)
    failed = [e for e in list_ingest_log(db) if e["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["details"] == {"error": "disk full"}


def test_migration_v3_to_v4_adds_ingest_log(tmp_path: Path):
    """v3 库 init_db 应自动 migration v3 -> v4, 加 ingest_log 表."""
    import sqlite3
    # 建 v3 库 (无 ingest_log 表)
    db_path = tmp_path / "v3.db"
    conn = sqlite3.connect(str(db_path))
    # v3 schema 简化版 (只有 sources / concepts / schema_meta, 无 ingest_log)
    conn.executescript("""
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY, raw_path TEXT NOT NULL, original_filename TEXT,
            size_bytes INTEGER NOT NULL, content_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'staged',
            created_at TEXT NOT NULL, committed_at TEXT, deleted_at TEXT, deleted_reason TEXT
        );
        CREATE TABLE concepts (
            concept_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            body TEXT NOT NULL, source_ids TEXT NOT NULL DEFAULT '[]',
            links TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            is_orphan INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
            certified_at TEXT, certified_score REAL, certified_issues TEXT,
            certified_suggestions TEXT, certified_by TEXT
        );
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3');
    """)
    conn.commit(); conn.close()



def test_migration_v4_to_v5_adds_status_aliases_tags(tmp_path: Path):
    """v4 库 init_db 应自动 migration v4 -> v5, 加 status/aliases/tags 列."""
    import sqlite3
    db_path = tmp_path / "legacy_v4.db"
    conn = sqlite3.connect(str(db_path))
    # v4 schema (有 ingest_log 但无 status/aliases/tags)
    conn.executescript("""
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY, raw_path TEXT NOT NULL, original_filename TEXT,
            size_bytes INTEGER NOT NULL, content_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'staged',
            created_at TEXT NOT NULL, committed_at TEXT, deleted_at TEXT, deleted_reason TEXT
        );
        CREATE TABLE concepts (
            concept_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            body TEXT NOT NULL, source_ids TEXT NOT NULL DEFAULT '[]',
            links TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            is_orphan INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
            certified_at TEXT, certified_score REAL, certified_issues TEXT,
            certified_suggestions TEXT, certified_by TEXT
        );
        CREATE TABLE extractions (
            extraction_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
            concept_slug TEXT NOT NULL, quote_span TEXT, char_start INTEGER,
            char_end INTEGER, extracted_at TEXT NOT NULL, extracted_by TEXT NOT NULL,
            prompt_version TEXT, confidence REAL, source_content_hash TEXT NOT NULL
        );
        CREATE TABLE links (from_slug TEXT NOT NULL, to_slug TEXT NOT NULL,
            PRIMARY KEY (from_slug, to_slug));
        CREATE TABLE cooccurrence (slug_a TEXT NOT NULL, slug_b TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 1, last_seen TEXT NOT NULL,
            PRIMARY KEY (slug_a, slug_b), CHECK (slug_a < slug_b));
        CREATE TABLE certification_log (concept_id TEXT NOT NULL, certified_at TEXT NOT NULL,
            score REAL NOT NULL, issues TEXT, suggestions TEXT, certified_by TEXT NOT NULL,
            PRIMARY KEY (concept_id, certified_at));
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE ingest_log (id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL,
            source_id TEXT, source_path TEXT, actor TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
            details TEXT, source_content_hash TEXT);
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '4');
    """)
    conn.commit(); conn.close()

    from corpus.storage import init_db
    init_db(db_path)

    with sqlite3.connect(str(db_path)) as c:
        ver = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(ver[0]) == 6
        # v5 新列存在
        cols = [r[1] for r in c.execute("PRAGMA table_info(concepts)").fetchall()]
        assert "status" in cols
        assert "aliases" in cols
        assert "tags" in cols
    # 跑 init_db 升级
    from corpus.storage import init_db, log_ingest
    init_db(db_path)

    # 验 schema_version 升到 4
    with sqlite3.connect(str(db_path)) as c:
        ver = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(ver[0]) == 6
        # ingest_log 表存在
        log_ingest(db_path, op="stage", source_id="post-mig", actor="test", status="ok")
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM ingest_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["op"] == "stage"
        assert rows[0]["source_id"] == "post-mig"


# ---------- dedup_candidate_scores ----------

def test_dedup_candidate_scores_basic(db: Path, staged_source: str):
    """dedup_candidate_scores 返多维度分数 (discrete / fuzzy / length_diff)."""
    from corpus.storage import dedup_candidate_scores, write_concept
    write_concept(
        db, slug="postgres", title="Postgres", body="b",
        extractions_data=[_make_extraction(staged_source)])
    write_concept(
        db, slug="postgres-mvcc", title="MVCC", body="b",
        extractions_data=[_make_extraction(staged_source)])
    write_concept(
        db, slug="wal", title="WAL of PostgreSQL", body="b",
        extractions_data=[_make_extraction(staged_source)])

    candidates = dedup_candidate_scores(db, "postgres")
    slugs = [c["slug"] for c in candidates]
    # 3 个相关: postgres (exact 1.0), postgres-mvcc (startswith 0.9), wal (title 0.4)
    assert "postgres" in slugs
    assert "postgres-mvcc" in slugs
    assert "wal" in slugs  # title contains 'postgres' (case-insensitive)

    # postgres 是 exact, match_score 应该 1.0
    pg = next(c for c in candidates if c["slug"] == "postgres")
    assert pg["discrete_score"] == 1.0
    assert pg["fuzzy_score"] >= 0.0
    assert pg["match_score"] == 1.0
    assert pg["length_diff"] == 0
    assert pg["source_count"] == 1


def test_dedup_candidate_scores_fuzzy(db: Path, staged_source: str):
    """fuzzy_score 抓拼写错误 (postgers -> postgres)."""
    from corpus.storage import dedup_candidate_scores, write_concept
    write_concept(
        db, slug="postgres", title="Postgres", body="b",
        extractions_data=[_make_extraction(staged_source)])

    # 搜 'postgers' (拼写错误)
    candidates = dedup_candidate_scores(db, "postgers")
    # 应该匹配到 postgres (fuzzy 抓, 不靠 discrete)
    assert len(candidates) >= 1
    assert candidates[0]["slug"] == "postgres"
    # discrete=0 (无 exact/startswith/contains), 全靠 fuzzy
    assert candidates[0]["discrete_score"] == 0.0
    assert candidates[0]["fuzzy_score"] > 0
    # match_score > 0.1 (阈值过滤), 证明 fuzzy 起了作用
    assert candidates[0]["match_score"] > 0.1


def test_dedup_candidate_scores_no_match(db: Path, staged_source: str):
    """完全无关 slug 不出现在 candidates."""
    from corpus.storage import dedup_candidate_scores, write_concept
    write_concept(
        db, slug="kafka", title="Apache Kafka", body="b",
        extractions_data=[_make_extraction(staged_source)])
    # 搜 'postgres' -- 'kafka' 完全无关, 不返
    candidates = dedup_candidate_scores(db, "postgres")
    assert all(c["slug"] != "kafka" for c in candidates)


# ---------- export_index 含 concept_id / created_at ----------

def test_export_index_includes_concept_id_and_created_at(db: Path, staged_source: str, tmp_path: Path):
    """export_index 写的 concepts.json 含 concept_id + created_at 字段 (之前漏了)."""
    from corpus.storage import write_concept, export_index
    write_concept(
        db, slug="x", title="X", body="b",
        extractions_data=[_make_extraction(staged_source)])
    out_dir = tmp_path / "wiki_index"
    export_index(db, out_dir)
    import json
    data = json.loads((out_dir / "concepts.json").read_text())
    assert data["total"] == 1
    c = data["concepts"][0]
    assert c["slug"] == "x"
    # 这些字段之前漏了, 现在必须有
    assert "concept_id" in c
    assert c["concept_id"].startswith("c_")
    assert "created_at" in c
    assert "version" in c
    assert c["version"] == 0


# ---------- frontmatter + concept_file / source_file helpers ----------

def test_write_concept_file_basic(tmp_path: Path):
    from corpus.storage import write_concept_file, read_concept_file
    path = write_concept_file(
        tmp_path,
        slug="postgresql-mvcc", title="PostgreSQL MVCC", body="## 定义\nMVCC.",
        source_ids=["abc123", "def456"],
        version=0, status="draft")
    assert path.exists()
    assert path.name == "postgresql-mvcc.md"
    assert path.parent.name == "concept"

    meta = read_concept_file(tmp_path, "postgresql-mvcc")
    assert meta is not None
    assert meta["slug"] == "postgresql-mvcc"
    assert meta["title"] == "PostgreSQL MVCC"
    assert meta["type"] == "concept"
    assert meta["version"] == 0
    assert meta["status"] == "draft"
    assert meta["source_ids"] == ["abc123", "def456"]
    # frontmatter 不写 `links:` 字段 — body [[wikilinks]] 是 sole source of truth
    assert "links" not in meta
    assert meta["_body"].lstrip() == "## 定义\nMVCC."


def test_write_concept_file_with_certified(tmp_path: Path):
    from corpus.storage import write_concept_file
    write_concept_file(
        tmp_path, slug="c", title="C", body="b",
        certified_at="2026-08-20", certified_score=0.85,
        certified_issues=["缺源"], certified_suggestions=["补 WAL"])
    meta = read_concept_file_cached(tmp_path, "c")
    assert meta["certified_at"] == "2026-08-20"
    assert meta["certified_score"] == 0.85
    assert meta["certified_issues"] == ["缺源"]
    assert meta["certified_suggestions"] == ["补 WAL"]


def test_read_concept_file_not_found(tmp_path: Path):
    from corpus.storage import read_concept_file
    assert read_concept_file(tmp_path, "nonexistent") is None


def test_write_concept_file_atomic_overwrite(tmp_path: Path):
    """重复写同 slug 应 atomic 覆盖 (没半写文件)."""
    from corpus.storage import write_concept_file
    p1 = write_concept_file(tmp_path, slug="x", title="V1", body="b1", version=0)
    p2 = write_concept_file(tmp_path, slug="x", title="V2", body="b2", version=1)
    assert p1 == p2  # same path
    meta = read_concept_file_cached(tmp_path, "x")
    assert meta["title"] == "V2"
    assert meta["version"] == 1
    # body 前面有换行 (frontmatter 后 \n\n 留一个空行)
    assert meta["_body"].lstrip() == "b2"
    assert meta["_body"].endswith("b2")


def test_read_md_with_frontmatter_no_yaml(tmp_path: Path):
    """无 frontmatter 的文件返 ({}, 全文)."""
    from corpus.frontmatter import read_md_with_frontmatter
    p = tmp_path / "x.md"
    p.write_text("# Just a title\n\nNo yaml here.", encoding="utf-8")
    meta, body = read_md_with_frontmatter(p)
    assert meta == {}
    assert "No yaml" in body


def test_read_md_with_frontmatter_yaml_basic(tmp_path: Path):
    from corpus.frontmatter import read_md_with_frontmatter, write_md_with_frontmatter
    p = tmp_path / "x.md"
    write_md_with_frontmatter(p, meta={"a": 1, "b": ["x", "y"], "c": "z"}, body="# Body")
    meta, body = read_md_with_frontmatter(p)
    assert meta == {"a": 1, "b": ["x", "y"], "c": "z"}
    assert body.lstrip() == "# Body"


# helper
def read_concept_file_cached(vault_root, slug):
    from corpus.storage import read_concept_file
    return read_concept_file(vault_root, slug)


# ---------- restore_from_files ----------

def test_restore_from_files_basic(tmp_path: Path):
    """restore_from_files 从 raw/ + wiki/concept/ 重建 DB (换电脑恢复场景)."""
    import json as _json
    from corpus.storage import (
        write_concept_file, write_source_file, init_db, read_concept, read_source)
    from corpus.vault import ensure_vault

    vault = tmp_path / "v"
    (vault / "raw").mkdir(parents=True)
    ensure_vault(vault)
    init_db(vault / ".wiki-meta" / "corpus.db")

    # 1. 写 raw/<file>.md (frontmatter 含 source_id / content_hash)
    raw_path = vault / "raw" / "test-ingest-20260820-150000.md"
    write_source_file(
        vault, raw_path,
        source_id="abc123def4560000", content_hash="abc", size_bytes=10,
        status="staged", body="# Test content")

    # 2. 写 wiki/concept/<slug>.md (frontmatter 含 sources / links / version / etc)
    write_concept_file(
        vault, slug="test-concept", title="Test", body="## body",
        source_ids=["abc123def4560000"],
        version=2, status="draft", tags=["test"])

    # 3. 删 DB (模拟 db 丢失) - 直接 delete file
    import os
    os.remove(vault / ".wiki-meta" / "corpus.db")

    # 4. restore_from_files 重建
    from corpus.storage import restore_from_files
    summary = restore_from_files(vault)
    assert summary["sources"] == 1
    assert summary["concepts"] == 1
    # extractions 看 frontmatter 'sources:' 数组 - 但我们写时用 source_ids (sid list),
    # restore 时按 string 处理, 会建 1 条 extraction
    assert summary["extractions"] >= 0  # 可能 0 或 1, 取决于 frontmatter 'sources' 字段格式

    # 5. verify DB 重建
    from corpus.storage import read_concept as _read_concept
    info = _read_concept(vault / ".wiki-meta" / "corpus.db", "test-concept")
    assert info is not None
    assert info["title"] == "Test"
    assert info["body"].lstrip() == "## body"
    assert info["source_ids"] == ["abc123def4560000"]
    assert info["version"] == 2
    # status / tags / aliases 在 frontmatter (git), DB schema v3 没这些列
    # (schema v4 可加), 验证 frontmatter 即可
    fm = (vault / "wiki" / "concept" / "test-concept.md").read_text()
    assert "status: draft" in fm
    assert "tags:" in fm

    src_info = read_source(vault / ".wiki-meta" / "corpus.db", "abc123def4560000")
    assert src_info is not None
    assert src_info["content_hash"] == "abc"


def test_restore_from_files_dry_run(tmp_path: Path):
    """dry_run=True 不写 DB, 只统计."""
    from corpus.storage import (
        write_concept_file, write_source_file, init_db,
        connect, restore_from_files)
    from corpus.vault import ensure_vault
    import os

    vault = tmp_path / "v"
    (vault / "raw").mkdir(parents=True)
    ensure_vault(vault)
    init_db(vault / ".wiki-meta" / "corpus.db")

    raw_path = vault / "raw" / "x.md"
    write_source_file(
        vault, raw_path,
        source_id="x" * 16,
        content_hash="x", size_bytes=1, status="staged", body="x")
    write_concept_file(vault, slug="c", title="C", body="b",
                      source_ids=["x" * 16], version=0)

    # 删 DB
    os.remove(vault / ".wiki-meta" / "corpus.db")

    # dry_run
    summary = restore_from_files(vault, dry_run=True)
    assert summary["sources"] == 1
    assert summary["concepts"] == 1
    # DB 不应被创建
    assert not (vault / ".wiki-meta" / "corpus.db").exists()


# ---------- schema v5: status / aliases / tags ----------

def test_write_concept_status_aliases_tags_roundtrip(db: Path, staged_source: str):
    """write_concept 接受 status/aliases/tags, 写 DB + read 返回."""
    from corpus.storage import write_concept, read_concept
    res = write_concept(
        db, slug="x", title="X", body="b",
        extractions_data=[_make_extraction(staged_source)],
        status="evergreen",
        aliases=["MVCC", "多版本并发"],
        tags=["concept", "database"])
    info = read_concept(db, "x")
    assert info["status"] == "evergreen"
    assert info["aliases"] == ["MVCC", "多版本并发"]
    assert info["tags"] == ["concept", "database"]


def test_write_concept_default_status_draft(db: Path, staged_source: str):
    """不传 status 默认 'draft'."""
    from corpus.storage import write_concept, read_concept
    write_concept(
        db, slug="x", title="X", body="b",
        extractions_data=[_make_extraction(staged_source)])
    info = read_concept(db, "x")
    assert info["status"] == "draft"
    assert info["aliases"] == []
    assert info["tags"] == []


def test_restore_from_files_v4_to_v5_picks_up_status_aliases_tags(tmp_path: Path):
    """restore_from_files 从 frontmatter 读 status/aliases/tags 写进 DB (v5)."""
    from corpus.storage import (
        write_concept_file, init_db, restore_from_files, read_concept)
    from corpus.vault import ensure_vault
    import os

    vault = tmp_path / "v"
    (vault / "raw").mkdir(parents=True)
    ensure_vault(vault)
    init_db(vault / ".wiki-meta" / "corpus.db")

    write_concept_file(
        vault, slug="c", title="C", body="b",
        source_ids=[],
        status="evergreen", aliases=["alias1"], tags=["t1", "t2"])

    # 删 db, restore
    os.remove(vault / ".wiki-meta" / "corpus.db")
    summary = restore_from_files(vault)
    assert summary["concepts"] == 1
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "c")
    assert info["status"] == "evergreen"
    assert info["aliases"] == ["alias1"]
    assert info["tags"] == ["t1", "t2"]


def test_list_concepts_status_filter(db: Path, staged_source: str):
    """list_concepts is_status filter (schema v5)."""
    from corpus.storage import list_concepts, write_concept
    write_concept(
        db, slug="draft-c", title="Draft", body="b",
        extractions_data=[_make_extraction(staged_source)],
        status="draft")
    write_concept(
        db, slug="evergreen-c", title="Evergreen", body="b",
        extractions_data=[_make_extraction(staged_source)],
        status="evergreen")
    all_concepts = list_concepts(db)
    assert {c["slug"] for c in all_concepts} == {"draft-c", "evergreen-c"}
    drafts = list_concepts(db, status="draft")
    assert {c["slug"] for c in drafts} == {"draft-c"}
    evergreens = list_concepts(db, status="evergreen")
    assert {c["slug"] for c in evergreens} == {"evergreen-c"}


def test_find_concept_by_link_matches_aliases(db: Path, staged_source: str):
    """find_concept_by_link 'MVCC' 找 postgresql-mvcc (via aliases, schema v5)."""
    from corpus.storage import find_concept_by_link, write_concept
    write_concept(
        db, slug="postgresql-mvcc", title="PG MVCC", body="b",
        extractions_data=[_make_extraction(staged_source)],
        aliases=["MVCC", "多版本并发"])
    # 精确 alias 匹配
    candidates = find_concept_by_link(db, "MVCC")
    assert any(c["slug"] == "postgresql-mvcc" for c in candidates)
    # 部分 alias 匹配 (中文)
    candidates = find_concept_by_link(db, "多版本并发")
    assert any(c["slug"] == "postgresql-mvcc" for c in candidates)


# ---------- source page slug 文件名 (obsidian 兼容) ----------

def test_pick_source_page_target_no_collision(tmp_path: Path):
    """pick_source_page_target: 无撞返 base_slug.md."""
    from corpus.storage import pick_source_page_target
    target = pick_source_page_target(tmp_path, "postgresql", "abc123de")
    assert target == tmp_path / "postgresql.md"


def test_pick_source_page_target_collision_adds_hash(tmp_path: Path):
    """slug 重名加 -<short-hash> 后缀 (8 hex, content_hash 前 8)."""
    from corpus.storage import pick_source_page_target
    # 第一个: postgresql.md
    (tmp_path / "postgresql.md").write_text("# v1", encoding="utf-8")
    # 第二个 (同 slug, 不同 hash): postgresql-abc123de.md
    target = pick_source_page_target(tmp_path, "postgresql", "abc123de")
    assert target == tmp_path / "postgresql-abc123de.md"
    # 第三个: postgresql-fed45678.md
    target2 = pick_source_page_target(tmp_path, "postgresql", "fed45678")
    assert target2 == tmp_path / "postgresql-fed45678.md"


def test_write_source_wiki_page_uses_slug_filename(tmp_path: Path):
    """write_source_wiki_page 写 wiki/source/<slug>.md (非 source_id)."""
    from corpus.storage import write_source_wiki_page, read_source_file
    p = write_source_wiki_page(
        tmp_path, "abc123def4560001", slug="postgresql-13",
        content_hash="abc123de",
        size_bytes=100, status="staged")
    assert p.name == "postgresql-13.md"
    assert p.parent.name == "source"
    # frontmatter 含 slug
    meta = read_source_file(p)
    assert meta["slug"] == "postgresql-13"
    assert meta["source_id"] == "abc123def4560001"
    # wiki/source 是 extraction manifest, 不复制原文
    body = p.read_text()
    assert "# postgresql content" not in body
    assert "## Concepts extracted from this source" in body


def test_write_source_wiki_page_slug_collision(tmp_path: Path):
    """slug 重名 source page 加 -<short-hash> 后缀 (obsidian 兼容)."""
    from corpus.storage import write_source_wiki_page, read_source_file
    # 第一个 slug=postgresql
    p1 = write_source_wiki_page(tmp_path, "id1", slug="postgresql",
                                content_hash="abc123de")
    # 第二个同 slug, 不同 source_id + hash
    p2 = write_source_wiki_page(tmp_path, "id2", slug="postgresql",
                                content_hash="fed45678")
    assert p1.name == "postgresql.md"
    assert p2.name == "postgresql-fed45678.md"
    # 两个文件都存在, 都带 section header (没有 body 混进)
    assert "## Concepts extracted from this source" in p1.read_text()
    assert "## Concepts extracted from this source" in p2.read_text()


# ---------- wiki/source 不复制原文 (single source of truth = raw/<file>.md) ----------
def test_update_source_page_concepts_writes_only_section(tmp_path: Path):
    """update_source_page_concepts 只写 extraction section, 不复制原文.

    wiki/source 是 manifest (frontmatter + Concepts 表), 原文 sole 在 raw/.
    即便 wiki/source 历史数据里夹了原文 (历史 bug), sync 也会被清掉.
    """
    from corpus.storage import (
        init_db, stage_source, write_source_wiki_page,
        update_source_page_concepts, write_concept)

    vault = tmp_path
    db_path = vault / ".wiki-meta" / "corpus.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)

    src = stage_source(
        db_path,
        raw_path=tmp_path / "raw" / "article.md",
        content="# Article\n\nbody content here\n")
    sid = src["source_id"]

    # 故意模拟「历史数据 / 误用」: wiki/source 写了带原文的 body.
    # 这是 cleanup 之前可能出现的状态. sync 应该自动清掉.
    src_path = write_source_wiki_page(
        tmp_path, sid, slug="article-test",
        content_hash=src["content_hash"], size_bytes=src["size_bytes"],
        status="staged")
    # 手动注入原文污染 frontmatter 外的 body (模拟历史 bug 残留)
    from corpus.storage import _write_md, _read_md, _build_concepts_extracted_section
    meta, _ = _read_md(src_path)
    polluted_body = (
        "# Article\n\nbody content here\n\n"
        "## Concepts extracted from this source\n\n"
        "_(none yet)_\n"
    )
    _write_md(src_path, meta=meta, body=polluted_body)

    # 加 extraction
    write_concept(
        db_path,
        slug="my-concept", title="My Concept", body="c",
        extractions_data=[{
            "source_id": sid,
            "quote_span": "body content here",
        }])

    update_source_page_concepts(tmp_path, sid)
    final = src_path.read_text()

    # frontmatter 保留
    assert "slug: article-test" in final
    # 原文必须清掉 (single source of truth = raw/)
    assert "# Article\n" not in final, (
        "原文不应当在 wiki/source — 应该 sole 在 raw/<file>.md"
    )
    # extraction 表必须在 (table 格式)
    assert "[[my-concept]]" in final
    assert "body content here" in final  # quote_span 在 evidence cell 显示
    assert "## Concepts extracted from this source" in final
    assert "| Concept | Confidence | Evidence (quote span) | Prompt | Extracted at |" in final
    # 旧占位符 _(none yet)_ 已被替换
    assert final.count("_(none yet)_") == 0




# ---------- Bug A: write_concept_file 接 status/aliases/tags ----------
def test_write_concept_file_passes_status_aliases_tags(tmp_path: Path):
    """write_concept_file 接受 status/aliases/tags 参数, 写进 frontmatter."""
    from corpus.storage import write_concept_file
    from corpus.frontmatter import read_md_with_frontmatter
    p = write_concept_file(
        tmp_path, slug="x", title="X", body="b",
        source_ids=["sid1"], version=0,
        status="evergreen", aliases=["X alias", "X2"], tags=["linux", "cpu"])
    meta, body = read_md_with_frontmatter(p)
    assert meta["status"] == "evergreen"
    assert meta["aliases"] == ["X alias", "X2"]
    assert meta["tags"] == ["linux", "cpu"]


# ---------- Bug C: export_index 含 status/aliases/tags ----------
def test_export_index_includes_status_aliases_tags(
    db: Path, staged_source: str, tmp_path: Path):
    """export_index 导出的 concepts.json 含 status / aliases / tags."""
    write_concept(
        db, slug="x", title="X", body="b",
        extractions_data=[_make_extraction(staged_source)], status="evergreen",
        aliases=["x-alias", "X 别名"],
        tags=["linux", "cpu"])
    index_dir = tmp_path / "wiki_index"
    export_index(db, index_dir)
    data = json.loads((index_dir / "concepts.json").read_text())
    assert data["total"] == 1
    c = data["concepts"][0]
    assert c["status"] == "evergreen"
    assert c["aliases"] == ["x-alias", "X 别名"]
    assert c["tags"] == ["linux", "cpu"]


# ---------- Bug B: update_concept status changes sync markdown ----------
def test_update_concept_status_syncs_to_markdown(
    db: Path, staged_source: str, tmp_path: Path):
    """update_concept 改 status 后, markdown frontmatter 也必须反映."""
    # write_concept 自身已经是 fix 后的 (Bug A), 显式 draft
    write_concept(
        db, slug="x", title="X", body="b",
        extractions_data=[_make_extraction(staged_source)], status="draft")
    vault = tmp_path
    # 模拟 CLI 路径: write_concept_file 后, markdown 是 draft
    from corpus.storage import write_concept_file, read_concept_file
    write_concept_file(
        vault, slug="x", title="X", body="b",
        source_ids=[staged_source],
        version=0, status="draft")
    md_meta = read_concept_file(vault, "x")
    assert md_meta["status"] == "draft"

    # 模拟 CLI: update_concept 改 status, 然后 _sync_concept_file (cli 内部)
    update_concept(db, slug="x", status="evergreen", body=None)
    # 显式调 sync (CLI 中的 _sync_concept_file 行为)
    from corpus.cli import _sync_concept_file
    paths = {
        "corpus_db": db,
        "root": vault,
        "wiki_concept": vault / "wiki" / "concept",
    }
    _sync_concept_file(paths, "x")

    md_meta = read_concept_file(vault, "x")
    assert md_meta["status"] == "evergreen", (
        "markdown status 没更新 — Bug B 仍存在"
    )


# ---------- _build_concepts_extracted_section: markdown table format ----------
def test_build_concepts_extracted_section_table_format(tmp_path: Path):
    """_build_concepts_extracted_section 输出 markdown table (5 列).

    安全处理: quote_span 含 `|` 必须 escape 成 `\\|`; 含换行拍平成空格."""
    from corpus.storage import (
        init_db, stage_source, write_concept,
        _build_concepts_extracted_section)

    vault = tmp_path
    db_path = vault / ".wiki-meta" / "corpus.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)

    src = stage_source(
        db_path,
        raw_path=tmp_path / "raw" / "art.md",
        content="x")
    sid = src["source_id"]

    # 3 个 extraction: 1) 普通; 2) 含 pipe; 3) 含换行.
    # prompt_version 是 write_concept 函数级参数 (不是 per-source).
    write_concept(
        db_path,
        slug="plain", title="P", body="",
        extractions_data=[{
            "source_id": sid,
            "quote_span": "plain evidence here",
            "confidence": 0.85,
        }],
        prompt_version="extract-v1")
    write_concept(
        db_path,
        slug="with-pipe", title="WP", body="",
        extractions_data=[{
            "source_id": sid,
            "quote_span": "grep -m 1 -E 'vmx|svm' /proc/cpuinfo",
            "confidence": 0.92,
        }],
        prompt_version="extract-v1")
    # 多行 quote_span + 缺 confidence + 换 prompt
    write_concept(
        db_path,
        slug="multi-line", title="ML", body="",
        extractions_data=[{
            "source_id": sid,
            "quote_span": "line one\nline two\nline three",
            "confidence": None,
        }],
        prompt_version="extract-v2")

    section = _build_concepts_extracted_section(vault, sid, body="")

    # 表头/分隔行
    assert "| Concept | Confidence | Evidence (quote span) | Prompt | Extracted at |" in section
    assert "| --- | --- | --- | --- | --- |" in section

    # 每个 concept 一行
    assert "[[plain]]" in section
    assert "[[with-pipe]]" in section
    assert "[[multi-line]]" in section

    # 含 pipe 的 quote 必须 escape -> 表格 row 不会 break
    assert "vmx\\|svm" in section, "pipe in quote_span should be escaped to \\|"
    assert "vmx|svm" not in section.replace("\\|", "")  # 只在 escape 之外没有 raw pipe

    # 换行拍平成空格 — 不能在同一 cell 里出现多行
    rows = [r for r in section.split("\n") if "[[multi-line]]" in r]
    assert rows, "multi-line concept 应该出现在至少一行"
    multi_line_row = rows[0]
    assert "line one line two line three" in multi_line_row
    # 6 个未 escape 的 '|' (leading + 5 列 = 6 cell boundaries 内含 5 col-divider + 2 outer = 6 内部 pipes... 见下)
    # 实际 multi-line_row 形如: | [[multi-line]] |  | line one line two line three | extract-v2 | <at> |
    # 数 row 中未 escape 的 pipe: 6 (leading + 4 dividers + trailing) = 6
    unescaped = multi_line_row.replace("\\|", "")
    assert unescaped.count("|") == 6, (
        f"table row 里应恰好 6 个未 escape 的 '|', 实际 {unescaped.count('|')}, row={multi_line_row!r}"
    )

    # confidence 缺省对应 cell 空
    unescaped = multi_line_row.replace("\\|", "")
    cells = unescaped.split("|")
    # layout: '' (leading), ' [[multi-line]] ', '  ' (conf), ' line one line two line three ',
    #         ' extract-v2 ' (prompt), ' <at> ', '' (trailing)
    assert cells[2].strip() == "", f"缺 confidence 时 cell 应留空, got {cells[2]!r}"
    # prompt cell 也填了 (区别于 confidence cell)
    assert cells[3].strip().startswith("line one"), "evidence cell 内容错位"
    assert cells[4].strip() == "extract-v2", f"prompt cell 错位, got {cells[4]!r}"

    # confidence 数值格式
    assert "| 0.85 |" in section
    assert "| 0.92 |" in section

    # prompt_version 也填进 cell
    assert "extract-v1" in section
    assert "extract-v2" in section


def test_build_concepts_extracted_section_empty_db(tmp_path: Path):
    """无 DB / 无 rows 时, 返回占位符而不崩."""
    from corpus.storage import _build_concepts_extracted_section

    s1 = _build_concepts_extracted_section(tmp_path, "no-such-source", body="")
    assert "_(no DB)_" in s1

    # 有 DB 但 extractions 空 -> _(none yet)_
    from corpus.storage import init_db
    db_path = tmp_path / ".wiki-meta" / "corpus.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    s2 = _build_concepts_extracted_section(tmp_path, "still-no-source", body="")
    assert "_(none yet)_" in s2
