"""CLI 层测试 (sources ingest / batch 端到端, 撞名 + revive)."""

from pathlib import Path

import json

import pytest
from click.testing import CliRunner

from corpus_bot.cli import cli
from corpus_bot.errors import ConflictError, ValidationError


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """新建一个空 vault, raw/ 已建好."""
    v = tmp_path / "vault"
    (v / "raw").mkdir(parents=True)
    # vault init 会建 wiki/ .wiki-meta/
    from corpus_bot.vault import ensure_vault, vault_paths
    from corpus_bot.storage import init_db
    ensure_vault(v)
    init_db(vault_paths(v)["corpus_db"])
    return v


def _runner():
    # click 8.3: stderr 不再混入 stdout, 也不再有 mix_stderr 参数
    return CliRunner()


# ---------- sources ingest 撞名修复 ----------

def test_batch_handles_existing_raw_file_without_clobber(vault: Path):
    """batch: raw/ 里手工放一个 v1 文件 (模拟外部已存 source) 不会被 batch 覆盖.

    batch 总是为新文件生成 -ingest-<ts> 后缀, 所以 raw/v1.md 保留,
    新文件变成 raw/note-ingest-<ts>.md.
    """
    import re
    r = _runner()

    # 步骤 1: 在 raw/ 里手工放一个 v1 文件
    (vault / "raw" / "note.md").write_text("# version 1 - already in raw\n", encoding="utf-8")

    # 步骤 2: batch 一个目录, 里有 note.md 但内容完全不同
    src_dir = vault / "incoming"
    src_dir.mkdir()
    (src_dir / "note.md").write_text("# version 2 - completely different\n", encoding="utf-8")

    res = r.invoke(cli, [
        "sources", "batch", str(vault), str(src_dir), "--json",
    ])
    assert res.exit_code == 0, res.stderr

    raw_files = sorted(p.name for p in (vault / "raw").iterdir())
    # 应该有 2 个文件: 原 v1 (note.md) + 改 ingest 后缀的 v2
    assert len(raw_files) == 2
    assert "note.md" in raw_files  # v1 保留
    renamed_name = next(f for f in raw_files if f != "note.md")
    assert renamed_name.startswith("note-ingest-") and renamed_name.endswith(".md"), renamed_name
    assert re.search(r"ingest-\d{8}-\d{6}", renamed_name)

    # v1 内容未被覆盖
    assert (vault / "raw" / "note.md").read_text(encoding="utf-8") == "# version 1 - already in raw\n"
    # v2 内容在改名后的文件里
    assert (vault / "raw" / renamed_name).read_text(encoding="utf-8") == "# version 2 - completely different\n"

    # batch 输出应含 staged=1, 无 failed
    assert '"staged": 1' in res.output
    assert '"failed": 0' in res.output


def test_ingest_collision_same_content_keeps_name(vault: Path):
    """同 hash 撞名 → 不改名 (覆写 idempotent)."""
    r = _runner()
    content = "# same\n"
    (vault / "raw" / "note.md").write_text(content, encoding="utf-8")
    res = r.invoke(cli, [
        "sources", "ingest", str(vault), str(vault / "raw" / "note.md"), "--json",
    ])
    assert res.exit_code == 0
    sid1 = res.output.strip().split('"source_id": "')[1].split('"')[0]

    # 同内容再 ingest (即使是覆写过的同名)
    res = r.invoke(cli, [
        "sources", "ingest", str(vault), str(vault / "raw" / "note.md"), "--json",
    ])
    # 同 hash active → ConflictError → exit 1
    assert res.exit_code == 1
    assert "duplicate" in res.stderr.lower()


# ---------- sources ingest --force-revive ----------

