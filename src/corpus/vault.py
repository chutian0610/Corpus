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

from .errors import ConfigError, StorageError, ValidationError

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MiB
ALLOWED_EXTENSIONS = frozenset({".md", ".markdown", ""})  # 无后缀也允许

# 默认子目录名
RAW_DIR = "raw"
WIKI_DIR = "wiki"
WIKI_CONCEPT_DIR = "concept"
WIKI_INDEX_DIR = "index"
META_DIR = ".wiki-meta"
CORPUS_DB = "corpus.db"


def _ensure_git_repo(vault_root: Path) -> dict[str, Any]:
    """在 vault_root 跑 'git init' (幂等). 返回 {'git_initialized', 'git_path'}.

    - vault_root/.git/ 已存在 → 不重复 init, 返回 'git_initialized': False
    - git 不在 PATH → 返回 {'git_initialized': False, 'reason': 'git not in PATH'}
    - 其它 git 错误 → raise StorageError (vault init 失败, 不算 vault 的错)
    """
    import shutil
    import subprocess
    from .errors import StorageError

    if not shutil.which("git"):
        return {"git_initialized": False, "reason": "git not in PATH"}

    git_dir = vault_root / ".git"
    if git_dir.exists():
        return {"git_initialized": False, "reason": "already a git repository"}

    try:
        result = subprocess.run(
            ["git", "init", "--initial-branch=main", str(vault_root)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as e:
        raise StorageError(f"git init timed out: {e}") from e
    except OSError as e:
        raise StorageError(f"git init failed: {e}") from e

    if result.returncode != 0:
        raise StorageError(
            f"git init failed (rc={result.returncode}): {result.stderr.strip()}"
        )

    return {
        "git_initialized": True,
        "git_path": str(git_dir),
    }


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
        gitignore.write_text("# corpus 元数据\n*\n!.gitignore\n")

    return paths


def validate_source_path_basic(source_path: Path, vault_root: Path) -> Path:
    """Rule 1-5 基础校验 (不限制 path 位置).

    适用场景: 'sources ingest' 接受 vault 外的文件, 只需要基础校验.
    返回 canonical (resolve strict=True) 的 Path.

    Rule 1: 存在
    Rule 2: 非 symlink
    Rule 3: 常规文件
    Rule 4: 扩展名 (.md / .markdown / 无后缀)
    Rule 5: ≤50 MiB
    """
    if not source_path:
        raise ValidationError("R1_missing", str(source_path), "empty path")
    if not source_path.exists():
        raise ValidationError("R1_exists", str(source_path), "file does not exist")
    if source_path.is_symlink():
        raise ValidationError("R2_symlink", str(source_path), "symlink not allowed")
    if not source_path.is_file():
        raise ValidationError("R3_regular", str(source_path), "not a regular file")

    ext = source_path.suffix
    if ext.lower() not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            "R4_extension", str(source_path),
            f"extension {ext!r} not allowed (use .md/.markdown)",
        )

    size = source_path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValidationError(
            "R5_size", str(source_path),
            f"file too large ({size} bytes, max {MAX_FILE_SIZE})",
        )

    try:
        return source_path.resolve(strict=True)
    except OSError as e:
        raise ValidationError("R6_canonical", str(source_path), f"canonicalize failed: {e}") from e


def assert_source_outside_vault(canonical: Path, vault_root: Path, raw_dir: Path) -> None:
    """检查 canonical 不在 vault 内 (避免重复 ingest vault 内已有文件).

    - 在 raw/ 内 → 报 'already in vault raw/'
    - 在 vault 内但非 raw/ (wiki/, .wiki-meta/) → 报 'forbidden internal vault dir'
    - 在 vault 外 → 通过
    """
    vault_real = vault_root.resolve()
    try:
        canonical.relative_to(vault_real)
    except ValueError:
        return  # 在 vault 外, OK

    # 在 vault 内, 区分 raw/ vs 其它内部目录
    try:
        canonical.relative_to(raw_dir.resolve())
        raise StorageError(
            f"path is inside vault raw/: {canonical}",
            hint="raw/ 是 ingest 产物目录, 不重复 ingest. "
                 "如要重新入库同一文件, 先 sources delete <sid>.",
        )
    except ValueError:
        pass  # 不在 raw/, 是 vault 内部目录

    raise StorageError(
        f"path is inside vault (forbidden internal directory): {canonical}",
        hint="ingest 只接受 vault 外的文件. "
             "vault 内部目录 (wiki/, .wiki-meta/) 禁止 ingest.",
    )


def validate_source_path(vault_root: Path, source_path: str) -> Path:
    """七条校验 (Rule 1-5 + Rule 6 在 vault 内 + Rule 7 在 raw/ 子树).

    适用场景: 未来需要严格 in-vault 检查的命令 (e.g. 'sources re-stage').
    当前 sources ingest 用 validate_source_path_basic + assert_source_outside_vault
    (因为 ingest 是 '从 vault 外拉进来' 的语义).
    """
    src = Path(source_path)
    canonical = validate_source_path_basic(src, vault_root)

    vault_real = vault_root.resolve()
    try:
        canonical.relative_to(vault_real)
    except ValueError as e:
        raise ValidationError(
            "R6_canonical", source_path, "path is outside vault root"
        ) from e

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
    """所有 ingest 都生成 raw/<stem>-ingest-<UTC compact ISO><suffix>.

    默认加 ingest 时间戳后缀 (与 stage_source 写入一一对应):
      - 同内容二次 ingest 会被 content_hash dedup 在 stage_source 拦下,
        根本走不到改名, 所以 idempotent 不依赖文件名
      - 撞名 (同秒入库) 概率对人类操作可忽略, 不再叠 hash 随机
      - DB 已存 original_filename 字段供 source 元数据检索, raw 文件名仅做内部唯一标识

    返回路径不保证对应 raw/ 里已存在的文件 (caller stage_source + write_text 处理).
    """
    from .ids import rename_suffix

    stem = Path(hint_name).stem
    suffix = Path(hint_name).suffix
    return raw_dir / f"{stem}-{rename_suffix()}{suffix}"

