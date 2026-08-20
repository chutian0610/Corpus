"""Vault 七条校验测试。"""

from pathlib import Path

import pytest

from corpus_bot.vault import ensure_vault, validate_source_path
from corpus_bot.errors import ValidationError, ConfigError


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


# ---------- pick_raw_target (撞名改名) ----------

def test_pick_raw_target_no_collision(vault: Path):
    """raw/ 下无同名 → 直接返回原路径."""
    from corpus_bot.vault import pick_raw_target
    target = pick_raw_target(vault / "raw", "any content", "note.md")
    assert target == vault / "raw" / "note.md"


def test_pick_raw_target_same_content_idempotent(vault: Path):
    """raw/ 下同名但 sid 相同 (同内容) → 返回原路径 (允许覆写)."""
    from corpus_bot.vault import pick_raw_target
    content = "# same content\n"
    (vault / "raw" / "note.md").write_text(content, encoding="utf-8")
    target = pick_raw_target(vault / "raw", content, "note.md")
    assert target == vault / "raw" / "note.md"


def test_pick_raw_target_different_content_renames(vault: Path):
    """raw/ 下同名但 sid 不同 → 改名 <stem>_<ts>_<4hex><ext>."""
    from corpus_bot.vault import pick_raw_target
    (vault / "raw" / "note.md").write_text("# original\n", encoding="utf-8")
    target = pick_raw_target(vault / "raw", "# completely new content\n", "note.md")
    # 不再指向原 note.md
    assert target != vault / "raw" / "note.md"
    # 后缀形如 note_<digits>_<4hex>.md
    name = target.name
    assert name.startswith("note_")
    assert name.endswith(".md")
    parts = name[:-3].split("_")  # 去掉 .md 再按 _ 拆
    # parts = ["note", ts, 4hex]
    assert len(parts) == 3
    assert parts[1].isdigit()
    assert len(parts[2]) == 4


def test_pick_raw_target_preserves_markdown_extension(vault: Path):
    from corpus_bot.vault import pick_raw_target
    (vault / "raw" / "deep.markdown").write_text("a\n")
    target = pick_raw_target(vault / "raw", "completely different\n", "deep.markdown")
    assert target.suffix == ".markdown"
    assert target.stem.startswith("deep_")
