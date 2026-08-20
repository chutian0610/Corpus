"""Markdown frontmatter 读写 (YAML in ---\\n...\\n---).

corpus 的 wiki 文件 (concept / source page) 用 YAML frontmatter 存 metadata
(替代 SQL DB 作为 query cache, git 跟踪作为 source of truth).

## 格式

    ---
    key: value
    list:
      - item1
      - item2
    ---

    # Body (Markdown)

## 用法

```python
meta, body = read_md_with_frontmatter(path)
# meta: dict (yaml.safe_load)
# body: str (frontmatter 之后的内容)

write_md_with_frontmatter(path, meta={"slug": "x", ...}, body="# hello")
```

## 设计选择

- 用 pyyaml (PyYAML) 解析, 安全模式 (yaml.safe_load, 不执行任意 Python 对象)
- atomic write: 写 tmp + os.replace (防半写)
- frontmatter 必须 UTF-8
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import yaml


def read_md_with_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """读 markdown 文件, 返 (frontmatter dict, body).

    无 frontmatter: 返 ({}, 全文).
    frontmatter 解析失败: 返 ({}, 全文) + 不抛错 (让 vault 仍可用, 降级用 DB).

    例:
        ---
        slug: x
        ---
        # body
    """
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    return parse_md_text(text)


def parse_md_text(text: str) -> tuple[dict[str, Any], str]:
    """parse markdown 文本. 同上, 文本版 (方便测试)."""
    if not text.startswith("---"):
        return {}, text
    # 找第二个 ---
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    yaml_part = text[4:end]
    body = text[end + 5:]
    try:
        meta = yaml.safe_load(yaml_part) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, body


def write_md_with_frontmatter(
    path: Path,
    meta: dict[str, Any],
    body: str,
) -> None:
    """写 markdown 文件 (frontmatter + body), atomic.

    步骤:
    1. 拼 frontmatter YAML + body
    2. 写 tmp file (parent/.tmp/<name>.<uuid8>.tmp)
    3. os.replace 原子替换 target
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = parent / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"

    yaml_str = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    content = f"---\n{yaml_str}---\n\n{body}"

    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
