"""Storage 层 —— 纯函数，未来可被 CLI 直接调，也可被 MCP thin wrapper 调。

核心设计：
- 每个 vault 一个 SQLite (corpus.db)，位于 <vault>/.wiki-meta/corpus.db
- 六张表：
  - sources: 源文件元数据 + 内容 + content_hash
  - concepts: wiki 页（slug + title + body + source_ids + links + is_orphan + 认证字段）
  - links: concept 之间的 wikilink 关系
  - cooccurrence: 同一 source 出现的 concept pair（阶段二用）
  - extractions: 抽取元数据 + 证据（source ↔ concept 的中间表，含 quote_span）
  - certification_log: 认证历史轨迹
- 所有函数返回纯 dict/list（不返回 ORM 对象），便于测试 + 序列化
- 不依赖任何 LLM / MCP / 异步运行时

source ↔ concept 映射规则：
- concepts.source_ids：JSON array（set 语义，去重），表示"这个 concept 来自哪些 source"
- extractions 表：每个抽取动作一行（含 quote_span / extracted_at / prompt_version / source_content_hash）
- 删 source（软删 status='deleted'）→ 自动从引用它的 concept.source_ids 移除这个 sid
- 如果 concept.source_ids 变空 → 自动 is_orphan=1（agent 决定怎么清理）
- write_concept 必须传 quote_spans（每个 source_id 一段原文证据）
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import ConflictError, StorageError

SCHEMA_VERSION = 2

# Migration 步骤表: key=(from_v, to_v), value=函数(conn)
_MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): "_migrate_1_to_2",
}

def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """v1 → v2: 去掉 sources.content_hash 的 UNIQUE 约束.

    SQLite 没有 ALTER TABLE DROP CONSTRAINT，标准做法是 rename + recreate.
    """
    conn.executescript(
        """
        ALTER TABLE sources RENAME TO sources__v1;
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            raw_path TEXT NOT NULL,
            original_filename TEXT,
            size_bytes INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'staged',
            created_at TEXT NOT NULL,
            committed_at TEXT,
            deleted_at TEXT,
            deleted_reason TEXT
        );
        INSERT INTO sources
            SELECT source_id, raw_path, original_filename, size_bytes,
                   content_hash, status, created_at, committed_at,
                   deleted_at, deleted_reason
            FROM sources__v1;
        DROP TABLE sources__v1;
        CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
        CREATE INDEX IF NOT EXISTS idx_sources_content_hash ON sources(content_hash);
        """
    )


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
    deleted_at TEXT,
    deleted_reason TEXT
    -- 注: content_hash 不再 UNIQUE. 软删后允许同 hash 复活.
    -- dedup 由 stage_source() 自己处理 (按 status 区分).
);

CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_content_hash ON sources(content_hash);

CREATE TABLE IF NOT EXISTS concepts (
    concept_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_ids TEXT NOT NULL DEFAULT '[]',   -- JSON array (set 语义)
    links TEXT NOT NULL DEFAULT '[]',         -- JSON array of slugs
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_orphan INTEGER NOT NULL DEFAULT 0,    -- 1 = source_ids 为空
    -- certification fields
    certified_at TEXT,
    certified_score REAL,
    certified_issues TEXT,                   -- JSON array
    certified_suggestions TEXT,              -- JSON array
    certified_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_concepts_slug ON concepts(slug);
CREATE INDEX IF NOT EXISTS idx_concepts_certified_at ON concepts(certified_at);
CREATE INDEX IF NOT EXISTS idx_concepts_is_orphan ON concepts(is_orphan);

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

CREATE TABLE IF NOT EXISTS extractions (
    extraction_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    concept_slug TEXT NOT NULL,
    -- 抽取的证据
    quote_span TEXT,                       -- source 里支撑这个 concept 的原文片段（必填）
    char_start INTEGER,                    -- 在 source 文件中的位置（可选）
    char_end INTEGER,
    -- 抽取的元数据
    extracted_at TEXT NOT NULL,
    extracted_by TEXT NOT NULL,             -- 'agent' / 'user'
    prompt_version TEXT,                    -- 'extract-v1' / 'extract-v2'
    confidence REAL,                        -- 0.0-1.0
    -- 防 source 改后审计失效：抽取时的 content_hash
    source_content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractions_pair ON extractions(source_id, concept_slug);

CREATE INDEX IF NOT EXISTS idx_extractions_source ON extractions(source_id);
CREATE INDEX IF NOT EXISTS idx_extractions_concept ON extractions(concept_slug);

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


def _new_uuid12(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _hash(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """打开 SQLite 连接，自动 foreign keys + autocommit。"""
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
    """初始化 schema（幂等），并按需跑 migration。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            # 旧库: 无 schema_meta, 假设是 v1 (旧 DDL 含 UNIQUE(content_hash)).
            current = 1
        else:
            current = int(row["value"])
        if current < SCHEMA_VERSION:
            _run_migrations(conn, current, SCHEMA_VERSION)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )


