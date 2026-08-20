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

from .atomic import atomic_write_text
from .errors import ConflictError, StorageError
from .frontmatter import (
    read_md_with_frontmatter as _read_md,
    write_md_with_frontmatter as _write_md,
)

SCHEMA_VERSION = 5

# Migration 步骤表: key=(from_v, to_v), value=函数(conn)
_MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): "_migrate_1_to_2",
    (2, 3): "_migrate_2_to_3",
    (3, 4): "_migrate_3_to_4",
    (4, 5): "_migrate_4_to_5",
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


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """v2 → v3: concepts 表加 version INTEGER NOT NULL DEFAULT 0 (optimistic concurrency control).

    SQLite 没有 ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT (没值), 标准做法是 rename + recreate.
    现有 concept 的 version 默认 0 (从未 update 过).
    """
    conn.executescript(
        """
        ALTER TABLE concepts RENAME TO concepts__v2;
        CREATE TABLE concepts (
            concept_id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            source_ids TEXT NOT NULL DEFAULT '[]',
            links TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_orphan INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            certified_at TEXT,
            certified_score REAL,
            certified_issues TEXT,
            certified_suggestions TEXT,
            certified_by TEXT
        );
        INSERT INTO concepts
            (concept_id, slug, title, body, source_ids, links,
             created_at, updated_at, is_orphan, version,
             certified_at, certified_score, certified_issues,
             certified_suggestions, certified_by)
            SELECT concept_id, slug, title, body, source_ids, links,
                   created_at, updated_at, is_orphan, 0,
                   certified_at, certified_score, certified_issues,
                   certified_suggestions, certified_by
            FROM concepts__v2;
        DROP TABLE concepts__v2;
        CREATE INDEX IF NOT EXISTS idx_concepts_slug ON concepts(slug);
        CREATE INDEX IF NOT EXISTS idx_concepts_certified_at ON concepts(certified_at);
        CREATE INDEX IF NOT EXISTS idx_concepts_is_orphan ON concepts(is_orphan);
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
    version INTEGER NOT NULL DEFAULT 0,      -- 乐观锁: 每次 update +1, CAS 用来 detect concurrent modification
    status TEXT NOT NULL DEFAULT 'draft',   -- concept 生命周期: draft / evergreen / stale (schema v5)
    aliases TEXT NOT NULL DEFAULT '[]',     -- JSON array, find-by-link 备用: ['MVCC', '多版本并发'] (schema v5)
    tags TEXT NOT NULL DEFAULT '[]',        -- JSON array, 概念分类: ['concept', 'database'] (schema v5)
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

CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL,
    source_id TEXT,
    source_path TEXT,
    actor TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    details TEXT,
    source_content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_log_started_at ON ingest_log(started_at);
CREATE INDEX IF NOT EXISTS idx_ingest_log_source_id ON ingest_log(source_id);
CREATE INDEX IF NOT EXISTS idx_ingest_log_op ON ingest_log(op);

"""




def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """v3 -> v4: 加 ingest_log 表 (audit log for source ops)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            op TEXT NOT NULL,
            source_id TEXT,
            source_path TEXT,
            actor TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            details TEXT,
            source_content_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ingest_log_started_at ON ingest_log(started_at);
        CREATE INDEX IF NOT EXISTS idx_ingest_log_source_id ON ingest_log(source_id);
        CREATE INDEX IF NOT EXISTS idx_ingest_log_op ON ingest_log(op);
        """
    )



def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """v4 -> v5: concepts 表加 status / aliases / tags 列.

    status:  概念生命周期 (draft / evergreen / stale).
    aliases: JSON array, find-by-link 备用 (e.g. 'MVCC' -> postgresql-mvcc).
    tags:    JSON array, 概念分类 (e.g. ['concept', 'database']).

    SQLite 没 ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT 0 (没值),
    标准 rename + recreate.
    """
    conn.executescript(
        """
        ALTER TABLE concepts RENAME TO concepts__v4;
        CREATE TABLE concepts (
            concept_id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            source_ids TEXT NOT NULL DEFAULT '[]',
            links TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_orphan INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            aliases TEXT NOT NULL DEFAULT '[]',
            tags TEXT NOT NULL DEFAULT '[]',
            certified_at TEXT,
            certified_score REAL,
            certified_issues TEXT,
            certified_suggestions TEXT,
            certified_by TEXT
        );
        INSERT INTO concepts
            (concept_id, slug, title, body, source_ids, links,
             created_at, updated_at, is_orphan, version, status, aliases, tags,
             certified_at, certified_score, certified_issues,
             certified_suggestions, certified_by)
            SELECT concept_id, slug, title, body, source_ids, links,
                   created_at, updated_at, is_orphan, version, 'draft', '[]', '[]',
                   certified_at, certified_score, certified_issues,
                   certified_suggestions, certified_by
            FROM concepts__v4;
        DROP TABLE concepts__v4;
        CREATE INDEX IF NOT EXISTS idx_concepts_slug ON concepts(slug);
        CREATE INDEX IF NOT EXISTS idx_concepts_certified_at ON concepts(certified_at);
        CREATE INDEX IF NOT EXISTS idx_concepts_is_orphan ON concepts(is_orphan);
        """
    )



