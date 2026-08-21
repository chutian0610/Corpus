"""CLI 层测试 (sources ingest / batch 端到端, 撞名 + revive)."""

from pathlib import Path

import json

import pytest
import sqlite3
from click.testing import CliRunner

from corpus.cli import cli
from corpus.errors import ConflictError, ValidationError


@pytest.fixture
def external(tmp_path: Path) -> Path:
    """vault 外的源文件目录 (corpus-bot ingest 必须从 vault 外拉)."""
    e = tmp_path / "external"
    e.mkdir(parents=True)
    return e


@pytest.fixture
def vault(tmp_path: Path, external: Path) -> Path:
    """新建一个空 vault, raw/ 已建好."""
    v = tmp_path / "vault"
    (v / "raw").mkdir(parents=True)
    # vault init 会建 wiki/ .wiki-meta/
    from corpus.vault import ensure_vault, vault_paths
    from corpus.storage import init_db
    ensure_vault(v)
    init_db(vault_paths(v)["corpus_db"])
    return v


def _runner():
    # click 8.3: stderr 不再混入 stdout, 也不再有 mix_stderr 参数
    return CliRunner()


# ---------- sources ingest 撞名修复 ----------

def test_batch_handles_already_ingested_source(vault: Path, external: Path):
    """batch: 已 ingest 过的 source, 再 batch 同 hash 目录 -> 跳过 (duplicate)."""
    import re
    r = _runner()

    # 步骤 1: 外部放 v1
    (external / "note.md").write_text("# version 1\n", encoding="utf-8")
    # ingest v1
    res = r.invoke(cli, [
        "sources", "add", str(vault), str(external / "note.md"), "--json",
    ])
    assert res.exit_code == 0

    # 步骤 2: batch 一个含同 v1 的目录 + 一个新文件 v2 (src_dir 必须在 vault 外, 同 ingest 校验)
    src_dir = external / "incoming2"
    src_dir.mkdir()
    (src_dir / "note.md").write_text("# version 1\n", encoding="utf-8")  # 同 v1
    (src_dir / "other.md").write_text("# version 2\n", encoding="utf-8")  # 新

    res = r.invoke(cli, [
        "sources", "add", str(vault), str(src_dir), "--json",
    ])
    assert res.exit_code == 0, res.stderr
    assert '"staged": 1' in res.output      # other.md 新入库
    assert '"duplicates": 1' in res.output   # note.md 同 hash 跳过

    # raw/ 下应该有 2 个文件 (v1 的 ingest-<ts> + v2 的 ingest-<ts>)
    raw_files = sorted(p.name for p in (vault / "raw").iterdir() if p.name != ".tmp")
    assert len(raw_files) == 2
    for f in raw_files:
        assert re.search(r"ingest-\d{8}-\d{6}", Path(f).stem)

    # batch 输出应含 staged=1, 无 failed
    assert '"staged": 1' in res.output
    assert '"failed": 0' in res.output


def test_ingest_collision_same_content_keeps_name(vault: Path, external: Path):
    """同 hash 撞名 → 不改名 (覆写 idempotent)."""
    r = _runner()
    content = "# same\n"
    (external / "note.md").write_text(content, encoding="utf-8")
    res = r.invoke(cli, [
        "sources", "add", str(vault), str(external / "note.md"), "--json",
    ])
    assert res.exit_code == 0
    sid1 = res.output.strip().split('"source_id": "')[1].split('"')[0]

    # 同内容再 ingest (即使是覆写过的同名)
    res = r.invoke(cli, [
        "sources", "add", str(vault), str(external / "note.md"), "--json",
    ])
    # 同 hash active → ConflictError → exit 1
    assert res.exit_code == 1
    assert "duplicate" in res.stderr.lower()


# ---------- sources ingest --force-revive ----------

def test_ingest_revive_deleted_source(vault: Path, external: Path):
    """soft_delete 后再 ingest 同内容 + --force-revive → 复活, 保留 sid."""
    from corpus.storage import soft_delete_source

    r = _runner()
    (external / "x.md").write_text("same content", encoding="utf-8")

    # 第一次入库
    res = r.invoke(cli, [
        "sources", "add", str(vault), str(external / "x.md"), "--json",
    ])
    assert res.exit_code == 0
    sid = res.output.strip().split('"source_id": "')[1].split('"')[0]

    # 软删
    soft_delete_source(vault / ".wiki-meta" / "corpus.db", sid, deleted_reason="test")

    # 同内容再 ingest, 不带 flag → exit 1 + hint
    res = r.invoke(cli, [
        "sources", "add", str(vault), str(external / "x.md"), "--json",
    ])
    assert res.exit_code == 1
    assert "deleted" in res.stderr.lower()
    assert "--force-revive" in (res.stderr or "")

    # 带 flag → 复活, 保留 sid
    res = r.invoke(cli, [
        "sources", "add", str(vault), str(external / "x.md"),
        "--force-revive", "--json",
    ])
    assert res.exit_code == 0, res.stderr
    assert '"action": "revived"' in res.output
    revived_sid = res.output.strip().split('"source_id": "')[1].split('"')[0]
    assert revived_sid == sid