def _run_migrations(conn: sqlite3.Connection, from_v: int, to_v: int) -> None:
    """顺序跑 from_v+1..to_v 的 migration 步骤. 用 getattr 推迟查函数名 (避免 forward ref)."""
    import sys
    mod = sys.modules[__name__]
    for v in range(from_v + 1, to_v + 1):
        step_name = _MIGRATIONS.get((v - 1, v))
        if step_name is None:
            raise StorageError(f"no migration path from v{v-1} to v{v}")
        step = getattr(mod, step_name, None)
        if step is None:
            raise StorageError(f"migration step {step_name} not defined")
        step(conn)


def is_initialized(db_path: Path) -> bool:
    return db_path.exists() and db_path.stat().st_size > 0


def _parse_json_list(s: str | None) -> list[Any]:
    if not s:
        return []
    return json.loads(s)


# ============================================================================
# sources
# ============================================================================

def stage_source(
    db_path: Path,
    *,
    raw_path: Path,
    content: str,
    original_filename: str | None = None,
    revive_on_deleted: bool = False,
) -> dict[str, Any]:
    """落源到 sources 表。dedup by content_hash, status-aware:

    - 同 hash 且 status in (staged, committed) → 抛 ConflictError.
    - 同 hash 且 status='deleted':
        - revive_on_deleted=False → 抛 ConflictError (提示 --force-revive).
        - revive_on_deleted=True  → UPDATE 复活 (status='staged', 刷新 raw_path/content_hash).
    - 不同 hash → 新插入.

    返回: {"source_id", "raw_path", "status", "size_bytes", "content_hash", "revived": bool}
    """
    content_bytes = content.encode("utf-8")
    sid = _hash(content_bytes)[:16]
    content_hash = _hash(content_bytes)
    now = _utc_now_iso()

    with connect(db_path) as conn:
        existing = conn.execute(
            "SELECT source_id, raw_path, status FROM sources WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing:
            existing_status = existing["status"]
            existing_id = existing["source_id"]
            if existing_status == "deleted":
                if not revive_on_deleted:
                    raise ConflictError(
                        f"duplicate content exists but is deleted: {existing_id}",
                        hint="use --force-revive to restore this source",
                    )
                # 复活: 保留 source_id 不变 (extractions / concepts 引用稳定),
                # 清掉 deleted_* 字段, 刷新 raw_path / size / content_hash / created_at.
                conn.execute(
                    """UPDATE sources SET
                        status='staged',
                        raw_path=?,
                        original_filename=?,
                        size_bytes=?,
                        content_hash=?,
                        created_at=?,
                        committed_at=NULL,
                        deleted_at=NULL,
                        deleted_reason=NULL
                    WHERE source_id=?""",
                    (
                        str(raw_path),
                        original_filename,
                        len(content_bytes),
                        content_hash,
                        now,
                        existing_id,
                    ),
                )
                return {
                    "source_id": existing_id,
                    "raw_path": str(raw_path),
                    "status": "staged",
                    "size_bytes": len(content_bytes),
                    "content_hash": content_hash,
                    "created_at": now,
                    "revived": True,
                }
            # active (staged or committed)
            raise ConflictError(
                f"duplicate content already staged as {existing['raw_path']}",
                hint=f"existing source_id: {existing_id}, status={existing_status}",
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
                    content_hash,
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
        "content_hash": content_hash,
        "created_at": now,
        "revived": False,
    }


def commit_source(db_path: Path, source_id: str) -> dict[str, Any]:
    """标记 source 为 committed。

    已 deleted 的 source 不能 commit。
    """
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if not row:
            raise StorageError(f"source not found: {source_id}")
        if row["status"] == "deleted":
            raise ConflictError(
                f"source is deleted, cannot commit: {source_id}",
                hint="deleted sources are immutable; restore is not supported",
            )
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
    include_deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列源。默认不显示 deleted 状态的源。"""
    with connect(db_path) as conn:
        if include_deleted:
            if status and status != "all":
                rows = conn.execute(
                    "SELECT * FROM sources WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sources ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        else:
            if status and status != "all":
                rows = conn.execute(
                    "SELECT * FROM sources WHERE status=? AND status!='deleted' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                ).fetchall()
            elif status == "all":
                rows = conn.execute(
                    "SELECT * FROM sources WHERE status!='deleted' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sources WHERE status!='deleted' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
    return [dict(r) for r in rows]


def read_source(db_path: Path, source_id: str) -> dict[str, Any] | None:
    """读 source 的元数据（不读文件内容，让 CLI 读文件本身）。

    deleted 状态的 source 也返回（status 字段告诉 caller）。
    """
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
    return dict(row) if row else None


def soft_delete_source(
    db_path: Path,
    source_id: str,
    *,
    deleted_reason: str | None = None,
) -> dict[str, Any]:
    """软删 source（status='deleted'），不级联删 concept。

    1. 标记 sources.status='deleted'
    2. 找出所有引用这个 source_id 的 concept
    3. 从它们的 source_ids 中移除这个 sid
    4. 如果 concept 的 source_ids 变空，自动 is_orphan=1
    5. 保留 extractions 行（audit trail）

    Returns: {
        "source_id": str,
        "deleted_at": iso,
        "affected_concepts": [{"slug", "is_orphan": bool}, ...],
        "orphans_created": int,
    }
    """
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if not row:
            raise StorageError(f"source not found: {source_id}")
        if row["status"] == "deleted":
            raise ConflictError(f"source already deleted: {source_id}")

        # 1. 标记 source 为 deleted
        conn.execute(
            "UPDATE sources SET status='deleted', deleted_at=?, deleted_reason=? WHERE source_id=?",
            (now, deleted_reason, source_id),
        )

        # 2. 找出引用这个 source 的 concept
        affected = conn.execute(
            """SELECT slug, source_ids FROM concepts
            WHERE source_ids LIKE ?""",
            (f'%"{source_id}"%',),
        ).fetchall()

        # 3. 从 source_ids 中移除 + 4. 检测 orphan
        affected_concepts = []
        orphans_created = 0
        for r in affected:
            old_ids = set(_parse_json_list(r["source_ids"]))
            new_ids = old_ids - {source_id}
            is_orphan_now = 1 if len(new_ids) == 0 else 0
            if len(old_ids) == len(new_ids):
                continue  # 没引用这个 source（罕见，LIKE 误中）
            conn.execute(
                "UPDATE concepts SET source_ids=?, is_orphan=?, updated_at=? WHERE slug=?",
                (json.dumps(sorted(new_ids)), is_orphan_now, now, r["slug"]),
            )
            affected_concepts.append({
                "slug": r["slug"],
                "source_ids": sorted(new_ids),
                "is_orphan": bool(is_orphan_now),
            })
            if is_orphan_now:
                orphans_created += 1

    return {
        "source_id": source_id,
        "deleted_at": now,
        "deleted_reason": deleted_reason,
        "affected_concepts": affected_concepts,
        "orphans_created": orphans_created,
    }


def dry_run_delete_source(db_path: Path, source_id: str) -> dict[str, Any]:
    """预览：删 source 会影响什么。"""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if not row:
            raise StorageError(f"source not found: {source_id}")

        affected = conn.execute(
            """SELECT slug, source_ids FROM concepts
            WHERE source_ids LIKE ?""",
            (f'%"{source_id}"%',),
        ).fetchall()

    would_be_orphans = []
    still_supported = []
    for r in affected:
        old_ids = set(_parse_json_list(r["source_ids"]))
        if source_id in old_ids:
            new_ids = old_ids - {source_id}
            if len(new_ids) == 0:
                would_be_orphans.append(r["slug"])
            else:
                still_supported.append({"slug": r["slug"], "remaining_sources": sorted(new_ids)})

    return {
        "source_id": source_id,
        "current_status": row["status"],
        "already_deleted": row["status"] == "deleted",
        "affected_concepts_count": len(affected),
        "would_become_orphans": would_be_orphans,
        "still_supported": still_supported,
        "recommendation": (
            "aborted: source not found"
            if not row else
            ("blocked: source already deleted" if row["status"] == "deleted" else
             ("safe to delete: no concept references this source" if not affected else
              f"will orphan {len(would_be_orphans)} concept(s) if deleted"
             ))
        ),
    }


# ============================================================================
# concepts + extractions
# ============================================================================

def write_concept(
    db_path: Path,
    *,
    slug: str,
    title: str,
    body: str,
    extractions_data: list[dict[str, Any]],
    links: list[str],
    prompt_version: str | None = None,
    extracted_by: str = "agent",
) -> dict[str, Any]:
    """写一篇 wiki concept。slug 已存在 → ConflictError。

    必传 extractions_data：每个 source_id 对应一段 quote_span 证据。
    格式：[{"source_id": "abc", "quote_span": "...", "char_start": 0, "char_end": 100, "confidence": 0.9}, ...]
    - source_ids 从 extractions_data 自动推导
    - 至少 1 个 extraction（无 source 的 concept 不允许）

    返回：{
        "concept_id", "slug", "wiki_path",
        "extraction_ids": [...],
        "source_ids": [...]
    }
    """
    if not extractions_data:
        raise StorageError(
            "extractions_data cannot be empty",
            hint="at least one extraction with quote_span is required",
        )

    # 校验 extractions_data
    source_ids_set: set[str] = set()
    for ed in extractions_data:
        sid = ed.get("source_id")
        qs = ed.get("quote_span")
        if not sid:
            raise StorageError("extraction missing source_id")
        if not qs or not qs.strip():
            raise StorageError(
                f"extraction for {sid} missing quote_span",
                hint="quote_span is required: the original text in the source that supports this concept",
            )
        source_ids_set.add(sid)
    source_ids = sorted(source_ids_set)
    links = _validate_links(slug, links)

    now = _utc_now_iso()
    cid = _new_uuid12("c_")
    extraction_ids = []

    with connect(db_path) as conn:
        # 1. 校验所有 source_id 存在且不是 deleted
        for sid in source_ids:
            row = conn.execute(
                "SELECT status, content_hash FROM sources WHERE source_id=?", (sid,)
            ).fetchone()
            if not row:
                raise StorageError(f"source_id not found: {sid}")
            if row["status"] == "deleted":
                raise ConflictError(
                    f"cannot write concept from deleted source: {sid}",
                    hint="deleted sources are immutable; restore is not supported",
                )

        # 2. 写入 concept
        try:
            conn.execute(
                """INSERT INTO concepts
                (concept_id, slug, title, body, source_ids, links, created_at, updated_at, is_orphan)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
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

        # 3. 写入 extractions 表
        for ed in extractions_data:
            ext_id = _new_uuid12("e_")
            conn.execute(
                """INSERT INTO extractions
                (extraction_id, source_id, concept_slug, quote_span,
                 char_start, char_end, extracted_at, extracted_by,
                 prompt_version, confidence, source_content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ext_id,
                    ed["source_id"],
                    slug,
                    ed["quote_span"],
                    ed.get("char_start"),
                    ed.get("char_end"),
                    now,
                    extracted_by,
                    prompt_version,
                    ed.get("confidence"),
                    _fetch_source_content_hash(conn, ed["source_id"]),
                ),
            )
            extraction_ids.append(ext_id)

        # 4. 同步链接表
        for to_slug in links:
            conn.execute(
                "INSERT OR IGNORE INTO links (from_slug, to_slug) VALUES (?, ?)",
                (slug, to_slug),
            )

    return {
        "concept_id": cid,
        "slug": slug,
        "source_ids": source_ids,
        "extraction_ids": extraction_ids,
    }


def _fetch_source_content_hash(conn: sqlite3.Connection, source_id: str) -> str:
    row = conn.execute(
        "SELECT content_hash FROM sources WHERE source_id=?", (source_id,)
    ).fetchone()
    if not row:
        raise StorageError(f"source not found: {source_id}")
    return row["content_hash"]


def update_concept(
    db_path: Path,
    slug: str,
    *,
    title: str | None = None,
    body: str | None = None,
    add_extractions: list[dict[str, Any]] | None = None,
    add_links: list[str] | None = None,
    prompt_version: str | None = None,
    extracted_by: str = "agent",
) -> dict[str, Any]:
    """增量更新 concept。

    add_extractions: 格式同 write_concept 的 extractions_data。
    自动去重 source_ids + 更新 extractions 表 + 自动清 is_orphan=0。
    """
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT concept_id, source_ids, links FROM concepts WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            raise StorageError(f"concept not found: {slug}")
        concept_id = row["concept_id"]

        old_source_ids = set(_parse_json_list(row["source_ids"]))
        old_links = set(_parse_json_list(row["links"]))
        if add_links:
            add_links = _validate_links(slug, list(add_links))

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

        # add_extractions
        added_source_ids: list[str] = []
        extraction_ids: list[str] = []
        if add_extractions:
            for ed in add_extractions:
                sid = ed.get("source_id")
                qs = ed.get("quote_span")
                if not sid:
                    raise StorageError("extraction missing source_id")
                if not qs or not qs.strip():
                    raise StorageError(
                        f"extraction for {sid} missing quote_span",
                        hint="quote_span is required: the original text in the source that supports this concept",
                    )
                # source 是否 deleted?
                src_row = conn.execute(
                    "SELECT status FROM sources WHERE source_id=?", (sid,)
                ).fetchone()
                if not src_row:
                    raise StorageError(f"source_id not found: {sid}")
                if src_row["status"] == "deleted":
                    raise ConflictError(
                        f"cannot add extraction from deleted source: {sid}"
                    )

                if sid not in old_source_ids:
                    added_source_ids.append(sid)
                    old_source_ids.add(sid)

                # 每次 add_extraction 都写一行（audit history）
                ext_id = _new_uuid12("e_")
                conn.execute(
                    """INSERT INTO extractions
                    (extraction_id, source_id, concept_slug, quote_span,
                     char_start, char_end, extracted_at, extracted_by,
                     prompt_version, confidence, source_content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ext_id,
                        sid,
                        slug,
                        qs,
                        ed.get("char_start"),
                        ed.get("char_end"),
                        now,
                        extracted_by,
                        prompt_version,
                        ed.get("confidence"),
                        _fetch_source_content_hash(conn, sid),
                    ),
                )
                extraction_ids.append(ext_id)

            new_source_ids_str = json.dumps(sorted(old_source_ids))
            conn.execute(
                "UPDATE concepts SET source_ids=?, is_orphan=0, updated_at=? WHERE slug=?",
                (new_source_ids_str, now, slug),
            )

        if add_links:
            old_links.update(add_links)
            conn.execute(
                "UPDATE concepts SET links=?, updated_at=? WHERE slug=?",
                (json.dumps(sorted(old_links)), now, slug),
            )
            for to_slug in add_links:
                conn.execute(
                    "INSERT OR IGNORE INTO links (from_slug, to_slug) VALUES (?, ?)",
                    (slug, to_slug),
                )

    return {
        "slug": slug,
        "updated_at": now,
        "added_source_ids": added_source_ids,
        "extraction_ids": extraction_ids,
        "source_ids": sorted(old_source_ids),
    }


def add_source_to_concept(
    db_path: Path,
    slug: str,
    source_id: str,
    *,
    quote_span: str,
    prompt_version: str | None = None,
    extracted_by: str = "agent",
    confidence: float | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
) -> dict[str, Any]:
    """加一个 source 到 concept。自动写 extractions + 清 is_orphan。

    Returns: {"slug", "added": bool, "extraction_id", "source_ids": [...], "is_orphan": bool}
    """
    if not quote_span or not quote_span.strip():
        raise StorageError("quote_span is required")

    now = _utc_now_iso()
    ext_id = _new_uuid12("e_")
    with connect(db_path) as conn:
        src_row = conn.execute(
            "SELECT status FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if not src_row:
            raise StorageError(f"source_id not found: {source_id}")
        if src_row["status"] == "deleted":
            raise ConflictError(f"cannot add from deleted source: {source_id}")

        row = conn.execute(
            "SELECT source_ids FROM concepts WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            raise StorageError(f"concept not found: {slug}")

        old_ids = set(_parse_json_list(row["source_ids"]))
        added = source_id not in old_ids
        if added:
            old_ids.add(source_id)

        conn.execute(
            """INSERT INTO extractions
            (extraction_id, source_id, concept_slug, quote_span,
             char_start, char_end, extracted_at, extracted_by,
             prompt_version, confidence, source_content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ext_id, source_id, slug, quote_span,
                char_start, char_end, now, extracted_by,
                prompt_version, confidence,
                _fetch_source_content_hash(conn, source_id),
            ),
        )
        conn.execute(
            "UPDATE concepts SET source_ids=?, is_orphan=0, updated_at=? WHERE slug=?",
            (json.dumps(sorted(old_ids)), now, slug),
        )

    return {
        "slug": slug,
        "added": added,
        "extraction_id": ext_id,
        "source_ids": sorted(old_ids),
        "is_orphan": len(old_ids) == 0,
    }


def remove_source_from_concept(
    db_path: Path,
    slug: str,
    source_id: str,
) -> dict[str, Any]:
    """从 concept.source_ids 移除一个 source_id。

    extractions 行保留（audit trail），但 source_ids 集合减一。
    如果 source_ids 变空，自动 is_orphan=1。

    Returns: {"slug", "removed": bool, "source_ids": [...], "is_orphan": bool}
    """
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT source_ids FROM concepts WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            raise StorageError(f"concept not found: {slug}")

        old_ids = set(_parse_json_list(row["source_ids"]))
        was_in = source_id in old_ids
        if was_in:
            old_ids.discard(source_id)

        is_orphan = 1 if len(old_ids) == 0 else 0
        conn.execute(
            "UPDATE concepts SET source_ids=?, is_orphan=?, updated_at=? WHERE slug=?",
            (json.dumps(sorted(old_ids)), is_orphan, now, slug),
        )

    return {
        "slug": slug,
        "removed": was_in,
        "source_ids": sorted(old_ids),
        "is_orphan": is_orphan == 1,
    }


def get_concept_evidence(
    db_path: Path, slug: str, source_id: str,
) -> dict[str, Any] | None:
    """查 concept 在指定 source 里的所有抽取记录（按 extracted_at 倒序）。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM extractions
            WHERE concept_slug=? AND source_id=?
            ORDER BY extracted_at DESC""",
            (slug, source_id),
        ).fetchall()
    if not rows:
        return None
    return {
        "concept_slug": slug,
        "source_id": source_id,
        "extractions": [dict(r) for r in rows],
    }


def get_concept_evidence_summary(db_path: Path, slug: str) -> dict[str, Any]:
    """查 concept 的所有抽取 evidence 摘要（按 source 分组）。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT source_id, COUNT(*) AS n_extractions,
                     MAX(extracted_at) AS last_extracted_at,
                     GROUP_CONCAT(DISTINCT prompt_version) AS prompt_versions,
                     GROUP_CONCAT(DISTINCT extracted_by) AS extracted_bys
            FROM extractions WHERE concept_slug=?
            GROUP BY source_id""",
            (slug,),
        ).fetchall()
    return {"concept_slug": slug, "by_source": [dict(r) for r in rows]}


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
    out["source_ids"] = _parse_json_list(out["source_ids"])
    out["links"] = _parse_json_list(out["links"])
    out["incoming_links"] = [s for s in (out.get("incoming_links") or "").split(",") if s]
    if out["certified_issues"]:
        out["certified_issues"] = _parse_json_list(out["certified_issues"])
    if out["certified_suggestions"]:
        out["certified_suggestions"] = _parse_json_list(out["certified_suggestions"])
    out["is_orphan"] = bool(out["is_orphan"])
    return out


def list_concepts(
    db_path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    is_orphan: bool | None = None,
    is_certified: bool | None = None,
) -> list[dict[str, Any]]:
    """列 concept.

    is_orphan:    None=全部, True=仅孤儿, False=仅非孤儿
    is_certified: None=全部, True=仅已认证, False=仅未认证
    """
    where_clauses: list[str] = []
    params: list[Any] = []
    if is_orphan is not None:
        where_clauses.append("is_orphan=?")
        params.append(1 if is_orphan else 0)
    if is_certified is not None:
        if is_certified:
            where_clauses.append("certified_at IS NOT NULL")
        else:
            where_clauses.append("certified_at IS NULL")
    sql = "SELECT * FROM concepts"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = _parse_json_list(d["source_ids"])
        d["links"] = _parse_json_list(d["links"])
        d["is_orphan"] = bool(d["is_orphan"])
        out.append(d)
    return out


def list_uncertified_concepts(
    db_path: Path,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """列还没被认证的 concept。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM concepts WHERE certified_at IS NULL ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = _parse_json_list(d["source_ids"])
        d["links"] = _parse_json_list(d["links"])
        d["is_orphan"] = bool(d["is_orphan"])
        out.append(d)
    return out


def find_concept_by_link(db_path: Path, link_target: str) -> list[dict[str, Any]]:
    """解析 [[wikilink]] → candidate concept list。"""
    from .ids import slugify
    exact_slug = slugify(link_target)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM concepts WHERE slug=?", (exact_slug,)
        ).fetchone()
        if row:
            d = dict(row)
            d["source_ids"] = _parse_json_list(d["source_ids"])
            d["links"] = _parse_json_list(d["links"])
            d["is_orphan"] = bool(d["is_orphan"])
            return [d]
        like_pattern = f"%{exact_slug}%"
        rows = conn.execute(
            "SELECT * FROM concepts WHERE slug LIKE ? LIMIT 10",
            (like_pattern,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = _parse_json_list(d["source_ids"])
        d["links"] = _parse_json_list(d["links"])
        d["is_orphan"] = bool(d["is_orphan"])
        out.append(d)
    return out


# ============================================================================
# certification
# ============================================================================

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
                now, score, json.dumps(issues),
                json.dumps(suggestions), certified_by, now, slug,
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
        orphans = conn.execute(
            "SELECT COUNT(*) AS n FROM concepts WHERE is_orphan=1"
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
        stats_sources = conn.execute(
            "SELECT COUNT(*) AS n FROM sources"
        ).fetchone()["n"]
        stats_committed = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE status='committed'"
        ).fetchone()["n"]
    return {
        "total_concepts": total,
        "certified": certified,
        "uncertified": total - certified,
        "orphans": orphans,
        "avg_score": round(avg, 3) if avg is not None else None,
        "score_distribution": {
            "<0.5": buckets["lt50"] or 0,
            "0.5-0.7": buckets["lt70"] or 0,
            "0.7-0.9": buckets["lt90"] or 0,
            ">=0.9": buckets["ge90"] or 0,
        },
        "total_sources": stats_sources,
        "committed_sources": stats_committed,
    }


# ============================================================================
# search (stage 1: LIKE; stage 3: FTS5)
# ============================================================================

def search_concepts(db_path: Path, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM concepts WHERE title LIKE ? OR slug LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_ids"] = _parse_json_list(d["source_ids"])
        d["links"] = _parse_json_list(d["links"])
        d["is_orphan"] = bool(d["is_orphan"])
        out.append(d)
    return out


# ============================================================================
# index sync (导出 wiki/index/*.json)
# ============================================================================

def _validate_links(slug: str, links: list[str]) -> list[str]:
    """links 校验: 去重 + 拒绝自引用 + 拒绝非 slug-safe 字符串.

    返回 cleaned list (保持原顺序, 已去重).
    """
    from .ids import slugify
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in links:
        link = raw.strip() if isinstance(raw, str) else raw
        if not link:
            continue
        if link == slug:
            raise StorageError(
                f"link cannot be self-reference: {link!r}",
                hint="concept cannot wikilink to itself",
            )
        if slugify(link) != link:
            raise StorageError(
                f"link not slug-safe: {link!r}",
                hint="links must already be valid slugs (lowercase alphanumeric + hyphens)",
            )
        if link in seen:
            continue
        seen.add(link)
        cleaned.append(link)
    return cleaned


def delete_concept(db_path: Path, slug: str) -> dict[str, Any]:
    """硬删 concept + 清理 extractions / links. 不影响 source 表.

    设计: concept delete 是 user-level 决策 (agent 已经看到 orphan 警告再决定),
    所以用 hard delete; certification_log / extractions 已记历史, 不需保留.

    Returns: {"slug", "deleted_concept", "deleted_extractions_count",
             "deleted_links_count", "had_wiki_file": bool}
    """
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute("SELECT concept_id FROM concepts WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise StorageError(f"concept not found: {slug}")

        ext_cur = conn.execute("DELETE FROM extractions WHERE concept_slug=?", (slug,))
        link_cur = conn.execute("DELETE FROM links WHERE from_slug=?", (slug,))
        conc_cur = conn.execute("DELETE FROM concepts WHERE slug=?", (slug,))
        if conc_cur.rowcount == 0:
            # 应该不会到这 (前面 SELECT 已确认存在)
            raise StorageError(f"concept vanished during delete: {slug}")

    return {
        "slug": slug,
        "deleted_concept": True,
        "deleted_extractions_count": ext_cur.rowcount,
        "deleted_links_count": link_cur.rowcount,
        "deleted_at": now,
    }


def export_index(db_path: Path, wiki_index_dir: Path) -> dict[str, Any]:
    """导出 concepts.json + sources.json 到 wiki_index_dir。

    这是个一次性导出的视图，让 agent 可以直接 Read JSON 拿到 vault 全貌。
    """
    wiki_index_dir.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        concepts_rows = conn.execute(
            """SELECT slug, title, source_ids, links,
                      is_orphan, certified_score, certified_at, updated_at
            FROM concepts ORDER BY slug"""
        ).fetchall()
        sources_rows = conn.execute(
            """SELECT source_id, raw_path, original_filename, status,
                      size_bytes, created_at, committed_at, deleted_at
            FROM sources ORDER BY created_at DESC"""
        ).fetchall()

    concepts_data = []
    for r in concepts_rows:
        d = dict(r)
        d["source_ids"] = _parse_json_list(d["source_ids"])
        d["links"] = _parse_json_list(d["links"])
        d["is_orphan"] = bool(d["is_orphan"])
        concepts_data.append(d)

    sources_data = [dict(r) for r in sources_rows]

    concepts_path = wiki_index_dir / "concepts.json"
    sources_path = wiki_index_dir / "sources.json"
    concepts_path.write_text(
        json.dumps({"version": 1, "total": len(concepts_data), "concepts": concepts_data},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    sources_path.write_text(
        json.dumps({"version": 1, "total": len(sources_data), "sources": sources_data},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "concepts_json": str(concepts_path),
        "sources_json": str(sources_path),
        "concepts_count": len(concepts_data),
        "sources_count": len(sources_data),
    }
