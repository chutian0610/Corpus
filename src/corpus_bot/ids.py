"""ID 与 content-hash 生成。

核心约定（参考 .legacy/docs/content-hash-identity.md）：
- source_id = sha256(content)[:16]    # 16 hex chars (64-bit)
- concept_slug = slugify(title)        # filesystem-safe
- 撞名不同内容 → 自动 <stem>_<unix_ts>_<4hex>.md
- 同 hash → 拒收（duplicate）
"""

from __future__ import annotations

import hashlib
import re
import time

# 16 hex chars (64-bit entropy) — 撞 hash 概率 ~1e-9 在 10亿 规模内
SOURCE_ID_LENGTH = 16
SLUG_MAX_LENGTH = 80
def source_id_from_content(content: str | bytes) -> str:
    """生成 source_id = sha256(content)[:16]。

    跨 ingest 稳定；同内容永远同 id（dedup 基础）。
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()[:SOURCE_ID_LENGTH]


def slugify(title: str) -> str:
    """filesystem-safe slug。

    - 小写、空格 → '-'
    - 非字母数字字符 → '-'
    - 连续 '-' 合并
    - 截断到 SLUG_MAX_LENGTH（保留 word boundary）
    """
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if len(s) > SLUG_MAX_LENGTH:
        s = s[:SLUG_MAX_LENGTH].rsplit("-", 1)[0] if "-" in s[:SLUG_MAX_LENGTH] else s[:SLUG_MAX_LENGTH]
    return s or "untitled"


def rename_suffix() -> str:
    """生成撞名改名的后缀: ingest-<UTC compact ISO>, 形如 ingest-20260820-183000.

    与 _utc_now_iso() 一致用 UTC; 不带冒号 (Windows 文件名非法); 不带 Z (compact).
    同秒撞名概率对人类操作可忽略, 不再叠 4hex 随机.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("ingest-%Y%m%d-%H%M%S")