def test_ingest_revive_then_relist_as_staged(vault: Path, external: Path):
    """复活后 list 能看到 status=staged."""
    from corpus.storage import soft_delete_source, read_source

    r = _runner()
    (external / "y.md").write_text("y content", encoding="utf-8")
    res = r.invoke(cli, ["sources", "add", str(vault), str(external / "y.md"), "--json"])
    sid = res.output.strip().split('"source_id": "')[1].split('"')[0]
    soft_delete_source(vault / ".wiki-meta" / "corpus.db", sid)
    r.invoke(cli, [
        "sources", "add", str(vault), str(external / "y.md"),
        "--force-revive", "--json",
    ])
    row = read_source(vault / ".wiki-meta" / "corpus.db", sid)
    assert row["status"] == "staged"
    assert row["deleted_at"] is None


# ---------- sources batch --force-revive + 撞名 ----------

def test_batch_renames_and_counts_revived(vault: Path, external: Path):
    """batch: 撞名改名 + 复活 计数都正确."""
    from corpus.storage import soft_delete_source, read_source, stage_source

    r = _runner()
    db = vault / ".wiki-meta" / "corpus.db"

    # 准备: 直接 stage 一个 source, 然后软删 (供 batch 复活)
    res = stage_source(
        db, raw_path=vault / "raw" / "ghost.md",
        content="ghost content")
    ghost_sid = res["source_id"]
    soft_delete_source(db, ghost_sid)

    # batch 目录: 同名碰撞 + ghost 复活
    # batch 现在也走 outside-vault check: src_dir 必须在 vault 外
    src_dir = external
    (src_dir / "note.md").write_text("# note v1\n", encoding="utf-8")
    (src_dir / "ghost.md").write_text("ghost content", encoding="utf-8")
    # 注意: batch 现在每个文件都生成 -ingest-<ts> 后缀, 无需撞名检测
    res = r.invoke(cli, [
        "sources", "add", str(vault), str(src_dir),
        "--force-revive", "--json",
    ])
    assert res.exit_code == 0, res.stderr
    out = res.output
    assert '"staged": 1' in out      # note.md v1 (raw 里没有同名)
    assert '"revived": 1' in out      # ghost.md 复活
    assert '"duplicates": 0' in out
    assert '"failed": 0' in out

    # ghost 已复活, sid 保留
    row = read_source(db, ghost_sid)
    assert row["status"] == "staged"


def test_batch_without_revive_flags_deleted_as_failed(vault: Path, external: Path):
    """batch 不带 --force-revive, 同 hash 已 deleted → failed, 给出 hint."""
    from corpus.storage import soft_delete_source, stage_source

    r = _runner()
    db = vault / ".wiki-meta" / "corpus.db"
    res = stage_source(
        db, raw_path=vault / "raw" / "z.md",
        content="z content")
    soft_delete_source(db, res["source_id"])

    src_dir = external
    (src_dir / "z.md").write_text("z content", encoding="utf-8")

    res = r.invoke(cli, [
        "sources", "add", str(vault), str(src_dir), "--json",
    ])
    assert res.exit_code == 0  # batch 整体不失败
    assert '"failed": 1' in res.output
    assert '"hint"' in res.output
    assert "--force-revive" in res.output


# ---------- concepts list 过滤 ----------

def test_concepts_list_orphans_filter(vault: Path, external: Path):
    """--orphans 只返回 source_ids=[] 的 concept."""
    from corpus.storage import write_concept, init_db
    init_db(vault / ".wiki-meta" / "corpus.db")

    # 需要先 ingest 一个 source 给非 orphan concept
    (external / "x.md").write_text("alpha", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="with-src", title="With", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "alpha"}])
    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="orphan-1", title="Orphan1", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "alpha"}])
    # 让 orphan-1 变 orphan: remove_source
    _runner().invoke(cli, ["concepts", "unlink", str(vault), "orphan-1", "--source", sid])

    res = _runner().invoke(cli, ["concepts", "list", str(vault), "--orphans", "--json"])
    items = json.loads(res.output)
    assert {c["slug"] for c in items} == {"orphan-1"}


def test_concepts_list_certified_filters(vault: Path, external: Path):
    """--certified / --uncertified 互斥, 默认无过滤."""
    from corpus.storage import write_concept, mark_certified, init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("a", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="certed", title="C", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "a"}])
    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="raw", title="R", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "a"}])
    mark_certified(vault / ".wiki-meta" / "corpus.db", slug="certed", score=0.9, issues=[], suggestions=[])

    only_cert = json.loads(_runner().invoke(cli, ["concepts", "list", str(vault), "--certified", "--json"]).output)
    only_uncert = json.loads(_runner().invoke(cli, ["concepts", "list", str(vault), "--uncertified", "--json"]).output)
    assert {c["slug"] for c in only_cert} == {"certed"}
    assert {c["slug"] for c in only_uncert} == {"raw"}

    # 同时传 --certified --uncertified 应报错
    res = _runner().invoke(cli, ["concepts", "list", str(vault), "--certified", "--uncertified", "--json"])
    assert res.exit_code == 1
    assert "mutually exclusive" in (res.stderr or "")


# ---------- concepts update CLI ----------