def test_ingest_revive_deleted_source(vault: Path):
    """soft_delete 后再 ingest 同内容 + --force-revive → 复活, 保留 sid."""
    from corpus_bot.storage import soft_delete_source

    r = _runner()
    (vault / "raw" / "x.md").write_text("same content", encoding="utf-8")

    # 第一次入库
    res = r.invoke(cli, [
        "sources", "ingest", str(vault), str(vault / "raw" / "x.md"), "--json",
    ])
    assert res.exit_code == 0
    sid = res.output.strip().split('"source_id": "')[1].split('"')[0]

    # 软删
    soft_delete_source(vault / ".wiki-meta" / "corpus.db", sid, deleted_reason="test")

    # 同内容再 ingest, 不带 flag → exit 1 + hint
    res = r.invoke(cli, [
        "sources", "ingest", str(vault), str(vault / "raw" / "x.md"), "--json",
    ])
    assert res.exit_code == 1
    assert "deleted" in res.stderr.lower()
    assert "--force-revive" in (res.stderr or "")

    # 带 flag → 复活, 保留 sid
    res = r.invoke(cli, [
        "sources", "ingest", str(vault), str(vault / "raw" / "x.md"),
        "--force-revive", "--json",
    ])
    assert res.exit_code == 0, res.stderr
    assert '"action": "revived"' in res.output
    revived_sid = res.output.strip().split('"source_id": "')[1].split('"')[0]
    assert revived_sid == sid


def test_ingest_revive_then_relist_as_staged(vault: Path):
    """复活后 list 能看到 status=staged."""
    from corpus_bot.storage import soft_delete_source, read_source

    r = _runner()
    (vault / "raw" / "y.md").write_text("y content", encoding="utf-8")
    res = r.invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "y.md"), "--json"])
    sid = res.output.strip().split('"source_id": "')[1].split('"')[0]
    soft_delete_source(vault / ".wiki-meta" / "corpus.db", sid)
    r.invoke(cli, [
        "sources", "ingest", str(vault), str(vault / "raw" / "y.md"),
        "--force-revive", "--json",
    ])
    row = read_source(vault / ".wiki-meta" / "corpus.db", sid)
    assert row["status"] == "staged"
    assert row["deleted_at"] is None


# ---------- sources batch --force-revive + 撞名 ----------

def test_batch_renames_and_counts_revived(vault: Path):
    """batch: 撞名改名 + 复活 计数都正确."""
    from corpus_bot.storage import soft_delete_source, read_source, stage_source

    r = _runner()
    db = vault / ".wiki-meta" / "corpus.db"

    # 准备: 直接 stage 一个 source, 然后软删 (供 batch 复活)
    res = stage_source(
        db, raw_path=vault / "raw" / "ghost.md",
        content="ghost content", original_filename="ghost.md",
    )
    ghost_sid = res["source_id"]
    soft_delete_source(db, ghost_sid)

    # batch 目录: 同名碰撞 + ghost 复活
    src_dir = vault / "incoming"
    src_dir.mkdir()
    (src_dir / "note.md").write_text("# note v1\n", encoding="utf-8")
    (src_dir / "ghost.md").write_text("ghost content", encoding="utf-8")
    # 注意: batch 现在每个文件都生成 -ingest-<ts> 后缀, 无需撞名检测
    res = r.invoke(cli, [
        "sources", "batch", str(vault), str(src_dir),
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


def test_batch_without_revive_flags_deleted_as_failed(vault: Path):
    """batch 不带 --force-revive, 同 hash 已 deleted → failed, 给出 hint."""
    from corpus_bot.storage import soft_delete_source, stage_source

    r = _runner()
    db = vault / ".wiki-meta" / "corpus.db"
    res = stage_source(
        db, raw_path=vault / "raw" / "z.md",
        content="z content", original_filename="z.md",
    )
    soft_delete_source(db, res["source_id"])

    src_dir = vault / "incoming"
    src_dir.mkdir()
    (src_dir / "z.md").write_text("z content", encoding="utf-8")

    res = r.invoke(cli, [
        "sources", "batch", str(vault), str(src_dir), "--json",
    ])
    assert res.exit_code == 0  # batch 整体不失败
    assert '"failed": 1' in res.output
    assert '"hint"' in res.output
    assert "--force-revive" in res.output


# ---------- concepts list 过滤 ----------

def test_concepts_list_orphans_filter(vault: Path):
    """--orphans 只返回 source_ids=[] 的 concept."""
    from corpus_bot.storage import write_concept, init_db
    init_db(vault / ".wiki-meta" / "corpus.db")

    # 需要先 ingest 一个 source 给非 orphan concept
    (vault / "raw" / "x.md").write_text("alpha", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="with-src", title="With", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "alpha"}], links=[])
    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="orphan-1", title="Orphan1", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "alpha"}], links=[])
    # 让 orphan-1 变 orphan: remove_source
    _runner().invoke(cli, ["concepts", "remove-source", str(vault), "orphan-1", "--source-id", sid])

    res = _runner().invoke(cli, ["concepts", "list", str(vault), "--orphans", "--json"])
    items = json.loads(res.output)
    assert {c["slug"] for c in items} == {"orphan-1"}


