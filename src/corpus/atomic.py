"""原子文件写入: 写 tmp + os.replace.

防 race:
- 多个 writer 同时写同一目标文件 → tmp 是 unique (uuid suffix), rename 原子替换
- 写过程 crash → tmp 留在 .wiki-meta/.tmp/ (可用 lock 后续清理)

并发模型:
- corpus CLI 多 agent 并行 ingest 不同 source → raw_path 都 unique (pick_raw_target 加 timestamp),
  atomic write 防同秒同 hash 同名 race
- 写入失败 (磁盘满 / crash) → tmp 残留 .wiki-meta/.tmp/, 但 target 文件原样
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from .errors import StorageError


def atomic_write_text(
    target: Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """原子写文本到 target.

    步骤:
    1. <parent>/.tmp/<target_basename>.<uuid8>.tmp  (unique tmp, 同目录)
    2. write content to tmp
    3. os.replace(tmp, target)  (POSIX atomic, 同一文件系统)

    Raises:
        OSError: 写或 rename 失败
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = parent / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f".{target.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp_path.write_text(content, encoding=encoding)
        os.replace(tmp_path, target)
    except OSError:
        # 清理 tmp
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def cleanup_tmp(parent: Path) -> int:
    """清理 <parent>/.tmp/ 残留文件 (写失败留下的). 返回清理数."""
    tmp_dir = parent / ".tmp"
    if not tmp_dir.exists():
        return 0
    n = 0
    for f in tmp_dir.iterdir():
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