def test_concepts_update_changes_body_and_adds_source(vault: Path, external: Path):
    """update: --body 覆盖, --add-extractions 加新 source, --add-links 加新 link."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")

    # 准备两个 source
    (external / "a.md").write_text("alpha", encoding="utf-8")
    (external / "b.md").write_text("beta", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "a.md"), "--json"])
    sid_a = json.loads(res.output)["source_id"]
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "b.md"), "--json"])
    sid_b = json.loads(res.output)["source_id"]

    # write initial concept with sid_a
    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "topic", "--title", "Topic", "--body", "initial body",
        "--extractions", json.dumps([{"source_id": sid_a, "quote_span": "alpha"}]),
        "--json",
    ])
    assert res.exit_code == 0, res.output

    # update: 改 body (含 wikilink 表达 link) + 加 sid_b
    res = _runner().invoke(cli, [
        "concepts", "update", str(vault), "topic",
        "--body", "new body after update with [[related-topic]]",
        "--add-extractions", json.dumps([{"source_id": sid_b, "quote_span": "beta"}]),
        "--json",
    ])
    assert res.exit_code == 0, res.output
    parsed = json.loads(res.output)
    assert sid_b in parsed["added_source_ids"]
    assert sid_a in parsed["source_ids"] and sid_b in parsed["source_ids"]

    # wiki 文件反映新 body
    wiki = (vault / "wiki" / "concept" / "topic.md").read_text(encoding="utf-8")
    assert "new body after update" in wiki
    assert "initial body" not in wiki
    # body 里的 [[related-topic]] 自动 derive 出 outgoing links (DB 侧)
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "topic")
    assert "related-topic" in info["links"]

    # DB body 也更新 (含 [[related-topic]] wikilink 用于派生 outgoing links)
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "topic")
    assert info["body"] == "new body after update with [[related-topic]]"


def test_concepts_update_rejects_self_link(vault: Path, external: Path):
    """update 加 link 含自引用应拒绝."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "self", "--title", "S",
        "--body", "self-ref body without wikilinks",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])
    # --add-links 已 drop. outgoing links 现在从 body wikilink 派生.
    # 写 body 含 self-ref [[self]] 应该被存储层自动过滤 (exclude_slug=slug).
    res = _runner().invoke(cli, [
        "concepts", "update", str(vault), "self",
        "--body", "this body has [[self]] self-ref should be filtered", "--json",
    ])
    assert res.exit_code == 0
    # 概念读出来 outgoing links 应为空
    from corpus.storage import read_concept
    info = read_concept(Path(vault) / ".wiki-meta" / "corpus.db", "self")
    assert info["links"] == [], f"self-ref wikilink 应该被过滤, 实际: {info['links']}"


# ---------- concepts delete CLI ----------

def test_concepts_delete_dry_run_then_real(vault: Path, external: Path):
    """默认 dry-run 显示信息, --no-dry-run 真删."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "kill", "--title", "K", "--body", "b",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])

    # dry-run: 仍存在
    res = _runner().invoke(cli, ["concepts", "delete", str(vault), "kill", "--json"])
    assert res.exit_code == 0
    assert read_concept(vault / ".wiki-meta" / "corpus.db", "kill") is not None
    # 真删
    res = _runner().invoke(cli, ["concepts", "delete", str(vault), "kill", "--no-dry-run", "--json"])
    assert res.exit_code == 0
    assert read_concept(vault / ".wiki-meta" / "corpus.db", "kill") is None
    # wiki 文件也被删
    assert not (vault / "wiki" / "concept" / "kill.md").exists()


def test_concepts_delete_404_on_unknown(vault: Path):
    res = _runner().invoke(cli, ["concepts", "delete", str(vault), "nope", "--no-dry-run"])
    assert res.exit_code == 1
    assert "concept not found" in (res.stderr or "")


# ---------- concepts write 物理写失败回滚 ----------

def test_concepts_write_rolls_back_db_on_wiki_write_failure(vault: Path, external: Path, monkeypatch):
    """wiki 文件写失败时, DB 概念行应被 delete_concept 回滚."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    # 让 wiki_path.write_text 抛 OSError
    from corpus import cli as cli_mod
    real_write_text = type(cli_mod.Path("x")).write_text if False else None
    # 用 monkeypatch patch write_concept_file 模拟磁盘满 (frontmatter 写失败 -> DB 回滚)
    from corpus import storage as _storage
    real = _storage.write_concept_file
    def boom(vault_root, **kwargs):
        raise OSError("simulated disk full")
    monkeypatch.setattr(cli_mod, "write_concept_file", boom)

    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "rollback-me", "--title", "R", "--body", "b",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])
    assert res.exit_code == 1
    assert "rolled back" in (res.stderr or "").lower() or "simulated disk full" in (res.stderr or "").lower()

    # DB 行已回滚 (concept 应不存在)
    assert read_concept(vault / ".wiki-meta" / "corpus.db", "rollback-me") is None


# ---------- concepts remove-extraction CLI (P2) ----------

