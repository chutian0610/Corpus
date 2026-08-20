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
