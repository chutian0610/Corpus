"""Vault 布局 + 七条 validate_source_path。

Vault 目录结构：
    <vault_path>/
    ├── raw/                # 用户源资料（content-hash 唯一副本）
    │   ├── <source_id>.md  # 已入库
    │   └── <source_id>.md
    ├── wiki/
    │   ├── concept/        # daemon 生成的 wiki 页
    │   │   └── <slug>.md
    │   └── index/           # 全局索引
    │       ├── concepts.json
    │       └── sources.json
    └── .wiki-meta/          #  daemon 元数据（gitignore 友好）
        └── corpus.db       #  SQLite: sources / concepts / links / cooccurrence / certification_log

七条 validate_source_path：
1. 存在
2. 非 symlink
3. 常规文件（不是 device/socket/fifo）
4. 扩展名为 .md / .markdown / 无后缀
5. ≤50 MiB
6. canonicalize 后在 vault 内（防 ../  越界）
7. 在 .raw/ 子树下
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ConfigError, ValidationError

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MiB
ALLOWED_EXTENSIONS = frozenset({".md", ".markdown", ""})  # 无后缀也允许

# 默认子目录名
RAW_DIR = "raw"
WIKI_DIR = "wiki"
WIKI_CONCEPT_DIR = "concept"
WIKI_INDEX_DIR = "index"
META_DIR = ".wiki-meta"
CORPUS_DB = "corpus.db"


def vault_paths(vault_root: Path) -> dict[str, Path]:
    """返回 vault 标准路径表。"""
    return {
        "root": vault_root,
        "raw": vault_root / RAW_DIR,
        "wiki": vault_root / WIKI_DIR,
        "wiki_concept": vault_root / WIKI_DIR / WIKI_CONCEPT_DIR,
        "wiki_index": vault_root / WIKI_DIR / WIKI_INDEX_DIR,
        "meta": vault_root / META_DIR,
        "corpus_db": vault_root / META_DIR / CORPUS_DB,
    }


def ensure_vault(vault_root: Path) -> dict[str, Path]:
    """创建 vault 目录结构（如果不存在）。"""
    if not vault_root.exists():
        raise ConfigError(f"vault root does not exist: {vault_root}")

    paths = vault_paths(vault_root)
    paths["raw"].mkdir(parents=True, exist_ok=True)
    paths["wiki_concept"].mkdir(parents=True, exist_ok=True)
    paths["wiki_index"].mkdir(parents=True, exist_ok=True)
    paths["meta"].mkdir(parents=True, exist_ok=True)

    # gitignore .wiki-meta/  (让 daemon 元数据不进 git)
    gitignore = paths["meta"] / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# corpus-bot 元数据\n*\n!.gitignore\n")

    return paths


def validate_source_path(vault_root: Path, source_path: str) -> Path:
    """七条校验。失败抛 ValidationError(rule, path, message)。"""
    # Rule 1: 存在
    if not source_path:
        raise ValidationError("R1_missing", source_path, "empty path")

    src = Path(source_path)
    if not src.exists():
        raise ValidationError("R1_exists", source_path, "file does not exist")

    # Rule 2: 非 symlink
    if src.is_symlink():
        raise ValidationError("R2_symlink", source_path, "symlink not allowed")

    # Rule 3: 常规文件
    if not src.is_file():
        raise ValidationError("R3_regular", source_path, "not a regular file")

    # Rule 4: 扩展名
    ext = src.suffix
    if ext.lower() not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            "R4_extension", source_path, f"extension {ext!r} not allowed (use .md/.markdown)"
        )

    # Rule 5: 大小
    size = src.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValidationError(
            "R5_size", source_path, f"file too large ({size} bytes, max {MAX_FILE_SIZE})"
        )

    # Rule 6: canonicalize 后在 vault 内
    try:
        canonical = src.resolve(strict=True)
    except OSError as e:
        raise ValidationError("R6_canonical", source_path, f"canonicalize failed: {e}") from e

    vault_real = vault_root.resolve()
    try:
        canonical.relative_to(vault_real)
    except ValueError as e:
        raise ValidationError(
            "R6_canonical", source_path, "path is outside vault root"
        ) from e

    # Rule 7: 在 raw/ 子树下
    raw_root = vault_root / RAW_DIR
    raw_real = raw_root.resolve()
    try:
        canonical.relative_to(raw_real)
    except ValueError as e:
        raise ValidationError(
            "R7_raw_subtree", source_path, "path must be inside <vault>/raw/"
        ) from e

    return canonical


def pick_raw_target(raw_dir: Path, content: str, hint_name: str) -> Path:
    """撞名改名：raw/<hint_name> 已被不同内容占用 → <stem>_<ts>_<4hex><suffix>.

    同 sid (同一内容) → 直接返回原路径 (覆写是 idempotent, OK 的).
    raw_dir 不存在 / 文件不可读 → 直接返回原路径 (让 stage_source 决定).
    """
    from .ids import source_id_from_content, rename_suffix

    target = raw_dir / hint_name
    if not target.exists():
        return target

    try:
        existing_bytes = target.read_bytes()
    except OSError:
        return target

    if source_id_from_content(existing_bytes) == source_id_from_content(content):
        return target  # same content, overwrite is fine

    stem = Path(hint_name).stem
    suffix = Path(hint_name).suffix
    return raw_dir / f"{stem}_{rename_suffix()}{suffix}"

