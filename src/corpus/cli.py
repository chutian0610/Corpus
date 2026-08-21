"""corpus CLI。

设计原则：
- LLM-decoupled: 任何子命令都不调 LLM
- LLM 调用调用由 agent 端负责（agent 自己用 OpenAI/Anthropic SDK）
- 纯数据操作：落源、写 concept、搜索、认证
- 输出 JSON 友好：所有命令支持 --json 输出供 agent 解析
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import __version__
from .errors import ConflictError, CorpusBotError, StorageError, ValidationError
from .ids import source_id_from_content
from .storage import (
    SCHEMA_VERSION,
    add_source_to_concept,
    dedup_candidate_scores,
    list_ingest_log,
    read_concept,
    write_concept_file,
    write_source_wiki_page,
    update_source_page_concepts,
    restore_from_files,
    write_source_file,
    certification_stats,
    commit_source,
    delete_concept,
    dry_run_delete_source,
    remove_extraction,
    export_index,
    find_concept_by_link,
    get_concept_evidence,
    init_db,
    is_initialized,
    list_concepts,
    list_sources,
    list_uncertified_concepts,
    mark_certified,
    read_concept,
    read_source,
    remove_source_from_concept,
    search_concepts,
    soft_delete_source,
    hard_delete_source,
    stage_source,
    unmark_certified,
    update_concept,
    write_concept,
)
from .vault import (
    _ensure_git_repo,
    assert_source_outside_vault,
    ensure_vault,
    pick_raw_target,
    validate_source_path,
    validate_source_path_basic,
    vault_paths,
)
from .atomic import atomic_write_text
from .lock import vault_file_lock  # utility (CLI 默认不用)




def _sync_concept_file(paths, slug: str) -> None:
    """从 DB 读最新 concept fields, 重写 wiki/concept/<slug>.md frontmatter.

    同步点: 任何写 DB 的命令 (write/update/add-source/remove-source/remove-extraction/
    certify) 调一下, 保持 markdown 文件 = DB view. recovery 时从 markdown 还原 DB.

    副作用: 也更新 concept 引用的 source wiki pages (双向同步).
    """
    info = read_concept(paths["corpus_db"], slug)
    if info is None:
        return
    write_concept_file(
        paths["root"],
        slug=slug,
        title=info["title"],
        body=info["body"],
        source_ids=info["source_ids"],
        links=info["links"],
        version=info.get("version", 0),
        created_at=info.get("created_at"),
        updated_at=info.get("updated_at"),
        certified_at=info.get("certified_at"),
        certified_score=info.get("certified_score"),
        certified_issues=info.get("certified_issues"),
        certified_suggestions=info.get("certified_suggestions"),
        aliases=info.get("aliases"),
        status=info.get("status") or "draft",
        tags=info.get("tags"),
    )
    # 双向同步: 更新 concept 引用的每个 source page
    for sid in info["source_ids"]:
        update_source_page_concepts(paths["root"], sid)



# ---------- helpers ----------

def _err(msg: str, *, hint: str | None = None, code: int = 1) -> None:
    if hint:
        click.echo(f"error: {msg}\n  hint: {hint}", err=True)
    else:
        click.echo(f"error: {msg}", err=True)
    sys.exit(code)


def _emit(data, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        click.echo(_humanize(data))


def _humanize(data) -> str:
    """简短的文本格式（默认输出，便于人眼扫）。"""
    if isinstance(data, list):
        if not data:
            return "(empty)"
        lines = []
        for item in data:
            lines.append(_humanize_one(item))
        return "\n".join(lines)
    return _humanize_one(data)


def _humanize_one(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    if "slug" in item:
        # concept
        cert = (
            f"  [cert {item.get('certified_score', '-')}]"
            if item.get("certified_at") else "  [uncertified]"
        )
        return f"  {item['slug']:<40} {item.get('title', '')}{cert}"
    if "source_id" in item:
        return (
            f"  {item['source_id']:<18} {item.get('status', '?'):<10} "
            f"{item.get('raw_path', '')}"
        )
    return json.dumps(item, ensure_ascii=False)


def _resolve_db(vault_root: Path, *, ensure_vault_dir: bool = True):
    """解析 vault 路径，返回 db_path (不带锁, 适用于 read-only 命令)."""
    if not vault_root.exists():
        _err(f"vault does not exist: {vault_root}", hint="run `corpus vault init <path>` first")
    if ensure_vault_dir:
        ensure_vault(vault_root)
    paths = vault_paths(vault_root)
    if not is_initialized(paths["corpus_db"]):
        init_db(paths["corpus_db"])
    return paths


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _ingest_one_source(
    paths: dict,
    source_file: Path,
    *,
    force_revive: bool = False,
) -> dict:
    """ingest 一个 source 到 vault (validate → stage DB → write raw frontmatter → write source page).

    Bug 2 修复: 写文件失败时回滚 DB (delete_source) — 之前 sources_ingest 失败
    raw write 但 DB 留 source_id 造成状态不一致.

    Returns: stage_source result dict (含 source_id / raw_path / size_bytes / content_hash / revived).
    Raises: CorpusBotError (ValidationError / ConflictError / StorageError) on 校验/写失败.
      - sources_ingest: 直接 propagate (click exit 1)
      - sources_batch:  catch + 累积 failed results
    """
    # 1. validate (Rule 1-5)
    canonical = validate_source_path_basic(source_file, paths["root"])

    # 2. 不允许 ingest vault 内的任何文件
    assert_source_outside_vault(canonical, paths["root"], paths["raw"])

    # 3. read content + 算 raw_path
    content = _read_file(canonical)
    raw_path = pick_raw_target(paths["raw"], content, canonical.name)

    # 4. stage DB
    result = stage_source(
        paths["corpus_db"],
        raw_path=raw_path,
        content=content,
        original_filename=canonical.name,
        revive_on_deleted=force_revive,
    )

    # 5. slug for obsidian 兼容 wiki 文件名
    from .ids import slugify
    slug = slugify(canonical.stem or "source")

    # 6. 写 raw/<file> frontmatter (Bug 2: 失败回滚 DB)
    try:
        write_source_file(
            paths["root"],
            raw_path,
            source_id=result["source_id"],
            original_filename=canonical.name,
            content_hash=result["content_hash"],
            size_bytes=result["size_bytes"],
            status="staged",
            body=content,
            slug=slug,
        )
    except OSError as e:
        from .storage import hard_delete_source as delete_source
        try:
            delete_source(paths["corpus_db"], result["source_id"])
        except Exception:
            pass
        raise StorageError(
            f"failed to write raw file {raw_path}: {e}",
            hint="DB rolled back via delete_source; 重新 ingest 重试",
        ) from e

    # 7. 写 wiki/source/<slug>.md (Bug 2: 失败回滚 DB)
    try:
        write_source_wiki_page(
            paths["root"],
            source_id=result["source_id"],
            slug=slug,
            original_filename=canonical.name,
            content_hash=result["content_hash"],
            size_bytes=result["size_bytes"],
            status="staged",
            body=content,
        )
    except OSError as e:
        from .storage import hard_delete_source as delete_source
        try:
            delete_source(paths["corpus_db"], result["source_id"])
        except Exception:
            pass
        raise StorageError(
            f'failed to write source page for source_id={result["source_id"]}: {e}',
            hint="DB rolled back; raw/<file> 保留但未与 DB 关联 (可手动 sources delete 或重新 ingest)",
        ) from e

    return result


# ---------- top-level group ----------

@click.group()
@click.version_option(version=__version__, prog_name="corpus")
def cli() -> None:
    """corpus: LLM-driven wiki builder (CLI-first, LLM-decoupled).

    \b
    Quick start:
      corpus vault init ~/my-wiki
      corpus sources ingest ~/my-wiki ~/notes/postgresql.md
      corpus concepts write ~/my-wiki \\
        --slug postgres-mvcc --title "PostgreSQL MVCC" \\
        --body "..." --source-ids <sid> --links postgres
      corpus stats ~/my-wiki

    LLM 调用（extract / compile / 评分）由 agent 自己做，corpus 不装 LLM。
    """


# ---------- vault ----------

@cli.group()
def vault() -> None:
    """vault 生命周期：init / info / stats。"""


@vault.command(name="init")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出")
def vault_init(
    vault_path: Path, as_json: bool,
) -> None:
    """初始化 vault 目录结构 (创建 vault root + raw/ + wiki/ + .wiki-meta/ + corpus.db).

    vault root 不存在时会自动创建 (mkdir -p 语义). 已初始化的 vault 跑 init 是幂等的.

    强制行为 (无法关闭):
      1. git init --initial-branch=main (vault 独立 git 仓库)
      2. 写 vault 根 .gitignore 排除 *.db / *.db-* / .wiki-meta/
      3. raw/ / wiki/concept/ / wiki/index/ 加 .gitkeep 占位
      4. git config user.email=corpus@localhost user.name=corpus (local)
      5. initial commit 'chore: init corpus vault'

    git 不在 PATH 时: skip git init (报 reason 'git not in PATH'), vault 仍可用.
    """
    vault_path.mkdir(parents=True, exist_ok=True)
    paths = ensure_vault(vault_path)
    if not is_initialized(paths["corpus_db"]):
        init_db(paths["corpus_db"])

    git_info = _ensure_git_repo(vault_path)

    _emit(
        {
            "vault": str(vault_path),
            "raw": str(paths["raw"]),
            "wiki": str(paths["wiki"]),
            "wiki_concept": str(paths["wiki_concept"]),
            "wiki_index": str(paths["wiki_index"]),
            "meta": str(paths["meta"]),
            "corpus_db": str(paths["corpus_db"]),
            "schema_version": SCHEMA_VERSION,
            "git": git_info,
        },
        as_json=as_json,
    )


@vault.command(name="info")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def vault_info(vault_path: Path, as_json: bool) -> None:
    """vault 元信息 + 路径表。"""
    paths = _resolve_db(vault_path)
    db_exists = is_initialized(paths["corpus_db"])
    sources = list_sources(paths["corpus_db"], limit=1) if db_exists else []
    concepts = list_concepts(paths["corpus_db"], limit=1) if db_exists else []
    _emit(
        {
            "vault": str(vault_path),
            "paths": {k: str(v) for k, v in paths.items()},
            "db_initialized": db_exists,
            "has_sources": bool(sources),
            "has_concepts": bool(concepts),
        },
        as_json=as_json,
    )


@vault.command(name="stats")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def vault_stats(vault_path: Path, as_json: bool) -> None:
    """统计信息：source 数 / concept 数 / 认证覆盖率 / score 分布。"""
    paths = _resolve_db(vault_path)
    stats = certification_stats(paths["corpus_db"])
    with open(paths["corpus_db"], "rb") as f:
        from .storage import connect
        pass
    from .storage import connect
    with connect(paths["corpus_db"]) as conn:
        stats["total_sources"] = conn.execute(
            "SELECT COUNT(*) AS n FROM sources"
        ).fetchone()["n"]
        stats["committed_sources"] = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE status='committed'"
        ).fetchone()["n"]
    _emit(stats, as_json=as_json)


# ---------- sources ----------

@cli.group()
def sources() -> None:
    """源文件管理：ingest / batch / list / show / commit / delete。"""


@sources.command(name="ingest")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_file", type=click.Path(exists=False, path_type=Path))
@click.option("--force-revive", is_flag=True,
              help="同 hash 但已 soft-deleted -> 复活该 source (status=staged), 而非报 ConflictError.")
@click.option("--json", "as_json", is_flag=True)
def sources_ingest(vault_path: Path, source_file: Path, force_revive: bool, as_json: bool) -> None:
    """单文件入库: content-hash dedup + 可选复活.

    源文件必须放在 **vault 外** (vault 内任何位置都报错):
      - 在 vault 外 -> 自动 cp 到 <vault>/raw/<stem>-ingest-<UTC><ext> + stage
      - 在 vault/raw/ 内 -> 报 'already in vault raw/' (避免重复 ingest)
      - 在 vault 其它目录 (wiki/ .wiki-meta/) -> 报 'forbidden internal directory'

    ingest 的语义是 '把 vault 外的内容拉进来', 不是 '重新入库 vault 里的文件'.
    如要重新入库同一文件: 先 sources delete <sid> 软删, 再 ingest (默认会因 content_hash 撞;
    软删后再 ingest 需 --force-revive).

    同 hash dedup:
      active (staged/committed) -> ConflictError
      deleted + --force-revive -> 复用 source_id, status='staged', 刷新 raw_path/content_hash
      deleted 不带 flag -> ConflictError, hint 提示 --force-revive
    """
    paths = _resolve_db(vault_path)

    # Rule 1-5 基础校验 (symlink / 扩展名 / 大小 / 存在性)
    try:
        canonical = validate_source_path_basic(source_file, paths["root"])
    except Exception as e:
        _err(str(e))

    # 新增: 不允许 ingest vault 内的任何文件
    try:
        assert_source_outside_vault(canonical, paths["root"], paths["raw"])
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))

    try:
        result = _ingest_one_source(paths, source_file, force_revive=force_revive)
    except CorpusBotError as e:
        _err(str(e), hint=getattr(e, "hint", None))
        return
    action = "revived" if result.get("revived") else "staged"
    _emit(
        {
            "action": action,
            "source_id": result["source_id"],
            "raw_path": str(result["raw_path"]),
            "size_bytes": result["size_bytes"],
            "content_hash": result["content_hash"],
        },
        as_json=as_json,
    )


@sources.command(name="batch")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--glob", "glob_pattern", default="*.md", help="glob 模式（默认 *.md）")
@click.option("--recursive/--no-recursive", default=True, help="是否递归子目录")
@click.option("--force-revive", is_flag=True,
              help="同 hash 但已 deleted -> 复活而非报错.")
@click.option("--json", "as_json", is_flag=True)
def sources_batch(
    vault_path: Path, source_dir: Path, glob_pattern: str, recursive: bool,
    force_revive: bool, as_json: bool,
) -> None:
    """批量入库: glob 匹配 + 逐个 stage, 撞名自动改名.

    返回每文件的处理结果 (staged / duplicate / revived / failed).
    --force-revive: 同 hash 已 deleted -> 复活该 source.
    """
    from .errors import ValidationError
    paths = _resolve_db(vault_path)

    matches = list(source_dir.rglob(glob_pattern) if recursive else source_dir.glob(glob_pattern))
    if not matches:
        _emit({"total": 0, "staged": 0, "revived": 0, "duplicates": 0, "failed": 0, "results": []}, as_json=as_json)
        return

    results = []
    n_staged = n_dup = n_revived = n_fail = 0
    for src in matches:
        # 撞名检测: 跟 _ingest_one_source 内部 pick_raw_target 一致
        # 但 batch 要先 read content 算 sid (dedup 检查)
        try:
            content = _read_file(src)
        except Exception as e:
            results.append({"source": str(src), "action": "failed", "message": str(e)})
            n_fail += 1
            continue
        sid = source_id_from_content(content)
        # dedup: 同 sid 已 active -> 跳过 (避免调 _ingest_one_source 内 stage_source 撞 UNIQUE)
        existing = read_source(paths["corpus_db"], sid)
        if existing and existing["status"] != "deleted":
            results.append({"source": str(src), "action": "duplicate", "existing_id": existing["source_id"]})
            n_dup += 1
            continue
        # deleted -> 让 _ingest_one_source 决定 (revive 或报错)
        try:
            result = _ingest_one_source(paths, src, force_revive=force_revive)
            if result.get("revived"):
                results.append({"source": str(src), "action": "revived", "source_id": result["source_id"]})
                n_revived += 1
            else:
                results.append({"source": str(src), "action": "staged", "source_id": result["source_id"]})
                n_staged += 1
        except ValidationError as e:
            results.append({"source": str(src), "action": "failed", "rule": e.rule, "message": e.message})
            n_fail += 1
        except CorpusBotError as e:
            # _ingest_one_source 已 _err 退出 (不返), 但 batch 用 results.append 累积
            # 实际上 _err 调 sys.exit, 不会返这里
            results.append({"source": str(src), "action": "failed", "message": str(e), "hint": getattr(e, "hint", None)})
            n_fail += 1

    _emit(
        {
            "total": len(matches),
            "staged": n_staged,
            "revived": n_revived,
            "duplicates": n_dup,
            "failed": n_fail,
            "results": results,
        },
        as_json=as_json,
    )


@cli.command(name="restore-from-files")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, help="只统计, 不写 DB (preview)")
@click.option("--json", "as_json", is_flag=True)
def cli_restore_from_files(
    vault_path: Path, dry_run: bool, as_json: bool,
) -> None:
    """从 raw/ + wiki/concept/ + wiki/source/ 重建整个 DB (sources/concepts/links/extractions).

    适用: 换电脑 (git clone vault repo) / .wiki-meta/corpus.db 损坏 / 跨平台迁移.
    流程:
      1. 读 wiki/concept/<slug>.md frontmatter -> INSERT/UPDATE concepts
      2. 读 wiki/concept/<slug>.md body 的 [[wikilinks]] -> INSERT links
      3. 读 raw/<file>-ingest-...md frontmatter (source_id/content_hash) -> INSERT/UPDATE sources
      4. 读 wiki/concept/<slug>.md frontmatter 的 sources: 数组 -> INSERT extractions

    不会改 git tracked 的 markdown 文件, 只重写 .wiki-meta/corpus.db.
    """
    paths = _resolve_db(vault_path)
    summary = restore_from_files(paths["root"], dry_run=dry_run)
    if as_json:
        _emit({"dry_run": dry_run, **summary}, as_json=True)
        return
    prefix = "[DRY-RUN] " if dry_run else ""
    click.echo(f"{prefix}restore-from-files 统计:")
    for k, v in summary.items():
        click.echo(f"  {k}: {v}")
    if dry_run:
        click.echo("")
        click.echo("去掉 --dry-run 实际跑一次.")

@cli.command(name="audit")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--op", "op_filter", default=None,
              help="过滤操作类型 (stage / commit / delete / revive / batch)")
@click.option("--source-id", default=None, help="只看这个 source 的 audit")
@click.option("--since", default=None, help="只看这个 ISO timestamp 之后的")
@click.option("--limit", "-n", type=int, default=50, help="最多返回条数 (默认 50)")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出")
def cli_audit(
    vault_path: Path, op_filter: str | None, source_id: str | None,
    since: str | None, limit: int, as_json: bool,
) -> None:
    """查 vault 操作审计日志 (ingest_log 表). 按时间倒序.

    ingest_log 记录每次 source 操作的 metadata:
      - op: stage / commit / delete / revive / batch
      - source_id, source_path, source_content_hash
      - actor (cli / agent), started_at, ended_at
      - status (ok / failed / skipped_duplicate / skipped_locked)
      - details (JSON: reason / error / extras)
    """
    paths = _resolve_db(vault_path)
    entries = list_ingest_log(
        paths["corpus_db"],
        op=op_filter,
        source_id=source_id,
        since=since,
        limit=limit,
    )
    if as_json:
        click.echo(json.dumps(entries, indent=2, ensure_ascii=False))
        return
    # humanize
    if not entries:
        click.echo("(no audit log entries)")
        return
    click.echo(f"audit log ({len(entries)} entries, latest first):")
    for e in entries:
        sid = e.get("source_id") or "-"
        path = e.get("source_path") or ""
        path_short = path.split("/")[-1] if path else ""
        status = e.get("status", "?")
        op = e.get("op", "?")
        actor = e.get("actor", "?")
        started = e.get("started_at", "?")
        details = e.get("details") or {}
        detail_str = ""
        if details:
            keys = list(details.keys())[:3]
            detail_str = " " + " ".join(f"{k}={details[k]}" for k in keys)
        click.echo(f"  {started}  {op:6s}  {status:20s}  {sid[:12]:12s}  {path_short}{detail_str}")


@sources.command(name="list")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--status", type=click.Choice(["staged", "committed", "deleted", "all"]), default="all")
@click.option("--limit", type=int, default=50)
@click.option("--offset", type=int, default=0)
@click.option("--json", "as_json", is_flag=True)
def sources_list(vault_path: Path, status: str, limit: int, offset: int, as_json: bool) -> None:
    paths = _resolve_db(vault_path)
    status_filter = None if status == "all" else status
    items = list_sources(paths["corpus_db"], status=status_filter, limit=limit, offset=offset)
    _emit(items, as_json=as_json)


@sources.command(name="show")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_id")
@click.option("--json", "as_json", is_flag=True)
def sources_show(vault_path: Path, source_id: str, as_json: bool) -> None:
    paths = _resolve_db(vault_path)
    item = read_source(paths["corpus_db"], source_id)
    if not item:
        _err(f"source not found: {source_id}")
    _emit(item, as_json=as_json)


@sources.command(name="commit")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_id")
@click.option("--json", "as_json", is_flag=True)
def sources_commit(vault_path: Path, source_id: str, as_json: bool) -> None:
    """标记 source 为 committed（agent 完成 extract+write_concept 后调用）。"""
    paths = _resolve_db(vault_path)
    try:
        result = commit_source(paths["corpus_db"], source_id)
    except Exception as e:
        _err(str(e))
    _emit(result, as_json=as_json)


@sources.command(name="delete")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_id")
@click.option("--yes", is_flag=True, help="跳过 dry-run 检查，直接删除")
@click.option("--dry-run/--no-dry-run", default=True, help="默认先 dry-run")
@click.option("--reason", default=None, help="删除原因")
@click.option("--json", "as_json", is_flag=True)
def sources_delete(vault_path: Path, source_id: str, yes: bool, dry_run: bool, reason: str | None, as_json: bool) -> None:
    """软删除 source（status=deleted）。不级联删 concept。

    默认先 dry-run 看 impact；--yes 跳过 dry-run 直接执行。
    """
    paths = _resolve_db(vault_path)
    # 显示 dry-run preview（除非已经 --yes 强制执行）
    if not yes:
        try:
            preview = dry_run_delete_source(paths["corpus_db"], source_id)
        except Exception as e:
            _err(str(e))
        if as_json:
            click.echo(json.dumps(preview, indent=2, ensure_ascii=False))
        else:
            click.echo("=== DRY RUN: source delete impact ===")
            click.echo(f"  source_id: {source_id}")
            click.echo(f"  current_status: {preview['current_status']}")
            click.echo(f"  affected_concepts: {preview['affected_concepts_count']}")
            if preview["would_become_orphans"]:
                click.echo(f"  will orphan: {preview['would_become_orphans']}")
            for c in preview["still_supported"]:
                click.echo(f"  still supported (other sources): {c}")
            click.echo("")
            click.echo(f"  recommendation: {preview['recommendation']}")
            click.echo("")
            click.echo("  pass --yes to actually delete")
        if dry_run:
            return  # 默认 dry-run 模式下永远不执行删除
    try:
        result = soft_delete_source(paths["corpus_db"], source_id, deleted_reason=reason)
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


# ---------- concepts ----------

# 顶层 stats alias（agent 常用）
cli.add_command(vault_stats, name="stats")

# 顶层 index sync（导出 wiki/index/*.json）
@cli.command(name="index")
@click.argument("subcmd", type=click.Choice(["sync"]))
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def cli_index(subcmd: str, vault_path: Path, as_json: bool) -> None:
    """维护 wiki/index/ 全局索引。

    corpus index sync <vault> → 重建 wiki/index/concepts.json + sources.json
    """
    if subcmd != "sync":
        _err(f"unknown subcmd: {subcmd}")
    paths = _resolve_db(vault_path)
    try:
        result = export_index(paths["corpus_db"], paths["wiki_index"])
    except Exception as e:
        _err(str(e))
    _emit(result, as_json=as_json)


@cli.group()
def concepts() -> None:
    """Wiki concept 管理：write / show / list / search / find-by-link / update / 认证。"""


@concepts.command(name="write")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--slug", required=True, help="filesystem-safe slug")
@click.option("--title", required=True)
@click.option("--body", required=True, help="wiki body (markdown)")
@click.option("--extractions", "extractions_json", required=True,
              help='JSON array: [{"source_id":"abc","quote_span":"..."}, ...]')
@click.option("--prompt-version", default=None)
@click.option("--links", "links", default="", help="逗号分隔的 wikilink slug 列表")
@click.option("--status", default="draft",
              help="concept 生命周期 (draft / evergreen / stale)")
@click.option("--aliases", default="",
              help="逗号分隔的别名 (find-by-link 备用, 例 'MVCC,多版本并发')")
@click.option("--tags", default="",
              help="逗号分隔的 tags (概念分类, 例 'concept,database')")
@click.option("--json", "as_json", is_flag=True)
def concepts_write(
    vault_path: Path, slug: str, title: str, body: str,
    extractions_json: str, prompt_version: str | None,
    links: str, status: str, aliases: str, tags: str,
    as_json: bool,
) -> None:
    """写一篇 wiki concept。slug 已存在 → ConflictError。

    必须传 --extractions：每个 source 一段 quote_span 原文证据。
    """
    paths = _resolve_db(vault_path)
    try:
        extractions_data = json.loads(extractions_json)
    except json.JSONDecodeError as e:
        _err(f"--extractions 不是合法 JSON: {e}")
    if not isinstance(extractions_data, list):
        _err("--extractions 必须是 JSON array")

    link_list = [s.strip() for s in links.split(",") if s.strip()]
    alias_list = [s.strip() for s in aliases.split(",") if s.strip()] if aliases else None
    tag_list = [s.strip() for s in tags.split(",") if s.strip()] if tags else None

    try:
        result = write_concept(
            paths["corpus_db"],
            slug=slug, title=title, body=body,
            extractions_data=extractions_data,
            links=link_list,
            prompt_version=prompt_version,
            status=status,
            aliases=alias_list,
            tags=tag_list,
        )
        # 物理写文件 (失败需回滚 DB, 避免概念存在但 wiki 文件缺的不一致)
        # frontmatter 含全部 metadata (slug/title/source_ids/links/version/created_at/updated_at/...)
        # 让 wiki/concept/<slug>.md 是 source of truth (git 跟踪, 换电脑能 recover)
        wiki_path = None
        try:
            wiki_path = write_concept_file(
                paths["root"], slug=slug, title=title, body=body,
                source_ids=result["source_ids"],
                links=link_list,
                version=result["version"],
                created_at=result["created_at"],
                status=status,
                aliases=alias_list,
                tags=tag_list,
            )
        except OSError as e:
            delete_concept(paths["corpus_db"], slug)
            _err(
                f"failed to write wiki file {wiki_path}: {e}",
                hint="DB rows rolled back via delete_concept; no orphan concept left",
            )
        result["wiki_path"] = str(wiki_path) if wiki_path else None
        # 双向同步: 更新每个 source page 的 '## Concepts extracted' 段 (反查 extractions)
        for sid in result["source_ids"]:
            update_source_page_concepts(paths["root"], sid)
        # 自动 export index
        export_index(paths["corpus_db"], paths["wiki_index"])
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="update")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--title", default=None, help="新标题 (省略则不改)")
@click.option("--body", default=None, help="新正文 (省略则不改)")
@click.option("--status", default=None, help="新 status (省略则不改)")
@click.option("--add-extractions", "add_extractions_json", default=None,
              help='JSON array: [{"source_id":"...","quote_span":"..."}, ...] (增量加 extractions, 必填 quote_span)')
@click.option("--add-links", "add_links", default="", help="逗号分隔 wikilink slugs (slug-safe, 拒绝自引用)")
@click.option("--prompt-version", default=None)
@click.option("--expected-version", type=int, default=None,
              help="CAS: 只在 concept 当前 version 等于此值时 update, 否则 OptimisticLockError. "
                   "agent read-modify-write 模式必传 (防 multi-agent 覆盖丢失).")
@click.option("--json", "as_json", is_flag=True)
def concepts_update(
    vault_path: Path, slug: str,
    title: str | None, body: str | None, status: str | None,
    add_extractions_json: str | None, add_links: str,
    prompt_version: str | None, expected_version: int | None,
    as_json: bool,
) -> None:
    """增量更新 concept: 改 title/body, 加 extractions, 加 wikilinks.

    仅做增量 (与 write_concept 不同, 没有"全量覆盖 extractions"接口).
    想重写 evidence 请用 `concepts write` 不同的 slug, 或先 `delete` 再 `write`.

    --expected-version: 乐观锁 CAS 标记. agent read-modify-write 工作流:
      1. read_concept 拿 current.version
      2. LLM merge 决定新 body/extractions
      3. update_concept --expected-version=<current.version> 提交
      4. 抛 OptimisticLockError -> 回 1 重新 read + merge
    不传 = last-write-wins (快但多 agent 场景可能丢数据).
    """
    add_extractions = None
    if add_extractions_json:
        try:
            add_extractions = json.loads(add_extractions_json)
        except json.JSONDecodeError as e:
            _err(f"--add-extractions 不是合法 JSON: {e}")
        if not isinstance(add_extractions, list):
            _err("--add-extractions 必须是 JSON array")

    link_list = [s.strip() for s in add_links.split(",") if s.strip()] if add_links else None

    paths = _resolve_db(vault_path)
    try:
        result = update_concept(
            paths["corpus_db"],
            slug=slug,
            title=title,
            body=body,
            add_extractions=add_extractions,
            add_links=link_list,
            prompt_version=prompt_version,
            expected_version=expected_version,
            status=status,
        )
        # 物理写文件 (DB 已更新, 同步 frontmatter 反映最新 metadata)
        # 含 status: 之前版本漏判, 导致仅改 status 时 DB 更新但 markdown 不重写.
        if (
            body is not None or title is not None or status is not None
            or add_extractions is not None or link_list is not None
        ):
            _sync_concept_file(paths, slug)
            info = read_concept(paths["corpus_db"], slug) or {}
            result["wiki_path"] = str(paths["wiki_concept"] / f"{slug}.md")
        export_index(paths["corpus_db"], paths["wiki_index"])
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="delete")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--dry-run/--no-dry-run", default=True,
              help="默认先 dry-run 显示会删什么; --no-dry-run 直接删")
@click.option("--json", "as_json", is_flag=True)
def concepts_delete(vault_path: Path, slug: str, dry_run: bool, as_json: bool) -> None:
    """硬删 concept + 清理 extractions / links. 不影响 source 表.

    是 user-level 决策, 没二次确认 flag; dry-run 默认开, 看清再 --no-dry-run.
    """
    paths = _resolve_db(vault_path)
    from .storage import read_concept
    info = read_concept(paths["corpus_db"], slug)
    if not info:
        _err(f"concept not found: {slug}")
    preview = {
        "slug": slug,
        "title": info.get("title"),
        "source_ids": info.get("source_ids", []),
        "links": info.get("links", []),
        "is_orphan": info.get("is_orphan", False),
        "would_delete": True,
        "action": "DRY-RUN: pass --no-dry-run to actually delete",
    }
    if dry_run:
        _emit(preview, as_json=as_json)
        return
    try:
        result = delete_concept(paths["corpus_db"], slug)
        # 同时清 wiki 文件 (best-effort)
        wiki_path = paths["wiki_concept"] / f"{slug}.md"
        if wiki_path.exists():
            try:
                wiki_path.unlink()
                result["wiki_file_removed"] = True
            except OSError:
                result["wiki_file_removed"] = False
        export_index(paths["corpus_db"], paths["wiki_index"])
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="show")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--json", "as_json", is_flag=True)
def concepts_show(vault_path: Path, slug: str, as_json: bool) -> None:
    paths = _resolve_db(vault_path)
    item = read_concept(paths["corpus_db"], slug)
    if not item:
        _err(f"concept not found: {slug}")
    _emit(item, as_json=as_json)


@concepts.command(name="list")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--orphans", "orphans_only", is_flag=True, help="只看 orphan (无 source_ids)")
@click.option("--certified", "certified_only", is_flag=True, help="只看已认证")
@click.option("--uncertified", "uncertified_only", is_flag=True, help="只看未认证")
@click.option("--status", type=click.Choice(["draft", "evergreen", "stale"]), default=None,
              help="按 status 过滤 (schema v5)")
@click.option("--limit", type=int, default=50)
@click.option("--offset", type=int, default=0)
@click.option("--json", "as_json", is_flag=True)
def concepts_list(
    vault_path: Path, orphans_only: bool, certified_only: bool, uncertified_only: bool,
    status: str | None, limit: int, offset: int, as_json: bool,
) -> None:
    if certified_only and uncertified_only:
        _err("--certified and --uncertified are mutually exclusive")
    paths = _resolve_db(vault_path)
    items = list_concepts(
        paths["corpus_db"], limit=limit, offset=offset,
        is_orphan=True if orphans_only else None,
        is_certified=True if certified_only else (False if uncertified_only else None),
        status=status,
    )
    _emit(items, as_json=as_json)


@concepts.command(name="search")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("query")
@click.option("--limit", type=int, default=10)
@click.option("--json", "as_json", is_flag=True)
def concepts_search(vault_path: Path, query: str, limit: int, as_json: bool) -> None:
    paths = _resolve_db(vault_path)
    items = search_concepts(paths["corpus_db"], query, limit=limit)
    _emit(items, as_json=as_json)


@concepts.command(name="find-by-link")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("link")
@click.option("--json", "as_json", is_flag=True)
def concepts_find_by_link(vault_path: Path, link: str, as_json: bool) -> None:
    """解析 wikilink → candidate concept list（dedup 用）.

    评分: discrete (exact/startswith/contains/title) + difflib fuzzy bonus.
    """
    paths = _resolve_db(vault_path)
    items = find_concept_by_link(paths["corpus_db"], link)
    _emit(items, as_json=as_json)


@concepts.command(name="dedup-candidates")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--limit", "-n", type=int, default=20, help="最多返 N 个候选 (默认 20)")
@click.option("--json", "as_json", is_flag=True)
def concepts_dedup_candidates(
    vault_path: Path, slug: str, limit: int, as_json: bool,
) -> None:
    """返 dedup 候选 + 多维度分数, 让 LLM 二次判断 '这两个 concept 真的是同一个吗'.

    字段:
      - discrete_score (0-1): 离散几档 (exact/startswith/contains/title_contains)
      - fuzzy_score (0-0.3): difflib.SequenceMatcher.ratio() 连续相似度
      - length_diff: |len(slug) - len(target)| (同 score 时短 slug 优先)
      - match_score (0-1): min(1.0, discrete + fuzzy) 综合

    比 find-by-link 多了 fuzzy 细节, 让 LLM 知道 'score=0.7' 怎么来的 (discrete 0.4 + fuzzy 0.3
    vs discrete 0.9 + fuzzy 0.0 含义不同). LLM 拿到后用自己语义能力判断.

    不调 LLM, 纯 storage 计算. LLM 决策权在 agent 端.
    """
    paths = _resolve_db(vault_path)
    candidates = dedup_candidate_scores(paths["corpus_db"], slug, limit=limit)
    if as_json:
        _emit(
            {
                "query": slug,
                "total": len(candidates),
                "candidates": candidates,
            },
            as_json=True,
        )
        return
    if not candidates:
        _emit(f"(no candidates for: {slug})", as_json=False)
        return
    click.echo(f"dedup candidates for '{slug}' ({len(candidates)} entries):")
    for c in candidates:
        click.echo(
            f"  match={c['match_score']:.3f}  "
            f"discrete={c['discrete_score']:.2f}  "
            f"fuzzy={c['fuzzy_score']:.2f}  "
            f"len_diff={c['length_diff']:>2}  "
            f"sources={c['source_count']:>2}  "
            f"{c['slug']:<32} {c['title']}"
        )


@concepts.command(name="uncertified")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--limit", type=int, default=20)
@click.option("--json", "as_json", is_flag=True)
def concepts_uncertified(vault_path: Path, limit: int, as_json: bool) -> None:
    paths = _resolve_db(vault_path)
    items = list_uncertified_concepts(paths["corpus_db"], limit=limit)
    _emit(items, as_json=as_json)


@concepts.command(name="certify")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--score", type=float, default=None, help="0.0-1.0; 省略则保留旧值")
@click.option("--issues", default=None, help="逗号分隔; 省略=保留; 传 \"\"=清空")
@click.option("--suggestions", default=None, help="同上")
@click.option("--by", "certified_by", default="agent")
@click.option("--json", "as_json", is_flag=True)
def concepts_certify(
    vault_path: Path, slug: str, score: float | None,
    issues: str | None, suggestions: str | None,
    certified_by: str, as_json: bool,
) -> None:
    """标记/部分更新认证. score/issues/suggestions 都可选.

    - 全省略: 报错 (no-op)
    - 部分省略: 保留旧值
    - 全传: 全量覆盖 (传 \"\" 表示清空 list)

    首次认证必须传 --score.
    """
    paths = _resolve_db(vault_path)
    issue_list: list[str] | None = None
    if issues is not None:
        issue_list = [s.strip() for s in issues.split(",")]
        # 保留显式传空 (清空 list) 的语义: 不 strip 后再过滤
    sugg_list: list[str] | None = None
    if suggestions is not None:
        sugg_list = [s.strip() for s in suggestions.split(",")]
    try:
        result = mark_certified(
            paths["corpus_db"],
            slug=slug, score=score, issues=issue_list,
            suggestions=sugg_list, certified_by=certified_by,
        )
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    # 同步 frontmatter (certified_at / score / issues / suggestions 写进 markdown)
    _sync_concept_file(paths, slug)
    export_index(paths["corpus_db"], paths["wiki_index"])
    _emit(result, as_json=as_json)


@concepts.command(name="unmark")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--json", "as_json", is_flag=True)
def concepts_unmark(vault_path: Path, slug: str, as_json: bool) -> None:
    paths = _resolve_db(vault_path)
    try:
        result = unmark_certified(paths["corpus_db"], slug)
    except Exception as e:
        _err(str(e))
    _emit(result, as_json=as_json)


@concepts.command(name="evidence")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source-id", default=None, help="只看这个 source 的证据")
@click.option("--json", "as_json", is_flag=True)
def concepts_evidence(vault_path: Path, slug: str, source_id: str | None, as_json: bool) -> None:
    """查 concept 的抽取证据（quote_span + agent + prompt + time）。"""
    paths = _resolve_db(vault_path)
    if source_id:
        evidence = get_concept_evidence(paths["corpus_db"], slug, source_id)
        if evidence is None:
            _err(f"no extraction found for {slug} from {source_id}")
        _emit(evidence, as_json=as_json)
    else:
        from .storage import get_concept_evidence_summary
        summary = get_concept_evidence_summary(paths["corpus_db"], slug)
        _emit(summary, as_json=as_json)


@concepts.command(name="add-source")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source-id", required=True)
@click.option("--quote-span", required=True)
@click.option("--prompt-version", default=None)
@click.option("--json", "as_json", is_flag=True)
def concepts_add_source(vault_path: Path, slug: str, source_id: str, quote_span: str, prompt_version: str | None, as_json: bool) -> None:
    """给 concept 加一个 source（自动写 extractions + 清 is_orphan）。"""
    paths = _resolve_db(vault_path)
    try:
        result = add_source_to_concept(
            paths["corpus_db"], slug, source_id,
            quote_span=quote_span, prompt_version=prompt_version,
        )
        export_index(paths["corpus_db"], paths["wiki_index"])
        _sync_concept_file(paths, slug)
        # 双向同步: 更新 source wiki page 的 '## Concepts extracted' 段
        update_source_page_concepts(paths["root"], source_id)
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="remove-extraction")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("extraction_id")
@click.option("--json", "as_json", is_flag=True)
def concepts_remove_extraction(vault_path: Path, extraction_id: str, as_json: bool) -> None:
    """细粒度撤一次抽取 (deletes extractions 行 + sync concept.source_ids).

    与 `concepts remove-source` 粗粒度 (撤掉整个 source) 互补:
    同 (concept, source) 多次抽取时, 这个只撤其中一次.

    sync 语义: 删的是该 sid 的最后一条 extraction → 从 concept.source_ids 移除,
    并按需 is_orphan=1.
    """
    paths = _resolve_db(vault_path)
    try:
        result = remove_extraction(paths["corpus_db"], extraction_id)
        export_index(paths["corpus_db"], paths["wiki_index"])
        _sync_concept_file(paths, result["concept_slug"])
        # result 含 source_id (remove_extraction return)
        if result.get("source_id"):
            update_source_page_concepts(paths["root"], result["source_id"])
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="remove-source")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source-id", required=True)
@click.option("--json", "as_json", is_flag=True)
def concepts_remove_source(vault_path: Path, slug: str, source_id: str, as_json: bool) -> None:
    """从 concept.source_ids 移除一个 source（自动更新 is_orphan）。"""
    paths = _resolve_db(vault_path)
    try:
        result = remove_source_from_concept(paths["corpus_db"], slug, source_id)
        export_index(paths["corpus_db"], paths["wiki_index"])
        _sync_concept_file(paths, slug)
        update_source_page_concepts(paths["root"], source_id)
    except Exception as e:
        _err(str(e))
    _emit(result, as_json=as_json)


def main() -> int:
    """CLI entry point."""
    try:
        cli(standalone_mode=False)
        return 0
    except click.exceptions.Abort:
        return 130
    except click.UsageError as e:
        click.echo(f"usage: {e.format_message()}", err=True)
        return 2
    except CorpusBotError as e:
        click.echo(f"error: {e.message}", err=True)
        if e.hint:
            click.echo(f"  hint: {e.hint}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
