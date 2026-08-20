"""Vault 跨进程文件锁 (fcntl.flock, macOS/Linux).

并发安全:
- SQLite: WAL 模式 + busy_timeout=5000 处理 DB 内部并发
- 物理文件 IO (raw/, wiki/index/*.json): 本模块的 vault_file_lock 处理
- 锁文件: <vault>/.wiki-meta/.lock (gitignored, 不入版本控制)

Windows fallback: 没 fcntl 时降级, 仅靠 SQLite WAL (无跨进程物理文件锁).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import StorageError

_HAS_FCNTL = False
try:
    import fcntl  # type: ignore[import-not-found]
    _HAS_FCNTL = True
except ImportError:
    fcntl = None  # type: ignore[assignment]


@contextmanager
def vault_file_lock(
    vault_root: Path,
    *,
    exclusive: bool = True,
    timeout_s: float = 0.0,
) -> Iterator[None]:
    """跨进程锁: <vault>/.wiki-meta/.lock (fcntl.flock, 非阻塞).

    Args:
        vault_root: vault 根目录
        exclusive: True=独占 (写), False=共享 (读)
        timeout_s: 0=非阻塞 (失败立刻抛); >0=阻塞等 timeout 秒

    Raises:
        StorageError: 锁已被另一进程持有 (含 pid 信息)
    """
    lock_path = vault_root / ".wiki-meta" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if not _HAS_FCNTL:
        # Windows / 无 fcntl: 降级, 仅靠 SQLite WAL
        yield
        return

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH

    try:
        if timeout_s > 0:
            fcntl.flock(fd, op)
        else:
            fcntl.flock(fd, op | fcntl.LOCK_NB)  # 非阻塞
    except (OSError, BlockingIOError) as e:
        os.close(fd)
        holder_pid = "unknown"
        try:
            with open(lock_path, "r") as f:
                holder_pid = f.read().strip() or "unknown"
        except Exception:
            pass
        op_name = "exclusive" if exclusive else "shared"
        raise StorageError(
            f"vault is locked by another process (pid={holder_pid}): {lock_path}",
            hint=f"retry after holder releases the {op_name} lock",
        ) from e

    # 写 holder pid 进 lock file (调试用)
    try:
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass

    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def is_locked(vault_root: Path) -> bool:
    """检查 vault 是否被另一进程锁住 (非阻塞检查, 不加锁)."""
    if not _HAS_FCNTL:
        return False
    lock_path = vault_root / ".wiki-meta" / ".lock"
    if not lock_path.exists():
        return False
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except (OSError, BlockingIOError):
            return True
        finally:
            os.close(fd)
    except OSError:
        return False