def test_concepts_remove_extraction_drops_extraction_and_may_orphan(vault: Path, external: Path):
    """remove-extraction: 撤唯一抽取 → concept is_orphan=1."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "rem", "--title", "R", "--body", "b",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])
    # 直接查 DB 拿 extraction_id
    import sqlite3
    with sqlite3.connect(str(vault / ".wiki-meta" / "corpus.db")) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT extraction_id FROM extractions WHERE concept_slug=?", ("rem",)).fetchone()
        ext_id = row["extraction_id"]

    res = _runner().invoke(cli, [
        "concepts", "unlink", str(vault), "rem",
        "--extraction-id", ext_id, "--json",
    ])
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.output)
    assert parsed["deleted"] is True
    assert parsed["concept_is_orphan_after"] is True

    info = read_concept(vault / ".wiki-meta" / "corpus.db", "rem")
    assert info["is_orphan"] is True


def test_concepts_remove_extraction_404(vault: Path):
    res = _runner().invoke(cli, [
        "concepts", "unlink", str(vault), "any-slug",
        "--extraction-id", "e_does_not_exist",
    ])
    assert res.exit_code == 1
    assert "extraction not found" in (res.stderr or "")


# ---------- concepts certify partial (P2) ----------

def test_concepts_certify_partial_keeps_old_score(vault: Path, external: Path):
    """certify --issues 只改 issues, score 保留."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "c", "--title", "C", "--body", "b",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])
    _runner().invoke(cli, ["concepts", "certify", str(vault), "c",
        "--score", "0.7", "--issues", "a", "--suggestions", "b", "--json"])

    res = _runner().invoke(cli, ["concepts", "certify", str(vault), "c",
        "--issues", "new-issue", "--json"])
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.output)
    assert parsed["score"] == 0.7
    assert parsed["issues"] == ["new-issue"]
    assert parsed["suggestions"] == ["b"]
    assert parsed["partial_update"] is True

    info = read_concept(vault / ".wiki-meta" / "corpus.db", "c")
    assert info["certified_score"] == 0.7
    assert info["certified_issues"] == ["new-issue"]


def test_concepts_certify_no_fields_errors(vault: Path, external: Path):
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "c2", "--title", "C2", "--body", "b",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])
    _runner().invoke(cli, ["concepts", "certify", str(vault), "c2",
        "--score", "0.5", "--issues", "x", "--json"])
    res = _runner().invoke(cli, ["concepts", "certify", str(vault), "c2", "--json"])
    assert res.exit_code == 1
    assert "no fields" in (res.stderr or "").lower()


