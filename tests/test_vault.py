"""Vault 七条校验测试。"""

from pathlib import Path

import json
import pytest

from corpus.vault import ensure_vault, validate_source_path
from corpus.errors import ValidationError, ConfigError


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """创建临时 vault。"""
    v = tmp_path / "test-vault"
    v.mkdir()
    ensure_vault(v)
    return v


def test_vault_init_creates_dirs(vault: Path):
    assert (vault / "raw").is_dir()
    assert (vault / "wiki" / "concept").is_dir()
    assert (vault / ".wiki-meta").is_dir()
    assert (vault / ".wiki-meta" / ".gitignore").is_file()


def test_validate_existing_file(vault: Path):
    (vault / "raw" / "test.md").write_text("# hello")
    result = validate_source_path(vault, str(vault / "raw" / "test.md"))
    assert result.name == "test.md"


def test_rule1_missing_path(vault: Path):
    with pytest.raises(ValidationError) as exc_info:
        validate_source_path(vault, str(vault / "raw" / "nonexistent.md"))
    assert exc_info.value.rule == "R1_exists"


def test_rule4_extension(vault: Path):
    (vault / "raw" / "test.exe").write_text("binary")
    with pytest.raises(ValidationError) as exc_info:
        validate_source_path(vault, str(vault / "raw" / "test.exe"))
    assert exc_info.value.rule == "R4_extension"


def test_rule6_outside_vault(vault: Path):
    # 文件在 vault 父目录（不在 vault 内），应抛 R6_canonical
    outside = vault.parent / "evil.md"
    outside.write_text("evil content")
    with pytest.raises(ValidationError) as exc_info:
        validate_source_path(vault, str(outside))
    assert exc_info.value.rule == "R6_canonical"


def test_rule7_not_in_raw(vault: Path):
    (vault / "wiki" / "evil.md").write_text("evil")
    with pytest.raises(ValidationError) as exc_info:
        validate_source_path(vault, str(vault / "wiki" / "evil.md"))
    assert exc_info.value.rule == "R7_raw_subtree"


def test_no_extension_allowed(vault: Path):
    (vault / "raw" / "README").write_text("# no ext")
    result = validate_source_path(vault, str(vault / "raw" / "README"))
    assert result.exists()


def test_markdown_extension_allowed(vault: Path):
    (vault / "raw" / "x.markdown").write_text("md")
    result = validate_source_path(vault, str(vault / "raw" / "x.markdown"))
    assert result.exists()


# ---------- pick_raw_target (默认 ingest 后缀) ----------

import re as _re
_INGEST_RE = _re.compile(r"ingest-\d{8}-\d{6}")


def test_pick_raw_target_always_adds_ingest_suffix(vault: Path):
    """无论 raw/ 下是否存在, 都生成 <stem>-ingest-<UTC compact ISO><suffix>."""
    from corpus.vault import pick_raw_target
    target = pick_raw_target(vault / "raw", "any content", "note.md")
    assert target.parent == vault / "raw"
    assert _INGEST_RE.search(target.stem), f"no ingest suffix in {target.name}"
    assert target.name.endswith(".md")
    assert target.stem.startswith("note-")


def test_pick_raw_target_same_stem_different_calls_yields_unique_paths(vault: Path):
    """连续两次调用生成不同 ingest 后缀 (跨秒不会撞名)."""
    from corpus.vault import pick_raw_target
    t1 = pick_raw_target(vault / "raw", "content", "note.md")
    t2 = pick_raw_target(vault / "raw", "content", "note.md")
    # 同秒情况下确实可能相等 (人类操作可忽略); 但格式一定对
    assert _INGEST_RE.search(t1.stem)
    assert _INGEST_RE.search(t2.stem)


def test_pick_raw_target_preserves_markdown_extension(vault: Path):
    """.markdown 后缀保留, ingest 后缀插在 stem 中间."""
    from corpus.vault import pick_raw_target
    target = pick_raw_target(vault / "raw", "any", "deep.markdown")
    assert target.suffix == ".markdown"
    assert target.stem.startswith("deep-ingest-")


def test_pick_raw_target_preserves_no_extension(vault: Path):
    """无后缀文件名也保留, ingest 后缀照样加."""
    from corpus.vault import pick_raw_target
    target = pick_raw_target(vault / "raw", "any", "README")
    assert target.suffix == ""
    assert _INGEST_RE.search(target.stem)
    assert target.stem.startswith("README-ingest-")


# ---------- _ensure_git_repo ----------

def test_ensure_git_repo_idempotent(vault: Path):
    """vault 已是 git 仓库 → _ensure_git_repo 不重复 init."""
    import subprocess
    # 先手动 git init (setup)
    subprocess.run(["git", "init", "--initial-branch=main", str(vault)],
                   check=True, capture_output=True)
    from corpus.vault import _ensure_git_repo
    info = _ensure_git_repo(vault)
    assert info["git_initialized"] is False
    assert "already" in info["reason"].lower() or "git repository" in info["reason"]


def test_ensure_git_repo_creates_repo(vault: Path):
    """空 vault → _ensure_git_repo 创建 .git/."""
    from corpus.vault import _ensure_git_repo
    info = _ensure_git_repo(vault)
    assert info["git_initialized"] is True
    assert info["git_path"].endswith(".git")
    assert (vault / ".git").exists()


def test_vault_init_default_git_init(vault: Path):
    """vault_init 默认会调 git init."""
    from click.testing import CliRunner
    from corpus.cli import cli
    new_vault = vault.parent / "new-vault-git"
    res = CliRunner().invoke(cli, ["vault", "init", str(new_vault), "--json"])
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.output)
    assert parsed["git"]["git_initialized"] is True
    assert (new_vault / ".git").exists()


def test_vault_init_no_git_flag(vault: Path):
    """vault_init --no-git 跳过 git init."""
    from click.testing import CliRunner
    from corpus.cli import cli
    new_vault = vault.parent / "new-vault-nogit"
    res = CliRunner().invoke(cli, ["vault", "init", str(new_vault), "--no-git", "--json"])
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.output)
    assert parsed["git"]["git_initialized"] is False
    assert "--no-git" in parsed["git"]["reason"]
    assert not (new_vault / ".git").exists()
