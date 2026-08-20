"""Storage 层 —— 纯函数，未来可被 CLI 直接调，也可被 MCP thin wrapper 调。

核心设计：
- 每个 vault 一个 SQLite (corpus.db)，位于 <vault>/.wiki-meta/corpus.db
- 五张表：
  - sources: 源文件元数据 + 内容 + content_hash
  - concepts: wiki 页（slug + title + body + source_ids + links + 认证字段）
  - links: concept 之间的 wikilink 关系
  - cooccurrence: 同一 source 出现的 concept pair（阶段二用）
  - certification_log: 认证历史轨迹
- 所有函数返回纯 dict/list（不返回 ORM 对象），便于测试 + 序列化
- 不依赖任何 LLM / MCP / 异步运行时
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import ConflictError, StorageError

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    raw_path TEXT NOT NULL,
    original_filename TEXT,
    size_bytes INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'staged',  -- staged / committed / deleted
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE(content_hash)
);

CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);

CREATE TABLE IF NOT EXISTS concepts (
    concept_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_ids TEXT NOT NULL DEFAULT '[]',   -- JSON array
    links TEXT NOT NULL DEFAULT '[]',          -- JSON array of slugs
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- certification fields
    certified_at TEXT,
    certified_score REAL,
    certified_issues TEXT,                    -- JSON array
    certified_suggestions TEXT,               -- JSON array
    certified_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_concepts_slug ON concepts(slug);
CREATE INDEX IF NOT EXISTS idx_concepts_certified_at ON concepts(certified_at);

CREATE TABLE IF NOT EXISTS links (
    from_slug TEXT NOT NULL,
    to_slug TEXT NOT NULL,
    PRIMARY KEY (from_slug, to_slug)
);

CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_slug);

CREATE TABLE IF NOT EXISTS cooccurrence (
    slug_a TEXT NOT NULL,
    slug_b TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (slug_a, slug_b),
    CHECK (slug_a < slug_b)
);

CREATE TABLE IF NOT EXISTS certification_log (
    concept_id TEXT NOT NULL,
    certified_at TEXT NOT NULL,
    score REAL NOT NULL,
    issues TEXT,
    suggestions TEXT,
    certified_by TEXT NOT NULL,
    PRIMARY KEY (concept_id, certified_at)
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """打开 SQLite 连接，自动 WAL + foreign keys。"""
    if not db_path.parent.exists():
        raise StorageError(f"meta directory missing: {db_path.parent}")
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    """初始化 schema（幂等）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )


def is_initialized(db_path: Path) -> bool:
    return db_path.exists() and db_path.stat().st_size > 0


# ---------- sources ----------