def test_concepts_certify_first_time_requires_score(vault: Path, external: Path):
    """首次认证必须 --score."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "c3", "--title", "C3", "--body", "b",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])
    res = _runner().invoke(cli, ["concepts", "certify", str(vault), "c3",
        "--issues", "only-issues", "--json"])
    assert res.exit_code == 1
    assert "first-time" in (res.stderr or "").lower() or "score is required" in (res.stderr or "").lower()


# ---------- find_concept_by_link scoring (P2) ----------

def test_concepts_find_returns_match_score_sorted(vault: Path, external: Path):
    """concepts find (rename 自 find-by-link) 按 match_score DESC 排序, 含 score 字段."""
    from corpus.storage import init_db, write_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    db = vault / ".wiki-meta" / "corpus.db"
    write_concept(db, slug="postgres", title="Postgres Overview", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "x"}])
    write_concept(db, slug="postgres-mvcc", title="MVCC", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "x"}])
    write_concept(db, slug="wal-deep", title="Deep dive into PostgreSQL WAL", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "x"}])

    # 搜 "postgres" → 第一个是 exact, 然后 startswith, contains, title
    res = _runner().invoke(cli, [
        "concepts", "find", str(vault), "--by-link", "postgres", "--json",
    ])
    items = json.loads(res.output)
    slugs = [c["slug"] for c in items]
    assert "postgres" in slugs
    assert "postgres-mvcc" in slugs
    # score 都在
    for it in items:
        assert "match_score" in it
    # 第一个 score 最高
    scores = [c["match_score"] for c in items]
    assert scores == sorted(scores, reverse=True)


def test_concepts_find_with_limit(vault: Path, external: Path):
    """concepts find --limit N 限制返回条数."""
    from corpus.storage import init_db, write_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    db = vault / ".wiki-meta" / "corpus.db"
    for slug in ["pg-a", "pg-b", "pg-c", "pg-d"]:
        write_concept(db, slug=slug, title=slug.upper(), body="b",
            extractions_data=[{"source_id": sid, "quote_span": "x"}])

    res = _runner().invoke(cli, [
        "concepts", "find", str(vault), "--by-link", "pg", "--limit", "2", "--json",
    ])
    assert res.exit_code == 0, res.output
    items = json.loads(res.output)
    assert len(items) == 2

    # 无 --limit 返全部 match_score > 0
    res = _runner().invoke(cli, [
        "concepts", "find", str(vault), "--by-link", "pg", "--json",
    ])
    items = json.loads(res.output)
    assert len(items) == 4


# ---------- vault init 修复 (vault root 不存在应自动 mkdir) ----------

def test_vault_init_creates_root_if_missing(tmp_path: Path):
    """`vault init <newpath>` 应等价 mkdir -p + 建子目录 + init db."""
    new_vault = tmp_path / "deeply" / "nested" / "myvault"
    assert not new_vault.exists()
    res = _runner().invoke(cli, ["vault", "init", str(new_vault), "--json"])
    assert res.exit_code == 0, res.stderr
    assert new_vault.exists()
    assert (new_vault / "raw").is_dir()
    assert (new_vault / "wiki" / "concept").is_dir()
    assert (new_vault / ".wiki-meta" / "corpus.db").is_file()


def test_vault_init_idempotent_on_existing(vault: Path):
    """已初始化的 vault 再 init 不应出错."""
    res = _runner().invoke(cli, ["vault", "init", str(vault), "--json"])
    assert res.exit_code == 0, res.stderr
    # raw/ 还在
    assert (vault / "raw").is_dir()


# ---------- sources ingest in-vault 检查 ----------

def test_ingest_rejects_path_inside_raw(vault: Path, external: Path):
    """ingest vault/raw/ 内文件 → 报 'already in vault raw/'."""
    # 先正常 ingest 一个文件到 raw/
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    assert res.exit_code == 0
    # raw/ 里有 x-ingest-<ts>.md, 拿真实路径再 ingest
    raw_files = [p for p in (vault / "raw").iterdir() if p.name != ".tmp"]
    assert len(raw_files) == 1
    target = raw_files[0]
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(target), "--json"])
    assert res.exit_code == 1
    assert "path is inside vault raw/" in (res.stderr or "")
    assert "不重复 ingest" in (res.stderr or "")


def test_ingest_rejects_path_inside_vault_other_dir(vault: Path, external: Path):
    """ingest vault 内部目录 (wiki/) → 报 'forbidden internal directory'."""
    # 手工在 wiki/ 里放文件
    (vault / "wiki" / "concept").mkdir(parents=True, exist_ok=True)
    wiki_file = vault / "wiki" / "concept" / "leak.md"
    wiki_file.write_text("leaked", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(wiki_file), "--json"])
    assert res.exit_code == 1
    assert "path is inside vault" in (res.stderr or "")
    assert "forbidden internal" in (res.stderr or "").lower() or "禁止" in (res.stderr or "")


def test_ingest_accepts_external_absolute_path(vault: Path, tmp_path: Path):
    """ingest 接受 vault 外的绝对路径 (不同 cwd 也行)."""
    src = tmp_path / "outside.md"
    src.write_text("absolute outside content", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(src), "--json"])
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.output)
    assert parsed["action"] == "staged"
    # raw_path 应在 vault/raw/
    assert str(vault / "raw") in parsed["raw_path"]
    assert parsed["raw_path"].endswith(".md")


# ---------- Bug 1: sources_batch 写 frontmatter + wiki source page ----------

def test_sources_batch_writes_frontmatter_and_wiki_page(vault: Path, external: Path, tmp_path: Path):
    """Bug 1 修复: sources_batch 现在跟 sources_ingest 一样:
      1. 写 raw/<file> frontmatter (含 source_id / content_hash / slug 等)
      2. 写 wiki/source/<slug>.md (obsidian 兼容)

    之前 batch 用 plain target.write_text, 没 frontmatter, 也没 wiki/source page.
    """
    # 准备 batch 目录 (vault 外)
    src_dir = external / "batch_articles"
    src_dir.mkdir()
    (src_dir / "postgresql-mvcc.md").write_text("# MVCC content", encoding="utf-8")
    (src_dir / "wal.md").write_text("# WAL content", encoding="utf-8")

    # batch ingest
    res = _runner().invoke(cli, [
        "sources", "add", str(vault), str(src_dir), "--glob", "*.md", "--json",
    ])
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.output)
    assert parsed["staged"] == 2

    # 1. raw/<file> 应该有 frontmatter (有 --- 开头)
    raw_files = [p for p in (vault / "raw").iterdir() if p.name not in (".tmp", ".gitkeep")]
    assert len(raw_files) == 2
    for f in raw_files:
        content = f.read_text(encoding="utf-8")
        assert content.startswith("---"), f"{f.name} 应该有 frontmatter"
        assert "source_id:" in content
        assert "content_hash:" in content


    # 2. wiki/source/<slug>.md 存在 (obsidian 兼容)
    src_pages = [p for p in (vault / "wiki" / "source").iterdir() if p.name != ".tmp"]
    assert len(src_pages) == 2
    for p in src_pages:
        assert p.suffix == ".md"
        # slug 文件名, 不是 source_id 16 hex
        assert not p.stem[0].isdigit() or p.stem.isdigit() and len(p.stem) <= 8
        content = p.read_text(encoding="utf-8")
        assert content.startswith("---")
        # frontmatter 含 slug
        assert "slug:" in content
        assert "source_id:" in content
        # wiki/source 是 extraction manifest: 只有 ## Concepts section, 不复制原文
        assert "## Concepts extracted from this source" in content
        # 原文 sole 在 raw/<file>.md, wiki/source 不复制
        assert "# rollback content" not in content


# ---------- Bug 2: write_source 失败回滚 DB ----------

def test_sources_ingest_rolls_back_db_on_write_failure(
    vault: Path, external: Path, monkeypatch
):
    """Bug 2 修复: write_source_file 失败时 delete_source 回滚 DB (DB 不留孤立 source_id).

    之前 sources_ingest 只 try/except _err, 不回滚 DB, 留下 stage_source 写但 raw 没写.
    """
    # 准备 external source file
    src = external / "rollback_test.md"
    src.write_text("# rollback content", encoding="utf-8")

    import sqlite3
    # monkeypatch write_source_file 让它 raise OSError
    from corpus import cli as _cli
    from corpus import storage as _storage
    real = _cli.write_source_file
    def boom(vault_root, raw_path, **kwargs):
        raise OSError("simulated disk full on raw file write")
    monkeypatch.setattr(_cli, "write_source_file", boom)

    res = _runner().invoke(cli, [
        "sources", "add", str(vault), str(src), "--json",
    ])
    # 应该 exit 1 (OSError 报给 user)
    assert res.exit_code == 1
    assert "rolled back" in (res.stderr or "").lower()

    # DB 不应该留 source_id (回滚 delete_source)
    with sqlite3.connect(str(vault / ".wiki-meta" / "corpus.db")) as c:
        n = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        assert n == 0, f"DB 应该被回滚, 但还有 {n} 个 source"


# ---------- --body-file: 从文件读 body (省 LLM shell 转义) ----------
def test_concepts_write_body_file_basic(vault: Path, external: Path):
    """--body-file 从文件读 markdown 内容, 含 shell-special chars 也能 round-trip."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x content", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    # 写含特殊字符的 body 到 temp 文件
    body_path = vault / "body.md"
    body_path.write_text(
        "Multi-line body with `$VAR`, `&&`, `'spaces'`, `|pipe|`, [[proc-cpuinfo]] wikilink.\n"
        "```bash\necho 'do not expand $1'\n```\n",
        encoding="utf-8",
    )

    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "bf", "--title", "BodyFile",
        "--body-file", str(body_path),
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "q"}]),
        "--json",
    ])
    assert res.exit_code == 0, res.output
    # DB body 与文件一致
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "bf")
    assert info["body"].startswith("Multi-line body with")
    # shell-special chars round-trip intact (无 markdown 转义 / shell 展开)
    assert "$1" in info["body"]
    assert "&" in info["body"]
    assert "|" in info["body"]
    assert "`$VAR`" in info["body"]
    assert "&&" in info["body"]
    assert "'" in info["body"]
    assert "[[proc-cpuinfo]]" in info["body"]
    # body 派生 links 工作
    assert "proc-cpuinfo" in info["links"]