def test_concepts_list_certified_filters(vault: Path):
    """--certified / --uncertified 互斥, 默认无过滤."""
    from corpus_bot.storage import write_concept, mark_certified, init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (vault / "raw" / "x.md").write_text("a", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]
    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="certed", title="C", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "a"}], links=[])
    write_concept(vault / ".wiki-meta" / "corpus.db",
        slug="raw", title="R", body="b",
        extractions_data=[{"source_id": sid, "quote_span": "a"}], links=[])
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

def test_concepts_update_changes_body_and_adds_source(vault: Path):
    """update: --body 覆盖, --add-extractions 加新 source, --add-links 加新 link."""
    from corpus_bot.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")

    # 准备两个 source
    (vault / "raw" / "a.md").write_text("alpha", encoding="utf-8")
    (vault / "raw" / "b.md").write_text("beta", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "a.md"), "--json"])
    sid_a = json.loads(res.output)["source_id"]
    res = _runner().invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "b.md"), "--json"])
    sid_b = json.loads(res.output)["source_id"]

    # write initial concept with sid_a
    res = _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "topic", "--title", "Topic", "--body", "initial body",
        "--extractions", json.dumps([{"source_id": sid_a, "quote_span": "alpha"}]),
        "--json",
    ])
    assert res.exit_code == 0, res.output

    # update: 改 body + 加 sid_b + 加 link
    res = _runner().invoke(cli, [
        "concepts", "update", str(vault), "topic",
        "--body", "new body after update",
        "--add-extractions", json.dumps([{"source_id": sid_b, "quote_span": "beta"}]),
        "--add-links", "related-topic",
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

    # DB body 也更新
    info = read_concept(vault / ".wiki-meta" / "corpus.db", "topic")
    assert info["body"] == "new body after update"


def test_concepts_update_rejects_self_link(vault: Path):
    """update 加 link 含自引用应拒绝."""
    from corpus_bot.storage import init_db
    init_db(vault / ".wiki-meta" / "corpus.db")
    (vault / "raw" / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    _runner().invoke(cli, [
        "concepts", "write", str(vault),
        "--slug", "self", "--title", "S", "--body", "b",
        "--extractions", json.dumps([{"source_id": sid, "quote_span": "x"}]),
        "--json",
    ])
    res = _runner().invoke(cli, [
        "concepts", "update", str(vault), "self",
        "--add-links", "self", "--json",
    ])
    assert res.exit_code == 1
    assert "self-reference" in (res.stderr or "").lower()


# ---------- concepts delete CLI ----------

def test_concepts_delete_dry_run_then_real(vault: Path):
    """默认 dry-run 显示信息, --no-dry-run 真删."""
    from corpus_bot.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (vault / "raw" / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "x.md"), "--json"])
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

def test_concepts_write_rolls_back_db_on_wiki_write_failure(vault: Path, monkeypatch):
    """wiki 文件写失败时, DB 概念行应被 delete_concept 回滚."""
    from corpus_bot.storage import init_db, read_concept
    init_db(vault / ".wiki-meta" / "corpus.db")
    (vault / "raw" / "x.md").write_text("x", encoding="utf-8")
    res = _runner().invoke(cli, ["sources", "ingest", str(vault), str(vault / "raw" / "x.md"), "--json"])
    sid = json.loads(res.output)["source_id"]

    # 让 wiki_path.write_text 抛 OSError
    from corpus_bot import cli as cli_mod
    real_write_text = type(cli_mod.Path("x")).write_text if False else None
    # 用 monkeypatch patch click.Path 内部 Path.write_text: 替换 Path 的 write_text
    from pathlib import Path as PathCls
    def boom(self, *a, **kw):
        # 仅当写到 wiki/concept/<slug>.md 时失败
        if "wiki" in str(self) and "concept" in str(self) and str(self).endswith(".md"):
            raise OSError("simulated disk full")
        return PathCls.write_text(self, *a, **kw)
    monkeypatch.setattr(PathCls, "write_text", boom)

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