def stage_source(
    db_path: Path,
    *,
    raw_path: Path,
    content: str,
    original_filename: str | None = None,
) -> dict[str, Any]:
    """落源到 sources 表。如果同 hash 已存在，抛 ConflictError。

    返回：{"source_id", "raw_path", "status", "size_bytes", "content_hash"}
    """
    from .ids import source_id_from_content  # lazy import

    content_bytes = content.encode("utf-8")
    sid = source_id_from_content(content_bytes)
    now = _utc_now_iso()

    with connect(db_path) as conn:
        existing = conn.execute(
            "SELECT source_id, raw_path FROM sources WHERE content_hash = ?",
            (hashlib_sha256_hex(content_bytes),),
        ).fetchone()
        if existing:
            raise ConflictError(
                f"duplicate content already staged as {existing['raw_path']}",
                hint=f"existing source_id: {existing['source_id']}",
            )
        try:
            conn.execute(
                """INSERT INTO sources
                (source_id, raw_path, original_filename, size_bytes, content_hash, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'staged', ?)""",
                (
                    sid,
                    str(raw_path),
                    original_filename,
                    len(content_bytes),
                    hashlib_sha256_hex(content_bytes),
                    now,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ConflictError(f"source_id collision: {sid}", hint=str(e)) from e

    return {
        "source_id": sid,
        "raw_path": str(raw_path),
        "status": "staged",
        "size_bytes": len(content_bytes),
        "content_hash": hashlib_sha256_hex(content_bytes),
        "created_at": now,
    }


def hashlib_sha256_hex(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()


def commit_source(db_path: Path, source_id: str) -> dict[str, Any]:
    """标记 source 为 committed（agent 完成 extract+write_concept 后调用）。"""
    now = _utc_now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE sources SET status='committed', committed_at=? WHERE source_id=?",
            (now, source_id),
        )
        if cur.rowcount == 0:
            raise StorageError(f"source not found: {source_id}")
    return {"source_id": source_id, "status": "committed", "committed_at": now}


def list_sources(
    db_path: Path,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM sources WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


def read_source(db_path: Path, source_id: str) -> dict[str, Any] | None:
    """读 source 的元数据 + raw_path（不读文件内容，让 CLI 读文件本身）。"""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------- concepts ----------

def write_concept(
    db_path: Path,
    *,
    slug: str,
    title: str,
    body: str,
    source_ids: list[str],
    links: list[str],
) -> dict[str, Any]:
    """写 concept（agent 自己用 LLM 生成 content 后调用）。

    如果 slug 已存在 → ConflictError。
    返回：{"concept_id", "slug", "wiki_path"}
    """
    import uuid
    now = _utc_now_iso()
    cid = f"c_{uuid.uuid4().hex[:12]}"
    with connect(db_path) as conn:
        try:
            conn.execute(
                """INSERT INTO concepts
                (concept_id, slug, title, body, source_ids, links, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    slug,
                    title,
                    body,
                    json.dumps(source_ids),
                    json.dumps(links),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ConflictError(
                f"concept slug already exists: {slug!r}",
                hint="use update_concept to modify existing, or pick a new slug",
            ) from e

        # 同步链接表
        for to_slug in links:
            conn.execute(
                "INSERT OR IGNORE INTO links (from_slug, to_slug) VALUES (?, ?)",
                (slug, to_slug),
            )

    return {"concept_id": cid, "slug": slug}


def update_concept(
    db_path: Path,
    slug: str,
    *,
    title: str | None = None,
    body: str | None = None,
    add_source_ids: list[str] | None = None,
    add_links: list[str] | None = None,
) -> dict[str, Any]:
    """增量更新 concept。返回 {"slug", "updated_at"}"""
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT concept_id, source_ids, links FROM concepts WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            raise StorageError(f"concept not found: {slug}")

        updates: list[str] = []
        params: list[Any] = []
        if title is not None:
            updates.append("title=?")
            params.append(title)
        if body is not None:
            updates.append("body=?")
            params.append(body)
        if updates:
            updates.append("updated_at=?")
            params.append(now)
            params.append(slug)
            conn.execute(
                f"UPDATE concepts SET {', '.join(updates)} WHERE slug=?",
                params,
            )

        if add_source_ids:
            existing = set(json.loads(row["source_ids"]))
            existing.update(add_source_ids)
            conn.execute(
                "UPDATE concepts SET source_ids=?, updated_at=? WHERE slug=?",
                (json.dumps(sorted(existing)), now, slug),
            )

        if add_links:
            existing_links = set(json.loads(row["links"]))
            existing_links.update(add_links)
            conn.execute(
                "UPDATE concepts SET links=?, updated_at=? WHERE slug=?",
                (json.dumps(sorted(existing_links)), now, slug),
            )
            for to_slug in add_links:
                conn.execute(
                    "INSERT OR IGNORE INTO links (from_slug, to_slug) VALUES (?, ?)",
                    (slug, to_slug),
                )

    return {"slug": slug, "updated_at": now}


def read_concept(db_path: Path, slug: str) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT c.*, GROUP_CONCAT(r.from_slug) AS incoming_links
            FROM concepts c
            LEFT JOIN links r ON r.to_slug = c.slug
            WHERE c.slug=?
            GROUP BY c.concept_id""",
            (slug,),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["source_ids"] = json.loads(out["source_ids"])
    out["links"] = json.loads(out["links"])
    out["incoming_links"] = [s for s in (out.get("incoming_links") or "").split(",") if s]
    if out["certified_issues"]:
        out["certified_issues"] = json.loads(out["certified_issues"])
    if out["certified_suggestions"]:
        out["certified_suggestions"] = json.loads(out["certified_suggestions"])
    return out


def list_concepts(
    db_path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM concepts ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = json.loads(d["source_ids"])
        d["links"] = json.loads(d["links"])
        out.append(d)
    return out


def list_uncertified_concepts(
    db_path: Path,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """列还没被认证（或认证已过期）的 concept。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM concepts WHERE certified_at IS NULL ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = json.loads(d["source_ids"])
        d["links"] = json.loads(d["links"])
        out.append(d)
    return out


def find_concept_by_link(db_path: Path, link_target: str) -> list[dict[str, Any]]:
    """解析 [[wikilink]] 到 concept。link_target 通常是 wikilink 文本（可能含空格 / 大小写）。

    策略：先用 slugify(link_target) 精确匹配；如果没找到，再 LIKE 模糊匹配。
    """
    from .ids import slugify
    exact_slug = slugify(link_target)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM concepts WHERE slug=?", (exact_slug,)
        ).fetchone()
        if row:
            d = dict(row)
            d["source_ids"] = json.loads(d["source_ids"])
            d["links"] = json.loads(d["links"])
            return [d]
        # 模糊匹配
        like_pattern = f"%{exact_slug}%"
        rows = conn.execute(
            "SELECT * FROM concepts WHERE slug LIKE ? LIMIT 10",
            (like_pattern,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = json.loads(d["source_ids"])
        d["links"] = json.loads(d["links"])
        out.append(d)
    return out


# ---------- certification ----------

def mark_certified(
    db_path: Path,
    *,
    slug: str,
    score: float,
    issues: list[str],
    suggestions: list[str],
    certified_by: str = "agent",
) -> dict[str, Any]:
    """标记 concept 已认证。同时写 certification_log。"""
    if not 0.0 <= score <= 1.0:
        raise StorageError(f"score must be in [0, 1], got {score}")
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT concept_id FROM concepts WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            raise StorageError(f"concept not found: {slug}")

        conn.execute(
            """UPDATE concepts SET
            certified_at=?, certified_score=?, certified_issues=?,
            certified_suggestions=?, certified_by=?, updated_at=?
            WHERE slug=?""",
            (
                now,
                score,
                json.dumps(issues),
                json.dumps(suggestions),
                certified_by,
                now,
                slug,
            ),
        )
        conn.execute(
            """INSERT INTO certification_log
            (concept_id, certified_at, score, issues, suggestions, certified_by)
            VALUES (?, ?, ?, ?, ?, ?)""",
                (row["concept_id"], now, score, json.dumps(issues), json.dumps(suggestions), certified_by),
        )

    return {"slug": slug, "score": score, "certified_at": now}


def unmark_certified(db_path: Path, slug: str) -> dict[str, Any]:
    """撤销认证（让 list_uncertified_concepts 能再列出来）。"""
    now = _utc_now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            """UPDATE concepts SET
            certified_at=NULL, certified_score=NULL, certified_issues=NULL,
            certified_suggestions=NULL, certified_by=NULL, updated_at=?
            WHERE slug=?""",
            (now, slug),
        )
        if cur.rowcount == 0:
            raise StorageError(f"concept not found: {slug}")
    return {"slug": slug, "unmarked_at": now}


def certification_stats(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM concepts").fetchone()["n"]
        certified = conn.execute(
            "SELECT COUNT(*) AS n FROM concepts WHERE certified_at IS NOT NULL"
        ).fetchone()["n"]
        avg = conn.execute(
            "SELECT AVG(certified_score) AS s FROM concepts WHERE certified_at IS NOT NULL"
        ).fetchone()["s"]
        buckets = conn.execute(
            """SELECT
              SUM(CASE WHEN certified_score < 0.5 THEN 1 ELSE 0 END) AS lt50,
              SUM(CASE WHEN certified_score >= 0.5 AND certified_score < 0.7 THEN 1 ELSE 0 END) AS lt70,
              SUM(CASE WHEN certified_score >= 0.7 AND certified_score < 0.9 THEN 1 ELSE 0 END) AS lt90,
              SUM(CASE WHEN certified_score >= 0.9 THEN 1 ELSE 0 END) AS ge90
            FROM concepts WHERE certified_at IS NOT NULL"""
        ).fetchone()
    return {
        "total_concepts": total,
        "certified": certified,
        "uncertified": total - certified,
        "avg_score": round(avg, 3) if avg is not None else None,
        "score_distribution": {
            "<0.5": buckets["lt50"] or 0,
            "0.5-0.7": buckets["lt70"] or 0,
            "0.7-0.9": buckets["lt90"] or 0,
            ">=0.9": buckets["ge90"] or 0,
        },
    }


# ---------- FTS5 search (stage 1: titles only, stage 3 will add full-text) ----------

def search_concepts(db_path: Path, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Stage 1: 仅按 title LIKE 搜索（占位）。Stage 3 会接 FTS5。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM concepts WHERE title LIKE ? OR slug LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = json.loads(d["source_ids"])
        d["links"] = json.loads(d["links"])
        out.append(d)
    return out