def test_concepts_write_body_and_body_file_mutex(vault: Path, external: Path):
    """--body 与 --body-file 不能同时传 (两者都给时, 走我们的 exit 1 互斥检查)."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    body_path = vault / "body.md"
    body_path.write_text("from file", encoding="utf-8")  # 文件存在让 click.Path 通过
    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "x", "--title", "X",
        "--body", "inline",
        "--body-file", str(body_path),
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "q"}]),
        "--json",
    ])
    assert res.exit_code == 1
    assert "互斥" in (res.stderr or "")


def test_concepts_write_neither_body_nor_file(vault: Path, external: Path):
    """--body / --body-file 必须传一个."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "x", "--title", "X",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "q"}]),
        "--json",
    ])
    assert res.exit_code == 1
    assert "必须传" in (res.stderr or "")


def test_concepts_write_body_file_not_found(vault: Path, external: Path):
    """--body-file 不存在的文件应该报错."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "x", "--title", "X",
        "--body-file", "/nonexistent/path/body.md",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "q"}]),
        "--json",
    ])
    # click.Path(exists=True) 会拒绝不存在的路径 (exit 2, usage 错)
    assert res.exit_code != 0


def test_concepts_update_body_file(vault: Path, external: Path):
    """concepts update 也支持 --body-file."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    # 先写一个
    _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "u", "--title", "U",
        "--body", "initial body",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "q"}]),
        "--json",
    ])
    # 通过 --body-file 替换
    body_path = vault / "new_body.md"
    body_path.write_text(
        "updated body with [[lscpu]] wikilink and shell chars: $1 && | &",
        encoding="utf-8",
    )
    res = _runner().invoke(cli, [
        "concepts", "update", str(vault), "u",
        "--body-file", str(body_path),
        "--json",
    ])
    assert res.exit_code == 0, res.output
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "u")
    assert info["body"].startswith("updated body with")
    assert "lscpu" in info["links"]


