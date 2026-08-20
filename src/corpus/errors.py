"""错误模型。

corpus 错误分类：
- ConfigError: 配置错误（vault 不存在、参数非法）
- ValidationError: 七条 validate_source_path 失败
- ConflictError: 业务冲突（slug 重复、source 已存在）
- StorageError: SQLite/文件系统错误
"""

from __future__ import annotations


class CorpusBotError(Exception):
    """corpus 基类错误。"""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(CorpusBotError):
    """配置/参数错误（vault 不存在、glob 无效等）。"""


class ValidationError(CorpusBotError):
    """七条 validate_source_path 校验失败。"""

    def __init__(self, rule: str, path: str, message: str) -> None:
        super().__init__(f"[{rule}] {path}: {message}")
        self.rule = rule
        self.path = path
        self.message = message


class ConflictError(CorpusBotError):
    """业务冲突（slug 重复、source 同 hash 已存在）。"""


class StorageError(CorpusBotError):
    """SQLite/文件系统层错误。"""


class OptimisticLockError(ConflictError):
    """乐观锁冲突: 并发 update 时 expected_updated_at 不匹配.

    agent 端 read-modify-write 工作流:
      1. read_concept 拿 current.updated_at
      2. 决定修改 (LLM merge / 业务逻辑)
      3. update_concept(expected_updated_at=current.updated_at, ...) 提交
      4. 抛 OptimisticLockError -> 回 1 重新 read + merge (中间被另一 agent 改了)
    """