def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_uuid12(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _hash(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()


@contextmanager
def connect(db_path: Path, *, busy_timeout_ms: int = 30000) -> Iterator[sqlite3.Connection]:
    """打开 SQLite 连接: WAL + autocommit + busy_timeout.

    并发模型 (无 flock 强制锁):
    - WAL: 一个写者 + 多读者, reader 不阻塞 writer
    - busy_timeout=30s: 多 writer 等 SQLite 文件锁排队 (POSIX advisory lock on WAL/SHM)
    - autocommit: 每语句独立事务, 无嵌套锁

    物理文件 IO (raw/ wiki/) 由 corpus.atomic.atomic_write_text 保护 (写 tmp + os.replace).
    """
    if not db_path.parent.exists():
        raise StorageError(f"meta directory missing: {db_path.parent}")
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # autocommit
        timeout=busy_timeout_ms / 1000.0,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# SQLite 错误码
_SQLITE_BUSY = 5        # database is locked
_SQLITE_LOCKED = 6      # table is locked


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    """判断是否是 SQLITE_BUSY / SQLITE_LOCKED."""
    msg = str(exc).lower()
    return "database is locked" in msg or "table is locked" in msg or "locked" in msg and "database" in msg


@contextmanager
def write_with_retry(db_path: Path, *, max_retries: int = 5, backoff_ms: int = 50):
    """带 retry 的写 context manager: SQLITE_BUSY 自动重试.

    多个 CLI 并发写同一 vault 时, SQLite 自动串行化 (writer 等锁).
    若等锁超时 (busy_timeout=30s), 抛 SQLITE_BUSY → 我们 retry max_retries 次, 每次 backoff.

    实际生产中: busy_timeout=30s 期间大概率拿到锁, retry 通常不触发.
    但 retry 给边缘 case (timeout 刚到 / 多个 writer) 兜底.
    """
    import sqlite3
    import time
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with connect(db_path) as conn:
                yield conn
            return  # 成功, 退出
        except sqlite3.OperationalError as e:
            if not _is_busy_error(e):
                raise
            last_exc = e
            if attempt < max_retries:
                time.sleep(backoff_ms / 1000.0 * (attempt + 1))  # 线性 backoff
                continue
            raise StorageError(
                f"vault locked after {max_retries + 1} attempts: {e}",
                hint="another corpus process is writing; retry in a moment",
            ) from e


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


def log_ingest(
    db_path: Path,
    *,
    op: str,
    source_id: str | None = None,
    source_path: str | None = None,
    actor: str = "agent",
    started_at: str | None = None,
    ended_at: str | None = None,
    status: str = "ok",
    details: dict[str, Any] | None = None,
    source_content_hash: str | None = None,
) -> int:
    """写一行 ingest_log. 返回新行 id.

    op: 'stage' | 'revive' | 'commit' | 'delete' | 'batch'
    status: 'ok' | 'failed' | 'skipped_duplicate' | 'skipped_locked'
    details: dict -> JSON 存 (reason / error / extras)
    """
    if started_at is None:
        started_at = _utc_now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO ingest_log
            (op, source_id, source_path, actor, started_at, ended_at, status, details, source_content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                op, source_id, source_path, actor,
                started_at, ended_at, status,
                json.dumps(details) if details else None,
                source_content_hash,
            ),
        )
    return cur.lastrowid or 0


def list_ingest_log(
    db_path: Path,
    *,
    op: str | None = None,
    source_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """查 ingest_log. 按 started_at DESC 排序.

    op: 过滤操作类型 (stage / commit / delete / revive / batch)
    source_id: 过滤 source
    since: 过滤 started_at >= since (ISO timestamp)
    limit: 最多返回条数
    """
    where_clauses: list[str] = []
    params: list[Any] = []
    if op:
        where_clauses.append("op = ?")
        params.append(op)
    if source_id:
        where_clauses.append("source_id = ?")
        params.append(source_id)
    if since:
        where_clauses.append("started_at >= ?")
        params.append(since)
    sql = "SELECT * FROM ingest_log"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("details"):
            try:
                d["details"] = json.loads(d["details"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


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


def _concept_path(vault_root: Path, slug: str) -> Path:
    """wiki/concept/<slug>.md 路径."""
    return vault_root / "wiki" / "concept" / f"{slug}.md"


def _source_path(vault_root: Path, source_id: str) -> Path:
    """wiki/source/<source_id>.md 路径."""
    return vault_root / "wiki" / "source" / f"{source_id}.md"


def _raw_path(vault_root: Path, source_id: str) -> Path:
    """raw/<file>-ingest-...md 路径 (按 source_id 在 raw/ 找, 不唯一)."""
    raw_dir = vault_root / "raw"
    if not raw_dir.exists():
        return None
    for p in raw_dir.iterdir():
        # raw 文件没 frontmatter 存 source_id, 用 DB 反查更准; 这里只能扫.
        # 调用方应传 vault_root + 用 DB 反查.
        if p.name == ".tmp" or p.name == ".gitkeep":
            continue
        # 用 read_source_file 看 frontmatter 是否有 source_id
        meta, _ = _read_md(p)
        if meta.get("source_id") == source_id:
            return p
    return None


def restore_from_files(vault_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """从 git 仓库 (raw/ + wiki/concept/ + wiki/source/) 重建整个 DB.

    适用场景: 换电脑 (git clone vault repo) / .wiki-meta/corpus.db 损坏 / 跨平台迁移.
    不会改 git tracked 的 markdown 文件, 只重写 .wiki-meta/corpus.db.

    流程:
      1. 读 wiki/concept/<slug>.md frontmatter -> INSERT/UPDATE concepts
      2. 读 wiki/concept/<slug>.md body 的 [[wikilinks]] -> INSERT links
      3. 读 raw/<file>-ingest-...md frontmatter -> INSERT/UPDATE sources
      4. 读 wiki/concept/<slug>.md frontmatter 的 sources: 数组 -> INSERT extractions

    dry_run=True 时只统计不写 DB.

    返回 {sources, concepts, links, extractions} 计数.
    """
    # 跨电脑恢复时 corpus.db 可能还没 init, 自动 init
    db_path = vault_root / ".wiki-meta" / "corpus.db"
    if not dry_run and not is_initialized(db_path):
        init_db(db_path)
    from .ids import slugify
    from datetime import datetime, timezone
    summary = {"sources": 0, "concepts": 0, "links": 0, "extractions": 0, "skipped": 0}

    # 1+2) concepts + links
    concept_dir = vault_root / "wiki" / "concept"
    if concept_dir.exists():
        for path in sorted(concept_dir.glob("*.md")):
            meta, body = _read_md(path)
            slug = meta.get("slug") or path.stem
            if not dry_run:
                with write_with_retry(vault_root / ".wiki-meta" / "corpus.db") as conn:
                    _upsert_concept_from_meta(conn, slug, meta, body)
                    # 收集 wikilinks from body
                    import re
                    for m in re.finditer(r"\[\[([^\]|\+]+?)\]\]", body):
                        link_slug = slugify(m.group(1).split("|")[0])
                        if link_slug != slug:
                            conn.execute(
                                "INSERT OR IGNORE INTO links (from_slug, to_slug) VALUES (?, ?)",
                                (slug, link_slug),
                            )
                            summary["links"] += 1
            summary["concepts"] += 1

    # 3) sources
    raw_dir = vault_root / "raw"
    if raw_dir.exists():
        for path in sorted(raw_dir.glob("*.md")):
            if path.name.startswith("."):
                continue  # .tmp/ .gitkeep
            meta, _body = _read_md(path)
            sid = meta.get("source_id")
            if not sid:
                continue  # 老 plain markdown 没 frontmatter, 跳过
            if not dry_run:
                with write_with_retry(vault_root / ".wiki-meta" / "corpus.db") as conn:
                    _upsert_source_from_meta(conn, sid, path, meta)
            summary["sources"] += 1

    # 4) extractions (从 wiki/concept/<slug>.md frontmatter 的 sources: 数组读)
    #    但我们 frontmatter 存 source_ids (sid list) 而非 quote_span, 所以 extractions 缺 quote_span.
    #    改进: sources: [{source_id, quote_span, confidence, prompt_version}] 嵌套对象
    if concept_dir.exists() and not dry_run:
        with write_with_retry(vault_root / ".wiki-meta" / "corpus.db") as conn:
            for path in sorted(concept_dir.glob("*.md")):
                meta, _body = _read_md(path)
                slug = meta.get("slug") or path.stem
                sources = meta.get("sources") or []
                for src_entry in sources:
                    if isinstance(src_entry, str):
                        # 旧格式: sources: [sid1, sid2, ...]
                        sid = src_entry
                        quote_span = None
                        confidence = None
                        prompt_version = None
                    elif isinstance(src_entry, dict):
                        # 新格式: sources: [{source_id, quote_span, confidence, prompt_version}]
                        sid = src_entry.get("source_id")
                        quote_span = src_entry.get("quote_span")
                        confidence = src_entry.get("confidence")
                        prompt_version = src_entry.get("prompt_version")
                    else:
                        continue
                    if not sid:
                        continue
                    # source_content_hash 查 DB
                    row = conn.execute(
                        "SELECT content_hash FROM sources WHERE source_id=?", (sid,),
                    ).fetchone()
                    src_hash = row["content_hash"] if row else None
                    now = _utc_now_iso()
                    conn.execute(
                        """INSERT INTO extractions
                        (extraction_id, source_id, concept_slug, quote_span, char_start, char_end,
                         extracted_at, extracted_by, prompt_version, confidence, source_content_hash)
                        VALUES (?, ?, ?, ?, NULL, NULL, ?, 'restore', ?, ?, ?)""",
                        (
                            _new_uuid12("e_"), sid, slug, quote_span,
                            now, prompt_version, confidence, src_hash,
                        ),
                    )
                    summary["extractions"] += 1

    return summary


def _upsert_concept_from_meta(conn, slug: str, meta: dict, body: str) -> None:
    """从 frontmatter meta 写 concepts 行 (INSERT 或 UPDATE)."""
    source_ids = list(meta.get("source_ids") or [])
    links = list(meta.get("links") or [])
    aliases = list(meta.get("aliases") or [])
    tags = list(meta.get("tags") or [])
    certified_issues = list(meta.get("certified_issues") or [])
    certified_suggestions = list(meta.get("certified_suggestions") or [])
    try:
        conn.execute(
            """INSERT INTO concepts
            (concept_id, slug, title, body, source_ids, links, created_at, updated_at,
             is_orphan, version, status, aliases, tags,
             certified_at, certified_score, certified_issues,
             certified_suggestions, certified_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _new_uuid12("c_"),
                slug,
                meta.get("title", slug),
                body,
                json.dumps(source_ids),
                json.dumps(links),
                meta.get("created_at") or _utc_now_iso(),
                meta.get("updated_at") or _utc_now_iso(),
                0 if source_ids else 1,  # is_orphan
                meta.get("version", 0),
                meta.get("status", "draft"),
                json.dumps(aliases) if aliases else "[]",
                json.dumps(tags) if tags else "[]",
                meta.get("certified_at"),
                meta.get("certified_score"),
                json.dumps(certified_issues) if certified_issues else None,
                json.dumps(certified_suggestions) if certified_suggestions else None,
                meta.get("certified_by"),
            ),
        )
    except sqlite3.IntegrityError:
        # slug 已存在 → UPDATE
        conn.execute(
            """UPDATE concepts
            SET title=?, body=?, source_ids=?, links=?, updated_at=?, is_orphan=?,
                version=?, status=?, aliases=?, tags=?,
                certified_at=?, certified_score=?,
                certified_issues=?, certified_suggestions=?, certified_by=?
            WHERE slug=?""",
            (
                meta.get("title", slug), body,
                json.dumps(source_ids), json.dumps(links),
                meta.get("updated_at") or _utc_now_iso(),
                0 if source_ids else 1,
                meta.get("version", 0),
                meta.get("status", "draft"),
                json.dumps(aliases) if aliases else "[]",
                json.dumps(tags) if tags else "[]",
                meta.get("certified_at"),
                meta.get("certified_score"),
                json.dumps(certified_issues) if certified_issues else None,
                json.dumps(certified_suggestions) if certified_suggestions else None,
                meta.get("certified_by"),
                slug,
            ),
        )


def _upsert_source_from_meta(conn, sid: str, raw_path: Path, meta: dict) -> None:
    """从 frontmatter meta 写 sources 行."""
    try:
        conn.execute(
            """INSERT INTO sources
            (source_id, raw_path, original_filename, size_bytes, content_hash, status,
             created_at, committed_at, deleted_at, deleted_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)""",
            (
                sid,
                str(raw_path),
                meta.get("original_filename", raw_path.name),
                meta.get("size_bytes", raw_path.stat().st_size),
                meta.get("content_hash", ""),
                meta.get("status", "staged"),
                meta.get("created_at") or _utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError:
        conn.execute(
            """UPDATE sources
            SET raw_path=?, original_filename=?, size_bytes=?, content_hash=?, status=?,
                created_at=?
            WHERE source_id=?""",
            (
                str(raw_path),
                meta.get("original_filename", raw_path.name),
                meta.get("size_bytes", raw_path.stat().st_size),
                meta.get("content_hash", ""),
                meta.get("status", "staged"),
                meta.get("created_at") or _utc_now_iso(),
                sid,
            ),
        )


def write_concept_file(
    vault_root: Path,
    *,
    slug: str,
    title: str,
    body: str,
    source_ids: list[str] | None = None,
    links: list[str] | None = None,
    certified_at: str | None = None,
    certified_score: float | None = None,
    certified_issues: list[str] | None = None,
    certified_suggestions: list[str] | None = None,
    version: int = 0,
    created_at: str | None = None,
    updated_at: str | None = None,
    aliases: list[str] | None = None,
    status: str = "draft",
    tags: list[str] | None = None,
) -> Path:
    """写 wiki/concept/<slug>.md (frontmatter + body), atomic.

    frontmatter 存所有 metadata (slug / title / version / source_ids / links /
    certified_* / created_at / updated_at / aliases / status / tags).
    body 是 markdown (LLM 写的 wiki 内容).

    返回写入的 path.
    """
    
    now = updated_at or _utc_now_iso()
    if created_at is None:
        created_at = now
    meta = {
        "slug": slug,
        "title": title,
        "type": "concept",
        "version": version,
        "status": status,
        "source_ids": list(source_ids or []),
        "links": list(links or []),
        "aliases": list(aliases or []),
        "tags": list(tags or []),
        "created_at": created_at,
        "updated_at": now,
    }
    if certified_at:
        meta["certified_at"] = certified_at
        if certified_score is not None:
            meta["certified_score"] = certified_score
        if certified_issues:
            meta["certified_issues"] = list(certified_issues)
        if certified_suggestions:
            meta["certified_suggestions"] = list(certified_suggestions)
    path = _concept_path(vault_root, slug)
    _write_md(path, meta=meta, body=body)
    return path


def read_concept_file(vault_root: Path, slug: str) -> dict[str, Any] | None:
    """读 wiki/concept/<slug>.md frontmatter. 不存在返 None."""
    path = _concept_path(vault_root, slug)
    if not path.exists():
        return None
    meta, body = _read_md(path)
    meta["_body"] = body
    meta["_path"] = str(path)
    return meta


def write_source_file(
    vault_root: Path,
    raw_path: Path,
    *,
    source_id: str,
    original_filename: str,
    content_hash: str,
    size_bytes: int,
    status: str = "staged",
    created_at: str | None = None,
    body: str = "",
) -> Path:
    """写 raw/<file> frontmatter (raw file 含 source metadata).

    caller 传 raw_path (从 pick_raw_target 算出来, 不要函数自己找).
    body 是原始 markdown (用户提供).

    替代 plain write_text, 让 raw/<file> 也是 source of truth.
    """
    now = created_at or _utc_now_iso()
    meta = {
        "source_id": source_id,
        "original_filename": original_filename,
        "content_hash": content_hash,
        "size_bytes": size_bytes,
        "status": status,
        "created_at": now,
    }
    _write_md(raw_path, meta=meta, body=body)
    return raw_path


def _build_concepts_extracted_section(
    vault_root: Path, source_id: str, body: str,
) -> str:
    """生成 '## Concepts extracted from this source' section, 反查 extractions 表."""
    db_path = vault_root / ".wiki-meta" / "corpus.db"
    if not db_path.exists():
        return "## Concepts extracted from this source\n\n_(no DB)_\n"
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT concept_slug, quote_span, prompt_version, extracted_at, confidence
            FROM extractions WHERE source_id=?
            ORDER BY extracted_at DESC""",
            (source_id,),
        ).fetchall()
    if not rows:
        return "## Concepts extracted from this source\n\n_(none yet)_\n"
    lines = ["## Concepts extracted from this source", ""]
    for r in rows:
        confidence_str = f" (confidence {r['confidence']:.2f})" if r["confidence"] is not None else ""
        prompt_str = f" [{r['prompt_version']}]" if r["prompt_version"] else ""
        quote = (r["quote_span"] or "").replace("\n", " ")[:80]
        if len(r["quote_span"] or "") > 80:
            quote += "..."
        lines.append(
            f"- [[{r['concept_slug']}]]{prompt_str}{confidence_str} \u2014 \"{quote}\" ({r['extracted_at']})"
        )
    return "\n".join(lines)


def write_source_wiki_page(
    vault_root: Path,
    source_id: str,
    *,
    original_filename: str | None = None,
    content_hash: str | None = None,
    size_bytes: int | None = None,
    status: str = "staged",
    created_at: str | None = None,
    body: str = "",
) -> Path:
    """写 wiki/source/<source_id>.md (per-source wiki 页).

    frontmatter: source_id / original_filename / content_hash / size_bytes / status / created_at.
    body: 原始 markdown (用户提供) + '## Concepts extracted from this source' section (DB 反查填充).

    替代 wiki/index/concepts.json 里 source 列表, 直接 git 跟踪.
    """
    pages_dir = vault_root / "wiki" / "source"
    pages_dir.mkdir(parents=True, exist_ok=True)
    path = pages_dir / f"{source_id}.md"
    now = created_at or _utc_now_iso()
    meta = {
        "source_id": source_id,
        "type": "source",
    }
    if original_filename:
        meta["original_filename"] = original_filename
    if content_hash:
        meta["content_hash"] = content_hash
    if size_bytes is not None:
        meta["size_bytes"] = size_bytes
    meta["status"] = status
    meta["created_at"] = now
    concepts_section = _build_concepts_extracted_section(vault_root, source_id, body)
    full_body = body.rstrip() + "\n\n" + concepts_section
    _write_md(path, meta=meta, body=full_body)
    return path


def update_source_page_concepts(vault_root: Path, source_id: str) -> None:
    """重写 wiki/source/<source_id>.md 的 '## Concepts extracted' section.

    从 DB 反查 extractions (source_id), 重新生成完整 file.
    调用场景: concepts add-source / remove-source / remove-extraction 改了 extractions 后.
    """
    path = vault_root / "wiki" / "source" / f"{source_id}.md"
    if not path.exists():
        return
    meta, body = _read_md(path)
    section_marker = "\n## Concepts extracted from this source"
    if section_marker in body:
        original_body = body.split(section_marker)[0].rstrip()
    else:
        original_body = body.rstrip()
    new_concepts_section = _build_concepts_extracted_section(
        vault_root, source_id, original_body,
    )
    _write_md(path, meta=meta, body=new_concepts_section)


def read_source_file(path: Path) -> dict[str, Any] | None:
    """读 raw/<file>-ingest-...md frontmatter."""
    if not path.exists():
        return None
    meta, body = _read_md(path)
    meta["_body"] = body
    meta["_path"] = str(path)
    return meta


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
    status: str = "draft",
    aliases: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """严格 INSERT concept. slug 已存在 → ConflictError (LLM 重新走 dedup + update_concept).

    语义边界:
    - write_concept = '创建新 concept' (insert-only)
    - update_concept = '修改已有 concept' (CAS via --expected-version)
    - 撞 slug 不静默 merge: 让 LLM 自己 read + merge, 业务决策不应 storage 层静默做

    并发: 整个事务包 BEGIN IMMEDIATE + write_with_retry (SQLITE_BUSY 自动 retry).
    多 agent 并发写同一 slug 的 race:
      - 第一个拿写锁 → SELECT 不存在 → INSERT 成功
      - 第二个 wait 后拿写锁 → INSERT 撞 UNIQUE → IntegrityError → ConflictError
      - 第二个 LLM 重新 find-by-link + read + merge + update_concept(--expected-version)
      - 合并逻辑由 LLM 决定 (它知道怎么 merge body / 保留哪些 source), 不由 storage 静默

    必传 extractions_data: 每个 source_id 对应一段 quote_span 证据.
    - 至少 1 个 extraction

    返回: {
        "concept_id", "slug", "source_ids", "extraction_ids"
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
    extraction_ids: list[str] = []

    with write_with_retry(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. 校验所有 source_id 存在且不是 deleted (在事务内, 防 source 状态 race)
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

            # 2. 查 slug 是否已存在 (upsert 决策)
            existing = conn.execute(
                "SELECT concept_id, source_ids, links FROM concepts WHERE slug=?",
                (slug,),
            ).fetchone()

            if existing is not None:
                # slug 已存在 → 抛 ConflictError, 让 LLM 走 dedup + update 路径
                # 业务决策 (merge 哪些内容 / 保留哪些 source) 不应 storage 静默做
                raise ConflictError(
                    f"concept slug already exists: {slug!r}",
                    hint="find-by-link 看候选, read_concept 读现有, LLM merge, "
                         "然后 update_concept <vault> <slug> --expected-version N (CAS) 提交",
                )
            # INSERT 路径
            cid = _new_uuid12("c_")
            current_version = 0  # 新 concept 从 version 0 开始 (UPDATE 路径才在 SELECT 拿)
            try:
                conn.execute(
                    """INSERT INTO concepts
                    (concept_id, slug, title, body, source_ids, links, created_at, updated_at,
                     is_orphan, status, aliases, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                    (
                        cid, slug, title, body,
                        json.dumps(source_ids), json.dumps(links),
                        now, now,
                        status, json.dumps(aliases or []), json.dumps(tags or []),
                    ),
                )
            except sqlite3.IntegrityError as e:
                # race: 另一 agent 在 SELECT (existing=None) 和 INSERT 之间也 INSERT 了同 slug
                raise ConflictError(
                    f"concept slug already exists: {slug!r} (concurrent insert race)",
                    hint="find-by-link + read_concept + LLM merge, "
                         "update_concept <vault> <slug> --expected-version N 提交",
                ) from e

            # 3. 写入 extractions (每次调用都写新行, audit history)
            for ed in extractions_data:
                ext_id = _new_uuid12("e_")
                conn.execute(
                    """INSERT INTO extractions
                    (extraction_id, source_id, concept_slug, quote_span,
                     char_start, char_end, extracted_at, extracted_by,
                     prompt_version, confidence, source_content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ext_id, ed["source_id"], slug, ed["quote_span"],
                        ed.get("char_start"), ed.get("char_end"),
                        now, extracted_by, prompt_version,
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

            # 5. 拿到最终 source_ids (合并后) 用于返回
            final_source_ids_row = conn.execute(
                "SELECT source_ids FROM concepts WHERE slug=?", (slug,)
            ).fetchone()
            final_source_ids = _parse_json_list(final_source_ids_row["source_ids"])

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "concept_id": cid,
        "slug": slug,
        "source_ids": final_source_ids,
        "extraction_ids": extraction_ids,
        "created_at": now,
        "version": current_version,  # INSERT 路径下 current_version=0
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
    expected_version: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """增量更新 concept (optimistic concurrency control via version).

    并发: 整个事务包 BEGIN IMMEDIATE + write_with_retry. UPDATE 时 version+1.
    多 agent 并发改同一 concept:
      - 第一个 commit 后 version+1
      - 第二个等锁拿到后 SELECT version, 不匹配 expected_version → OptimisticLockError
        → agent 重新 read_concept + merge + 再 update_concept (新 expected_version)

    add_extractions: 格式同 write_concept 的 extractions_data.
    自动去重 source_ids + 更新 extractions 表 + 自动清 is_orphan=0.

    expected_version (optional): CAS 标记. None=last-write-wins (快但可能丢数据),
      int=strict CAS (不匹配抛 OptimisticLockError).
    """
    from .errors import OptimisticLockError
    now = _utc_now_iso()
    with write_with_retry(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT concept_id, source_ids, links, version FROM concepts WHERE slug=?",
                (slug,),
            ).fetchone()
            if not row:
                raise StorageError(f"concept not found: {slug}")
            concept_id = row["concept_id"]
            current_version = row["version"]

            if expected_version is not None and current_version != expected_version:
                raise OptimisticLockError(
                    f"concept {slug!r} was modified concurrently "
                    f"(current_version={current_version}, expected={expected_version})",
                    hint="read_concept again, merge with current, then update_concept with new expected_version",
                )

            old_source_ids = set(_parse_json_list(row["source_ids"]))
            old_links = set(_parse_json_list(row["links"]))
            if add_links:
                add_links = _validate_links(slug, list(add_links))

            # 合并所有 SET (合并到一个 UPDATE 提高效率)
            set_parts: list[str] = []
            params: list[Any] = []
            if title is not None:
                set_parts.append("title=?")
                params.append(title)
            if body is not None:
                set_parts.append("body=?")
                params.append(body)
            if status is not None:
                set_parts.append("status=?")
                params.append(status)
            # 每次有改动 version+1 (CAS 自增)
            if set_parts or add_extractions or add_links:
                set_parts.append("version=version+1")
                set_parts.append("updated_at=?")
                params.append(now)
                params.append(slug)
                conn.execute(
                    f"UPDATE concepts SET {', '.join(set_parts)} WHERE slug=?",
                    params,
                )

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

                    ext_id = _new_uuid12("e_")
                    conn.execute(
                        """INSERT INTO extractions
                        (extraction_id, source_id, concept_slug, quote_span,
                         char_start, char_end, extracted_at, extracted_by,
                         prompt_version, confidence, source_content_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            ext_id, sid, slug, qs,
                            ed.get("char_start"), ed.get("char_end"),
                            now, extracted_by, prompt_version,
                            ed.get("confidence"),
                            _fetch_source_content_hash(conn, sid),
                        ),
                    )
                    extraction_ids.append(ext_id)

                # source_ids / is_orphan 也要 version+1 (如果有 add_extractions 改了 source_ids)
                conn.execute(
                    "UPDATE concepts SET source_ids=?, is_orphan=0, version=version+1, updated_at=? WHERE slug=?",
                    (json.dumps(sorted(old_source_ids)), now, slug),
                )

            if add_links:
                old_links.update(add_links)
                conn.execute(
                    "UPDATE concepts SET links=?, version=version+1, updated_at=? WHERE slug=?",
                    (json.dumps(sorted(old_links)), now, slug),
                )
                for to_slug in add_links:
                    conn.execute(
                        "INSERT OR IGNORE INTO links (from_slug, to_slug) VALUES (?, ?)",
                        (slug, to_slug),
                    )

            # 读最新 version (刚 UPDATE 多次, 拿 final)
            new_version_row = conn.execute(
                "SELECT version FROM concepts WHERE slug=?", (slug,),
            ).fetchone()
            new_version = new_version_row["version"] if new_version_row else current_version

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "slug": slug,
        "updated_at": now,
        "version": new_version,
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


def remove_extraction(db_path: Path, extraction_id: str) -> dict[str, Any]:
    """细粒度撤一次抽取: 删 extractions 行 + sync concept.source_ids.

    如果该 sid 在该 concept 上无其它 extraction 引用, 从 concept.source_ids 移除,
    并按需 is_orphan=1. 与 remove_source_from_concept (粗粒度) 互为补充.

    Returns: {
        "extraction_id", "deleted": True,
        "concept_slug", "source_id",
        "concept_source_ids_after": [...],
        "concept_is_orphan_after": bool,
    }
    """
    now = _utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT concept_slug, source_id FROM extractions WHERE extraction_id=?",
            (extraction_id,),
        ).fetchone()
        if not row:
            raise StorageError(f"extraction not found: {extraction_id}")
        concept_slug = row["concept_slug"]
        source_id = row["source_id"]

        cur = conn.execute(
            "DELETE FROM extractions WHERE extraction_id=?",
            (extraction_id,),
        )
        if cur.rowcount == 0:
            raise StorageError(f"extraction vanished: {extraction_id}")

        # 是否有其它 extraction 仍引用 (concept_slug, source_id)?
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE concept_slug=? AND source_id=?",
            (concept_slug, source_id),
        ).fetchone()["n"]

        crow = conn.execute(
            "SELECT source_ids FROM concepts WHERE slug=?",
            (concept_slug,),
        ).fetchone()
        if crow is None:
            # concept 不存在 (理论不应发生, extractions.concept_slug 是无 FK 软引用)
            new_source_ids: list[str] = []
            is_orphan_after = 0
        else:
            old_ids = set(_parse_json_list(crow["source_ids"]))
            if remaining == 0:
                old_ids.discard(source_id)
            new_source_ids = sorted(old_ids)
            is_orphan_after = 1 if not new_source_ids else 0
            conn.execute(
                "UPDATE concepts SET source_ids=?, is_orphan=?, updated_at=? WHERE slug=?",
                (json.dumps(new_source_ids), is_orphan_after, now, concept_slug),
            )

    return {
        "extraction_id": extraction_id,
        "deleted": True,
        "concept_slug": concept_slug,
        "source_id": source_id,
        "concept_source_ids_after": new_source_ids,
        "concept_is_orphan_after": bool(is_orphan_after),
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
    if out.get("status"):
        pass  # already string
    if out.get("aliases"):
        out["aliases"] = _parse_json_list(out["aliases"])
    if out.get("tags"):
        out["tags"] = _parse_json_list(out["tags"])
    return out


def list_concepts(
    db_path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    is_orphan: bool | None = None,
    is_certified: bool | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """列 concept.

    is_orphan:    None=全部, True=仅孤儿, False=仅非孤儿
    is_certified: None=全部, True=仅已认证, False=仅未认证
    status:      None=全部, 'draft' / 'evergreen' / 'stale' 过滤 (schema v5)
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
    if status is not None:
        where_clauses.append("status=?")
        params.append(status)
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
        if d.get("aliases"):
            d["aliases"] = _parse_json_list(d["aliases"])
        if d.get("tags"):
            d["tags"] = _parse_json_list(d["tags"])
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
    """解析 [[wikilink]] → candidate concept list, 按 match_score 倒序 + slug 长度正序.

    评分 = discrete 几档 + difflib continuous bonus:
      1.0  exact slug match
      0.9  slug startswith target
      0.5  slug contains target (substring)
      0.4  title contains target (case-insensitive)
      + difflib.SequenceMatcher.ratio() * 0.3 (capped at 1.0)

    score == 0 的完全不相关过滤掉; 放宽 LIMIT 50.
    跨 slug 拼写错误 ("postgers" -> "postgres") 由 difflib fuzzy 抓.

    corpus 设计接受小规模 (< 10k concepts), 全表扫描 OK.

    注: 'dedup-candidates' CLI 命令返多维度分数 (discrete / fuzzy / length_diff),
    让 LLM 自己用 LLM 二次判断 '这两个 concept 真的是同一个吗' (string 相似不够).
    """
    candidates = dedup_candidate_scores(db_path, link_target, limit=50)
    if not candidates:
        return []
    # 拿全部 concept detail (复用现有 SELECT *)
    target_slugs = {c["slug"] for c in candidates}
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM concepts WHERE slug IN ({})".format(
                ",".join("?" * len(target_slugs))
            ),
            list(target_slugs),
        ).fetchall()
    full_by_slug = {r["slug"]: dict(r) for r in rows}
    out = []
    for c in candidates:
        d = full_by_slug.get(c["slug"])
        if d is None:
            continue
        d["source_ids"] = _parse_json_list(d["source_ids"])
        d["links"] = _parse_json_list(d["links"])
        d["is_orphan"] = bool(d["is_orphan"])
        d["match_score"] = c["match_score"]
        out.append(d)
    return out


def dedup_candidate_scores(
    db_path: Path,
    link_target: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """返 dedup 候选 + 多维度分数, 让 LLM 二次判断.

    每个 candidate 含:
      - slug / title / source_count (基本信息)
      - discrete_score: 0-1 离散几档 (exact / startswith / contains / title_contains)
      - fuzzy_score: difflib.SequenceMatcher.ratio() * 0.3 (连续相似度)
      - length_diff: abs(len(slug) - len(target_slug)) (同 score 时短 slug 优先)
      - match_score: min(1.0, discrete + fuzzy) (综合)

    LLM 拿到这 view 后, 用自己的 LLM 能力判断 '这两个 concept 真的是同一个吗'
    (string 相似不够, 比如 'postgres-mvcc' 和 'postgresql-mv' 字符相似但语义不同).
    """
    import difflib
    from .ids import slugify
    target_slug = slugify(link_target)
    target_lower = link_target.lower().strip()
    with connect(db_path) as conn:
        rows = conn.execute("SELECT slug, title, source_ids, aliases FROM concepts").fetchall()
    candidates: list[dict[str, Any]] = []
    for r in rows:
        slug = r["slug"]
        title = r["title"] or ""
        source_ids = _parse_json_list(r["source_ids"])
        aliases = _parse_json_list(r["aliases"] or "[]")
        # aliases 匹配 (schema v5: 'MVCC' -> postgresql-mvcc)
        alias_match = False
        for alias in aliases:
            alias_slug = slugify(alias)
            if alias_slug == target_slug:
                alias_match = "exact"
                break
            if target_slug and (alias_slug.startswith(target_slug) or target_slug in alias_slug):
                alias_match = "partial"
                break
            if target_lower and target_lower in alias.lower():
                alias_match = "partial"
                break
        if slug == target_slug:
            discrete = 1.0
        elif alias_match == "exact":
            discrete = 0.95  # alias exact match, 仅次于 slug exact
        elif alias_match == "partial":
            discrete = 0.6  # alias 部分匹配
        elif slug.startswith(target_slug):
            discrete = 0.9
        elif target_slug and target_slug in slug:
            discrete = 0.5
        elif target_lower and target_lower in title.lower():
            discrete = 0.4
        else:
            discrete = 0.0
        # continuous bonus: difflib SequenceMatcher (Levenshtein-like)
        # 拼写错误 ("postgers" -> "postgres") 由 fuzzy 抓, 不靠 discrete
        fuzzy = difflib.SequenceMatcher(None, target_slug, slug).ratio() * 0.3
        match_score = min(1.0, discrete + fuzzy)
        if match_score < 0.1:
            # 太弱 (discrete=0 + fuzzy<0.33) 过滤掉, 避免噪音
            continue
        candidates.append({
            "slug": slug,
            "title": title,
            "source_count": len(source_ids),
            "discrete_score": round(discrete, 3),
            "fuzzy_score": round(fuzzy, 3),
            "length_diff": abs(len(slug) - len(target_slug)),
            "match_score": round(match_score, 3),
        })
    candidates.sort(key=lambda c: (-c["match_score"], c["length_diff"]))
    return candidates[:limit]


# ============================================================================
# certification
# ============================================================================

def mark_certified(
    db_path: Path,
    *,
    slug: str,
    score: float | None = None,
    issues: list[str] | None = None,
    suggestions: list[str] | None = None,
    certified_by: str = "agent",
) -> dict[str, Any]:
    """标记 / 部分更新 concept 认证. 同时写 certification_log.

    各字段语义:
      - score=None       → 保留旧 score (首次认证必传)
      - issues=None      → 保留旧 issues
      - suggestions=None → 保留旧 suggestions
      - 传 list (含 [])  → 覆盖

    至少要传一个非 None 字段, 否则报 StorageError (无 update 内容).
    """
    if score is None and issues is None and suggestions is None:
        raise StorageError(
            "no fields to update",
            hint="pass at least one of --score / --issues / --suggestions",
        )
    if score is not None and not 0.0 <= score <= 1.0:
        raise StorageError(f"score must be in [0, 1], got {score}")

    # 用 microsecond 精度: certification_log PK 是 (concept_id, certified_at),
    # 同秒内多次 partial update 会撞 PK; microsecond 精度天然避开.
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT concept_id, certified_score, certified_issues,
                      certified_suggestions, certified_at
            FROM concepts WHERE slug=?""",
            (slug,),
        ).fetchone()
        if not row:
            raise StorageError(f"concept not found: {slug}")

        old_score = row["certified_score"]
        old_issues = _parse_json_list(row["certified_issues"])
        old_suggestions = _parse_json_list(row["certified_suggestions"])
        prev_certified_at = row["certified_at"]

        if score is not None:
            new_score = score
        else:
            if old_score is None:
                raise StorageError(
                    "score is required for first-time certification",
                    hint="concepts.uncertified list + first certify must include --score",
                )
            new_score = old_score

        new_issues = issues if issues is not None else old_issues
        new_suggestions = suggestions if suggestions is not None else old_suggestions

        conn.execute(
            """UPDATE concepts SET
            certified_at=?, certified_score=?, certified_issues=?,
            certified_suggestions=?, certified_by=?, updated_at=?
            WHERE slug=?""",
            (
                now, new_score, json.dumps(new_issues),
                json.dumps(new_suggestions), certified_by, now, slug,
            ),
        )
        conn.execute(
            """INSERT INTO certification_log
            (concept_id, certified_at, score, issues, suggestions, certified_by)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (row["concept_id"], now, new_score, json.dumps(new_issues),
             json.dumps(new_suggestions), certified_by),
        )

    return {
        "slug": slug,
        "score": new_score,
        "issues": new_issues,
        "suggestions": new_suggestions,
        "certified_at": now,
        "prev_certified_at": prev_certified_at,
        "partial_update": score is None or issues is None or suggestions is None,
    }


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
            """SELECT slug, concept_id, title, source_ids, links,
                      is_orphan, version, certified_score, certified_at,
                      created_at, updated_at
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