# ---------- --extractions-file: 从文件读 JSON array ----------
def test_concepts_write_extractions_file_basic(vault: Path, external: Path):
    """--extractions-file 从文件读 JSON array, 含 shell-special chars 也能 round-trip."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x content", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    # 写 JSON array 到 temp 文件 (含特殊字符: 双引号 / 反斜杠 / unicode)
    extractions_path = vault / "extr.json"
    extractions_data = [
        {"source_id": sid, "quote_span": 'quote with "double" + \\backslash + 中文'},
        {"source_id": sid, "quote_span": "another line"},
    ]
    extractions_path.write_text(
        json.dumps(extractions_data, ensure_ascii=False),
        encoding="utf-8",
    )

    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "ef", "--title", "EF",
        "--body", "body see [[lscpu]]",
        "--extractions-file", str(extractions_path),
        "--json",
    ])
    assert res.exit_code == 0, res.output
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "ef")
    # 两个 extractions 都进 DB
    with sqlite3.connect(str(vault / ".wiki-meta" / "corpus.db")) as c:
        c.row_factory = sqlite3.Row
        ext_n = c.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE concept_slug='ef'"
        ).fetchone()["n"]
    assert ext_n == 2, f"expected 2 extractions, got {ext_n}"
    # 引号 / 反斜杠 / unicode 都保留
    qs_conn = sqlite3.connect(str(vault / ".wiki-meta" / "corpus.db"))
    qs_conn.row_factory = sqlite3.Row
    qs_rows = [
        r["quote_span"]
        for r in qs_conn.execute(
            "SELECT quote_span FROM extractions WHERE concept_slug='ef' ORDER BY extraction_id"
        ).fetchall()
    ]
    assert any('"double"' in q and "\\backslash" in q and "中文" in q for q in qs_rows), qs_rows


def test_concepts_write_extractions_and_extractions_file_mutex(vault: Path, external: Path):
    """--extractions 与 --extractions-file 不能同时传."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    extr_path = vault / "extr.json"
    extr_path.write_text(json.dumps([{"source_id": sid, "quote_span": "q"}]), encoding="utf-8")
    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "x", "--title", "X",
        "--body", "body",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "q"}]),
        "--extractions-file", str(extr_path),
        "--json",
    ])
    assert res.exit_code == 1
    assert "互斥" in (res.stderr or "")


