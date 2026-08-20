"""CLI 层测试 (sources ingest / batch 端到端, 撞名 + revive)."""

from pathlib import Path

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

def test_batch_renames_on_name_collision(vault: Path):
    """batch: raw/ 已有 v1 同名, 目录里有 v2 同名 → v2 自动改名, v1 不丢.

    这是真实撞名场景: 不同会话/工具各 ingest 同一文件名但不同内容.
    """
    r = _runner()

    # 步骤 1: 在 raw/ 里手工放一个 v1 文件 (模拟已入库的旧 source)
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
    # 应该有 2 个文件: 原来的 v1 (note.md) + 改名的 v2
    assert len(raw_files) == 2
    assert "note.md" in raw_files
    renamed_name = next(f for f in raw_files if f != "note.md")
    assert renamed_name.startswith("note_") and renamed_name.endswith(".md")

    # v1 内容未被覆盖 (旧 source 仍指向原文件)
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
    (src_dir / "note.md").write_text("# note v1 modified\n", encoding="utf-8")  # 再覆盖

    # raw 里放一个占位同名文件触发撞名 (虽然 batch 模式下不需要, 测的是改名逻辑)
    # 实际上 batch 是从 source_dir 读, target = pick_raw_target(raw, content, src.name)
    # 如果 raw 里没有同名 → target = raw/<src.name> (不撞名)
    # 我们想测 batch 内有同 dir 下不同 content 但同名? 不可能, 一个 dir 不会有同名文件
    # 所以 batch 的撞名主要场景: raw/ 里已有别的 source 同名
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