def test_concepts_write_neither_extractions_nor_file(vault: Path, external: Path):
    """--extractions / --extractions-file 必须传一个."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "x", "--title", "X",
        "--body", "body",
        "--json",
    ])
    assert res.exit_code == 1
    assert "extractions" in (res.stderr or "").lower()


def test_concepts_write_extractions_file_invalid_json(vault: Path, external: Path):
    """--extractions-file 里的 JSON 不是合法 → exit 1."""
    from corpus.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    extr_path = vault / "extr.json"
    extr_path.write_text("not valid json {{{", encoding="utf-8")
    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "x", "--title", "X",
        "--body", "body",
        "--extractions-file", str(extr_path),
        "--json",
    ])
    assert res.exit_code == 1
    assert "JSON" in (res.stderr or "") or "json" in (res.stderr or "")


def test_concepts_update_add_extractions_file(vault: Path, external: Path):
    """concepts update --add-extractions-file 也走文件读."""
    from corpus.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "a.md").write_text("alpha", encoding="utf-8")
    (external / "b.md").write_text("beta", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "a.md"), "--json"])
    sid_a = json.loads(res.output)["source_id"]
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "b.md"), "--json"])
    sid_b = json.loads(res.output)["source_id"]

    # 初始 concept (用 sid_a)
    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "u", "--title", "U", "--body", "initial",
        "--extractions", json.dumps([{"source_id": sid_a, "quote_span": "alpha"}]),
        "--json",
    ])
    assert res.exit_code == 0, res.output

    # 加 sid_b: JSON 文件读入
    add_path = vault / "add.json"
    add_path.write_text(
        json.dumps([{"source_id": sid_b, "quote_span": "beta with 'quotes' & symbols"}]),
        encoding="utf-8",
    )
    res = _runner().invoke(cli, [
        "concepts", "update", str(vault), "u",
        "--add-extractions-file", str(add_path),
        "--json",
    ])
    assert res.exit_code == 0, res.output
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "u")
    assert sid_a in info["source_ids"] and sid_b in info["source_ids"]
    # 多了 1 条 extraction
    with sqlite3.connect(str(vault / ".wiki-meta" / "corpus.db")) as c:
        ext_n = c.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE concept_slug='u'"
        ).fetchone()[0]
    assert ext_n == 2, f"expected 2 extractions, got {ext_n}"


# ========== Stage 2 ingest 简化: 新命令 + aliases 测试 ==========

def test_sources_add_file_mode_basic(vault: Path, external: Path):
    """sources add -- 单文件 (path 是文件) 走单文件模式."""
    (external / "n.md").write_text("hello", encoding="utf-8")
    res = _runner().invoke(cli, [
        "sources", "add", str(vault), str(external / "n.md"), "--json",
    ])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["action"] == "staged"
    assert out["source_id"]


def test_sources_add_dir_mode_basic(vault: Path, external: Path):
    """sources add -- path 是目录走 batch."""
    (external / "a.md").write_text("aaa", encoding="utf-8")
    (external / "b.md").write_text("bbb", encoding="utf-8")
    res = _runner().invoke(cli, [
        "sources", "add", str(vault), str(external), "--glob", "*.md", "--json",
    ])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["total"] == 2
    assert out["staged"] == 2


def test_sources_mark_state_staged_to_committed(vault: Path, external: Path):
    """通用化: staged → committed (替代老的 sources commit)."""
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    res = _runner().invoke(cli, [
        "sources", "mark-state", str(vault), sid, "--status", "committed", "--json",
    ])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["status"] == "committed"
    assert out["old_status"] == "staged"


def test_vault_inspect_basic(vault: Path, external: Path):
    """vault inspect 替代 vault info + vault stats."""
    res = _runner().invoke(cli, ["vault", "inspect", str(vault), "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["db_initialized"] is True
    assert out["concepts"]["total"] == 0
    assert out["sources"]["total"] == 0
    assert out["schema_version"] >= 6


def test_corpus_history_basic(vault: Path, external: Path):
    """corpus history 顶层命令 — 列出 ingest_log, 至少一条 stage 操作."""
    from corpus.storage import list_ingest_log
    (external / "x.md").write_text("x", encoding="utf-8")
    _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md")])
    r = _runner().invoke(cli, ["history", str(vault), "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    assert isinstance(rows, list)
    assert len(rows) >= 1
    assert any(row["op"] == "stage" for row in rows)


def test_corpus_history_filter_by_op(vault: Path, external: Path):
    """corpus history --op stage 只返 stage 类型 rows."""
    (external / "x.md").write_text("x", encoding="utf-8")
    _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md")])
    r = _runner().invoke(cli, ["history", str(vault), "--op", "stage", "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    assert all(row["op"] == "stage" for row in rows)


def test_corpus_rebuild_dry_run(vault: Path):
    """corpus rebuild --dry-run 不写 DB, 只统计 scan 出来的 concept/source 数."""
    r = _runner().invoke(cli, ["rebuild", str(vault), "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    # 期望 keys: sources / concepts / links / extractions (counts, 全 0 因 vault 空)
    for k in ["sources", "concepts", "links", "extractions"]:
        assert k in out
        assert isinstance(out[k], int)
        assert out[k] == 0


def test_concepts_link_basic(vault: Path, external: Path):
    """concepts link 替代老的 add-source."""
    from corpus.storage import init_db, write_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    write_concept(
        vault / ".wiki-meta" / "corpus.db",
        slug="c", title="C", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "x1"}],
    )
    res = _runner().invoke(cli, [
        "concepts", "link", str(vault), "c",
        "--source", sid, "--quote-span", "additional evidence", "--json",
    ])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["action"] == "inserted"


def test_concepts_unlink_by_source(vault: Path, external: Path):
    """concepts unlink --source SID 粗粒度."""
    from corpus.storage import init_db, write_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    write_concept(
        vault / ".wiki-meta" / "corpus.db",
        slug="c", title="C", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "x"}],
    )
    res = _runner().invoke(cli, [
        "concepts", "unlink", str(vault), "c", "--source", sid, "--json",
    ])
    assert res.exit_code == 0, res.output


def test_concepts_unlink_requires_source_or_extraction_id(vault: Path, external: Path):
    """concepts unlink --source / --extraction-id 都不传 → 错."""
    from corpus.storage import init_db, write_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    write_concept(
        vault / ".wiki-meta" / "corpus.db",
        slug="c", title="C", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "x"}],
    )
    res = _runner().invoke(cli, ["concepts", "unlink", str(vault), "c", "--json"])
    assert res.exit_code == 1


def test_concepts_show_with_source_filter(vault: Path, external: Path):
    """concepts show --source SID 退化成 evidence 视图 (替代老的 evidence 命令)."""
    from corpus.storage import init_db, write_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    write_concept(
        vault / ".wiki-meta" / "corpus.db",
        slug="c", title="C", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "quote 1"},
                          {"source_id": sid, "quote_span": "quote 2"}],
    )
    res = _runner().invoke(cli, [
        "concepts", "show", str(vault), "c", "--source", sid, "--json",
    ])
    assert res.exit_code == 0, res.output
    # evidence-only 模式返 list (不是 concept dict)
    out = res.output
    assert "quote 1" in out
    assert "quote 2" in out


def test_concepts_list_with_multiple_tags(vault: Path, external: Path):
    """concepts list --tag X [--tag Y]: 多 tag 与 (AND) 取交集."""
    from corpus.storage import init_db, write_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (external / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "add", str(vault), str(external / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    # 两个 concept, tag 不同
    write_concept(
        vault / ".wiki-meta" / "corpus.db",
        slug="linux", title="L", body="l",
        extractions_data=[{"source_id": sid, "quote_span": "x"}],
        tags=["os", "kernel"],
    )
    write_concept(
        vault / ".wiki-meta" / "corpus.db",
        slug="postgres", title="P", body="p",
        extractions_data=[{"source_id": sid, "quote_span": "x"}],
        tags=["db", "kernel"],
    )
    # --tag kernel 返 2 个
    res = _runner().invoke(cli, [
        "concepts", "list", str(vault), "--tag", "kernel", "--json",
    ])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert len(out) == 2
    # --tag os --tag kernel 取交集 (linux 命中, postgres 没 os)
    res = _runner().invoke(cli, [
        "concepts", "list", str(vault), "--tag", "os", "--tag", "kernel", "--json",
    ])
    out = json.loads(res.output)
    assert len(out) == 1
    assert out[0]["slug"] == "linux"


def test_corpus_stats_alias_removed():
    """user 选 drop, 不应再存在 corpus stats 命令."""
    runner = _runner()
    # 没有 subcommand 'corpus stats', 应该报 unknown command (exit 2)
    res = runner.invoke(cli, ["stats", "/tmp/nonexistent"])
    assert res.exit_code == 2
    assert "No such command" in res.output or "Usage" in res.output
