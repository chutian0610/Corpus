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
    # 注意: 不传 links= 字段. frontmatter 不写 `links:`, body wikilinks 是 sole source of truth.
    write_concept_file(
        paths["root"],
        slug=slug,
        title=info["title"],
        body=info["body"],
        source_ids=info["source_ids"],
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
    """解析 vault 路径, 返回 db_path.

    每次都跑 init_db (幂等): 没 DB 就 CREATE, 有 DB 但 schema_version 旧就跑
    migration. 这样 CLI 任一命令都会按需升级, 用户不用单独跑 `corpus vault
    upgrade`. init_db 自身 cheap (1 个 SELECT schema_meta + 必要时 ALTER), 不
    影响读性能.
    """
    if not vault_root.exists():
        _err(f"vault does not exist: {vault_root}", hint="run `corpus vault init <path>` first")
    if ensure_vault_dir:
        ensure_vault(vault_root)
    paths = vault_paths(vault_root)
    init_db(paths["corpus_db"])  # idempotent + auto-migrate
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

    # 7. 写 wiki/source/<slug>.md (extraction manifest, 不复制原文)
    try:
        write_source_wiki_page(
            paths["root"],
            source_id=result["source_id"],
            slug=slug,
            content_hash=result["content_hash"],
            size_bytes=result["size_bytes"],
            status="staged",
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


@vault.command(name="inspect")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def vault_inspect(vault_path: Path, as_json: bool) -> None:
    """vault 体检 + 内容统计 (合并老 `vault info` + `vault stats`).

    返回: db_initialized / schema_version / concepts (total, certified,
    uncertified, orphans, avg_score, score_distribution) / sources (total, by_status).
    """
    paths = _resolve_db(vault_path)
    from .storage import vault_inspect as _vault_inspect
    info = _vault_inspect(paths["corpus_db"])
    _emit(
        {
            "vault": str(vault_path),
            "paths": {k: str(v) for k, v in paths.items()},
            **info,
        },
        as_json=as_json,
    )


# 兼容性 alias: 老的 `vault info` / `vault stats` 现在都 forward 到 `vault inspect`
@vault.command(name="info", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def vault_info_alias(vault_path: Path, as_json: bool) -> None:
    """[deprecated] alias for `vault inspect`. 会下个 release 删."""
    ctx = click.get_current_context()
    ctx.forward(vault_inspect)


@vault.command(name="stats", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def vault_stats_alias(vault_path: Path, as_json: bool) -> None:
    """[deprecated] alias for `vault inspect`. 会下个 release 删."""
    ctx = click.get_current_context()
    ctx.forward(vault_inspect)


# ---------- sources ----------

@cli.group()
def sources() -> None:
    """源文件管理：ingest / batch / list / show / commit / delete。"""


@sources.command(name="add")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("path", type=click.Path(exists=False, path_type=Path))
@click.option("--glob", "glob_pattern", default="*.md",
              help="当 path 是目录时, 用此 glob 匹配文件 (默认 *.md)")
@click.option("--recursive/--no-recursive", default=True,
              help="当 path 是目录时, 是否递归子目录 (默认 recursive)")
@click.option("--force-revive", is_flag=True,
              help="同 hash 已 soft-deleted → 复活该 source (status=staged), 而非报 ConflictError")
@click.option("--json", "as_json", is_flag=True)
def sources_add(vault_path: Path, path: Path, glob_pattern: str, recursive: bool,
                force_revive: bool, as_json: bool) -> None:
    """单文件或目录入库 (统一老的 `sources ingest` + `sources batch`).

    path 是文件 → 单文件 ingest (content-hash dedup + --force-revive)
    path 是目录 → batch mode 按 --glob (default *.md), --recursive (default True)

    vault 内任何文件不能 ingest — 报错 'path is inside vault'.
    """
    paths = _resolve_db(vault_path)

    try:
        path = path.resolve(strict=False)
        if path.is_dir():
            # batch mode (auto-detect from path = directory)
            matches = sorted(path.rglob(glob_pattern) if recursive else path.glob(glob_pattern))
            if not matches:
                _emit({"total": 0, "staged": 0, "revived": 0, "duplicates": 0, "failed": 0,
                       "results": [], "skipped_empty_dir": True}, as_json=as_json)
                return
            from .errors import ValidationError
            results = []
            n_staged = n_dup = n_revived = n_fail = 0
            for src in matches:
                try:
                    content = _read_file(src)
                except Exception as e:
                    results.append({"source": str(src), "action": "failed", "message": str(e)})
                    n_fail += 1
                    continue
                sid = source_id_from_content(content)
                existing = read_source(paths["corpus_db"], sid)
                if existing and existing["status"] != "deleted":
                    results.append({"source": str(src), "action": "duplicate",
                                    "existing_id": existing["source_id"]})
                    n_dup += 1
                    continue
                try:
                    r = _ingest_one_source(paths, src, force_revive=force_revive)
                    act = "revived" if r.get("revived") else "staged"
                    results.append({"source": str(src), "action": act, "source_id": r["source_id"]})
                    n_revived += 1 if act == "revived" else 0
                    n_staged += 1 if act == "staged" else 0
                except CorpusBotError as e:
                    results.append({"source": str(src), "action": "failed",
                                    "message": str(e), "hint": getattr(e, "hint", None)})
                    n_fail += 1
            _emit({"total": len(matches), "staged": n_staged, "revived": n_revived,
                   "duplicates": n_dup, "failed": n_fail, "results": results}, as_json=as_json)
            return
        # single file mode — 校验 + 防 vault 内
        try:
            canonical = validate_source_path_basic(path, paths["root"])
        except Exception as e:
            _err(str(e))
        try:
            assert_source_outside_vault(canonical, paths["root"], paths["raw"])
        except Exception as e:
            _err(str(e), hint=getattr(e, "hint", None))
        result = _ingest_one_source(paths, path, force_revive=force_revive)
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
    except CorpusBotError as e:
        _err(str(e), hint=getattr(e, "hint", None))


# 老 `sources batch` 改为 alias — 调用 sources_add (dir 模式)
@sources.command(name="batch", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--glob", "glob_pattern", default="*.md", help="[deprecated] 走 sources add --glob")
@click.option("--recursive/--no-recursive", default=True, help="[deprecated] 走 sources add --recursive/--no-recursive")
@click.option("--force-revive", is_flag=True, help="[deprecated] 走 sources add --force-revive")
@click.option("--json", "as_json", is_flag=True)
def sources_batch_alias(vault_path: Path, source_dir: Path, glob_pattern: str,
                         recursive: bool, force_revive: bool, as_json: bool) -> None:
    """[deprecated] alias for `sources add <vault> <dir>`. 下 release 删."""
    ctx = click.get_current_context()
    ctx.invoke(sources_add, vault_path=vault_path, path=source_dir,
               glob_pattern=glob_pattern, recursive=recursive,
               force_revive=force_revive, as_json=as_json)


@sources.command(name="list")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--status", type=click.Choice(["staged", "committed", "deleted", "all"]), default="all")
@click.option("--limit", type=int, default=50)
@click.option("--offset", type=int, default=0)
@click.option("--json", "as_json", is_flag=True)
def sources_list(vault_path: Path, status: str, limit: int, offset: int, as_json: bool) -> None:
    """列 source. --status 过滤 (staged/committed/deleted/all; 默认 all)."""
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


@sources.command(name="mark-state")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_id")
@click.option("--status", required=True, type=click.Choice(["staged", "committed", "deleted"]),
              help="目标状态 (staged | committed | deleted)")
@click.option("--reason", default=None, help="deleted 时存的 audit 理由")
@click.option("--json", "as_json", is_flag=True)
def sources_mark_state(
    vault_path: Path, source_id: str, status: str, reason: str | None, as_json: bool,
) -> None:
    """通用化 source 状态切换 (替代老的 `sources commit` 单向 staged→committed).

    --status=committed  设 committed_at (extract 跑完 mark)
    --status=deleted    软删 (status='deleted', 不级联 concept)
    --status=staged     清 committed_at / deleted_at (re-staging)
    """
    paths = _resolve_db(vault_path)
    from .storage import mark_source_state
    try:
        result = mark_source_state(
            paths["corpus_db"], source_id,
            new_status=status, reason=reason,
        )
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


# 兼容 alias: 老 `sources commit` → mark-state --status committed
@sources.command(name="commit", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_id")
@click.option("--json", "as_json", is_flag=True)
def sources_commit_alias(vault_path: Path, source_id: str, as_json: bool) -> None:
    """[deprecated] alias for `sources mark-state --status committed`. 下 release 删."""
    ctx = click.get_current_context()
    ctx.forward(sources_mark_state, source_id=source_id, status="committed")



# 老 `sources ingest <vault> <file>` 改为 alias — 转发到 sources_add
@sources.command(name="ingest", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_file", type=click.Path(exists=False, path_type=Path))
@click.option("--force-revive", is_flag=True, help="[deprecated] 走 sources add --force-revive")
@click.option("--json", "as_json", is_flag=True)
def sources_ingest_alias(vault_path: Path, source_file: Path, force_revive: bool, as_json: bool) -> None:
    """[deprecated] alias for `sources add <vault> <file>`. 下 release 删."""
    ctx = click.get_current_context()
    # sources_add 期望 path= 参数. 用 explicit kwargs 避免 click 把 source_file 透传
    ctx.invoke(sources_add, vault_path=vault_path, path=source_file,
               force_revive=force_revive, as_json=as_json)
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
# 顶层 index sync（opt-in — 不再被 concept write / update / delete 自动触发, 想要 snapshot 手动跑）
@cli.command(name="index")
@click.argument("subcmd", type=click.Choice(["snapshot"]))
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--out-dir", type=click.Path(path_type=Path), default=None,
              help="输出目录 (default wiki/index/)")
@click.option("--json", "as_json", is_flag=True)
def cli_index(subcmd: str, vault_path: Path, out_dir: Path | None, as_json: bool) -> None:
    """生成 wiki/index/ snapshot 给外部消费者 (opt-in, 替代老 `index sync`).

    `corpus index snapshot <vault>` → 写 wiki/index/concepts.json + sources.json.
    不会自动触发 (caller 想生成手动跑).
    """
    if subcmd != "snapshot":
        _err(f"unknown subcmd: {subcmd} (use 'snapshot')")
    paths = _resolve_db(vault_path)
    target = out_dir or paths["wiki_index"]
    try:
        result = export_index(paths["corpus_db"], target)
    except Exception as e:
        _err(str(e))
    _emit(result, as_json=as_json)


# 兼容性 alias (subcommand 名字从 sync → snapshot 自动迁移)
@cli.command(name="sync", hidden=True,
             context_settings={"ignore_unknown_options": True})
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def cli_index_sync_alias(vault_path: Path, as_json: bool) -> None:
    """[deprecated] alias for `corpus index snapshot`. 下 release 删。"""
    ctx = click.get_current_context()
    ctx.invoke(cli_index, subcmd="snapshot", vault_path=vault_path, as_json=as_json)


@cli.group()
def concepts() -> None:
    """Wiki concept 管理：write / show / list / search / find-by-link / update / 认证。"""


@concepts.command(name="write")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--slug", required=True, help="filesystem-safe slug")
@click.option("--title", required=True)
@click.option("--body", default=None, help="wiki body markdown 字符串 (与 --body-file 互斥)")
@click.option("--body-file", "body_file",
              type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
              default=None,
              help="从文件读 body (省 LLM shell 转义; 与 --body 互斥)")
@click.option("--extractions", "extractions_json", default=None,
              help='inline JSON array, 与 --extractions-file 互斥. 例: [{"source_id":"abc","quote_span":"..."}]')
@click.option("--extractions-file", "extractions_file",
              type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
              default=None,
              help="从文件读 extractions JSON array (省 LLM shell 转义; 与 --extractions 互斥)")
@click.option("--prompt-version", default=None)
@click.option("--status", default="draft",
              help="concept 生命周期 (draft / evergreen / stale)")
@click.option("--aliases", default="",
              help="逗号分隔的别名 (find-by-link 备用, 例 'MVCC,多版本并发')")
@click.option("--tags", default="",
              help="逗号分隔的 tags (概念分类, 例 'concept,database')")
@click.option("--json", "as_json", is_flag=True)
def concepts_write(
    vault_path: Path, slug: str, title: str, body: str | None, body_file: Path | None,
    extractions_json: str | None, extractions_file: Path | None,
    prompt_version: str | None,
    status: str, aliases: str, tags: str,
    as_json: bool,
) -> None:
    """写一篇 wiki concept。slug 已存在 → ConflictError。

    --body / --body-file 二选一, --extractions / --extractions-file 二选一.
    file 形式省 LLM shell 转义 (多行 markdown / JSON 数组这两类最容易翻车).
    必须传 --extractions(--extractions-file) 之一: 每个 source 一段 quote_span 原文证据.
    """
    # --body / --body-file 互斥
    if body is not None and body_file is not None:
        _err("--body 和 --body-file 互斥, 不能同时传")
    if body is None and body_file is None:
        _err("必须传 --body 或 --body-file 之一")
    if body_file is not None:
        body = body_file.read_text(encoding="utf-8")
        if len(body.encode("utf-8")) > 1024 * 1024:
            _err(f"--body-file 过大 (>1 MiB): {body_file}")

    # --extractions / --extractions-file 互斥
    if extractions_json is not None and extractions_file is not None:
        _err("--extractions 和 --extractions-file 互斥, 不能同时传")
    if extractions_json is None and extractions_file is None:
        _err("必须传 --extractions 或 --extractions-file 之一")
    if extractions_file is not None:
        raw = extractions_file.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > 1024 * 1024:
            _err(f"--extractions-file 过大 (>1 MiB): {extractions_file}")
        extractions_json = raw

    paths = _resolve_db(vault_path)
    try:
        extractions_data = json.loads(extractions_json)
    except json.JSONDecodeError as e:
        _err(f"--extractions 不是合法 JSON: {e}")
    if not isinstance(extractions_data, list):
        _err("--extractions 必须是 JSON array")

    # outgoing links 从 body [[wikilinks]] 自动派生 (storage._extract_wikilinks).
    alias_list = [s.strip() for s in aliases.split(",") if s.strip()] if aliases else None
    tag_list = [s.strip() for s in tags.split(",") if s.strip()] if tags else None

    try:
        result = write_concept(
            paths["corpus_db"],
            slug=slug, title=title, body=body,
            extractions_data=extractions_data,
            prompt_version=prompt_version,
            status=status,
            aliases=alias_list,
            tags=tag_list,
        )
        # 物理写文件 (失败需回滚 DB, 避免概念存在但 wiki 文件缺的不一致)
        # frontmatter 含全部 metadata (slug/title/source_ids/links/version/created_at/updated_at/...)
        # frontmatter 不写 `links:` 字段 (Obsidian 不显示 frontmatter,
        # body [[wikilinks]] 才是 sole source of truth for inter-concept links)
        wiki_path = None
        try:
            wiki_path = write_concept_file(
                paths["root"], slug=slug, title=title, body=body,
                source_ids=result["source_ids"],
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
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="update")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--title", default=None, help="新标题 (省略则不改)")
@click.option("--body", default=None, help="新正文 markdown 字符串 (与 --body-file 互斥)")
@click.option("--body-file", "body_file",
              type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
              default=None,
              help="从文件读 body (省 LLM shell 转义; 与 --body 互斥)")
@click.option("--status", default=None, help="新 status (省略则不改)")
@click.option("--add-extractions", "add_extractions_json", default=None,
              help='inline JSON array, 与 --add-extractions-file 互斥. 例: [{"source_id":"...","quote_span":"..."}]')
@click.option("--add-extractions-file", "add_extractions_file",
              type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
              default=None,
              help="从文件读 add-extractions JSON array (与 --add-extractions 互斥)")
# outgoing links 通过 body [[wikilinks]] 表达, 不用 --add-links 入参.
@click.option("--prompt-version", default=None)
@click.option("--expected-version", type=int, default=None,
              help="CAS: 只在 concept 当前 version 等于此值时 update, 否则 OptimisticLockError. "
                   "agent read-modify-write 模式必传 (防 multi-agent 覆盖丢失).")
@click.option("--json", "as_json", is_flag=True)
def concepts_update(
    vault_path: Path, slug: str,
    title: str | None, body: str | None, body_file: Path | None,
    status: str | None,
    add_extractions_json: str | None, add_extractions_file: Path | None,
    prompt_version: str | None, expected_version: int | None,
    as_json: bool,
) -> None:
    # --body / --body-file 互斥
    if body is not None and body_file is not None:
        _err("--body 和 --body-file 互斥, 不能同时传")
    if body_file is not None:
        body = body_file.read_text(encoding="utf-8")
        if len(body.encode("utf-8")) > 1024 * 1024:
            _err(f"--body-file 过大 (>1 MiB): {body_file}")

    # --add-extractions / --add-extractions-file 互斥
    if add_extractions_json is not None and add_extractions_file is not None:
        _err("--add-extractions 和 --add-extractions-file 互斥, 不能同时传")
    if add_extractions_file is not None:
        raw = add_extractions_file.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > 1024 * 1024:
            _err(f"--add-extractions-file 过大 (>1 MiB): {add_extractions_file}")
        add_extractions_json = raw
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

    paths = _resolve_db(vault_path)
    try:
        result = update_concept(
            paths["corpus_db"],
            slug=slug,
            title=title,
            body=body,
            add_extractions=add_extractions,
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
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="link")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source", "source_id", required=True, metavar="SID", help="要链接的 source_id")
@click.option("--quote-span", required=True, help="原文片段 (≥10字) — 必须出现于 source raw/<file>.md")
@click.option("--extraction-id", "extraction_id", default=None, metavar="X",
              help="[可选] 复用现有 extraction row X, UPDATE quote_span + 时间戳")
@click.option("--prompt-version", default=None)
@click.option("--json", "as_json", is_flag=True)
def concepts_link(
    vault_path: Path, slug: str, source_id: str, quote_span: str,
    extraction_id: str | None, prompt_version: str | None, as_json: bool,
) -> None:
    """链接 source → concept (fold of add-source + extraction-id UPDATE).

    --extraction-id 缺省 → INSERT 新 extractions row (允许多 evidence per (concept, source))
    --extraction-id 给出 → UPDATE 已有 row (X 必须属于此 concept + source)
    """
    paths = _resolve_db(vault_path)
    from .storage import link_extraction
    try:
        result = link_extraction(
            paths["corpus_db"], slug, source_id,
            quote_span=quote_span,
            extraction_id=extraction_id,
            prompt_version=prompt_version,
        )
        _sync_concept_file(paths, slug)
        update_source_page_concepts(paths["root"], source_id)
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


@concepts.command(name="unlink")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source", "source_id", default=None, metavar="SID",
              help="撤该 (concept, source) 全部 extractions + source_ids 减")
@click.option("--extraction-id", default=None, metavar="X",
              help="撤单条 extractions row + sync source_ids")
@click.option("--json", "as_json", is_flag=True)
def concepts_unlink(
    vault_path: Path, slug: str, source_id: str | None,
    extraction_id: str | None, as_json: bool,
) -> None:
    """解链 concept ↔ source (fold of remove-source + remove-extraction).

    互斥 + 必传其一:
      --source SID      撤该 source 来自此 concept 的全部 extractions + 清 source_ids
      --extraction-id X 撤单条 row + sync concept.source_ids
    """
    paths = _resolve_db(vault_path)
    from .storage import unlink_extraction
    try:
        result = unlink_extraction(
            paths["corpus_db"], slug,
            source_id=source_id, extraction_id=extraction_id,
        )
        # update affected concept + source pages
        _sync_concept_file(paths, slug)
        if result.get("source_id"):
            update_source_page_concepts(paths["root"], result["source_id"])
        elif source_id:
            update_source_page_concepts(paths["root"], source_id)
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))
    _emit(result, as_json=as_json)


# 兼容 alias: add-source / remove-source / remove-extraction








# 兼容 alias: add-source / remove-source / remove-extraction / evidence
@concepts.command(name="add-source", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source-id", required=True)
@click.option("--quote-span", required=True)
@click.option("--prompt-version", default=None)
@click.option("--json", "as_json", is_flag=True)
def concepts_add_source_alias(vault_path: Path, slug: str, source_id: str,
                                quote_span: str, prompt_version: str | None, as_json: bool) -> None:
    """[deprecated] alias for `concepts link`. 下 release 删."""
    ctx = click.get_current_context()
    ctx.invoke(concepts_link, vault_path=vault_path, slug=slug, source_id=source_id,
               quote_span=quote_span, prompt_version=prompt_version, as_json=as_json)


@concepts.command(name="remove-source", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source-id", required=True)
@click.option("--json", "as_json", is_flag=True)
def concepts_remove_source_alias(vault_path: Path, slug: str, source_id: str, as_json: bool) -> None:
    """[deprecated] alias for `concepts unlink --source`. 下 release 删."""
    ctx = click.get_current_context()
    ctx.invoke(concepts_unlink, vault_path=vault_path, slug=slug,
               source_id=source_id, as_json=as_json)


@concepts.command(name="remove-extraction", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("extraction_id")
@click.option("--json", "as_json", is_flag=True)
def concepts_remove_extraction_alias(vault_path: Path, extraction_id: str, as_json: bool) -> None:
    """[deprecated] — 用 corpus concepts show <slug> --source SID 列 evidence, 找到目标 extraction_id
    后用 `corpus concepts unlink --extraction-id X` 撤单条. 下 release 删."""
    # extraction_id 唯一且跨 concept 全局, 反查 slug
    try:
        from .storage import connect
        with connect(vault_path / ".wiki-meta" / "corpus.db") as conn:
            row = conn.execute(
                "SELECT concept_slug FROM extractions WHERE extraction_id=?",
                (extraction_id,),
            ).fetchone()
            if not row:
                _err(f"extraction not found: {extraction_id}", hint="先 corpus audit | sqlite3 .wiki-meta/corpus.db 查 extractions 表")
                return
            slug = row["concept_slug"]
        ctx = click.get_current_context()
        ctx.invoke(concepts_unlink, vault_path=vault_path, slug=slug,
                   extraction_id=extraction_id, as_json=as_json)
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))


@concepts.command(name="evidence", hidden=True)
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source-id", default=None)
@click.option("--json", "as_json", is_flag=True)
def concepts_evidence_alias(vault_path: Path, slug: str, source_id: str | None, as_json: bool) -> None:
    """[deprecated] alias for `concepts show <slug> --source <SID>`. 下 release 删."""
    ctx = click.get_current_context()
    ctx.invoke(concepts_show, vault_path=vault_path, slug=slug,
               source_id=source_id, as_json=as_json)


@concepts.command(name="show")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--source", "source_id", default=None, metavar="SID",
              help="只看该 source 的 evidence (替代老的 `concepts evidence`)")
@click.option("--json", "as_json", is_flag=True)
def concepts_show(vault_path: Path, slug: str, source_id: str | None, as_json: bool) -> None:
    """读 concept (含 frontmatter + body). --source SID filter 退化成 'only evidence from this source'."""
    paths = _resolve_db(vault_path)
    if source_id:
        # evidence-only 模式: 与老的 concepts evidence --source-id 等价
        ev = get_concept_evidence(paths["corpus_db"], slug, source_id)
        if ev is None:
            _err(f"no extraction found for {slug} from {source_id}")
        _emit(ev, as_json=as_json)
        return
    item = read_concept(paths["corpus_db"], slug)
    if not item:
        _err(f"concept not found: {slug}")
    _emit(item, as_json=as_json)


@concepts.command(name="list")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--orphans", "orphans_only", is_flag=True, help="只看 orphan (无 source_ids)")
@click.option("--certified", "certified_only", is_flag=True, help="只看已认证")
@click.option("--uncertified", "uncertified_only", is_flag=True, help="只看未认证 (与 --certified 互斥)")
@click.option("--status", type=click.Choice(["draft", "evergreen", "stale"]), default=None,
              help="按 status 过滤 (与下 flags 可叠加)")
@click.option("--tag", "tags", multiple=True,
              help="按 tag 过滤 (多 tag AND 取交集, 与其他 flags 可叠加)")
@click.option("--limit", type=int, default=50)
@click.option("--offset", type=int, default=0)
@click.option("--json", "as_json", is_flag=True)
def concepts_list(
    vault_path: Path, orphans_only: bool, certified_only: bool, uncertified_only: bool,
    status: str | None, tags: tuple[str, ...], limit: int, offset: int, as_json: bool,
) -> None:
    """列 concept.

    flags 不互斥 (除 --certified/--uncertified 二选一):
      --status draft|evergreen|stale   按 lifecycle 过滤
      --certified / --uncertified         二选一 (filter on certified_at IS NULL)
      --orphans                           filter on is_orphan=1
      --tag X [--tag Y]                   filter on tags JSON array contains all given (AND)
    """
    if certified_only and uncertified_only:
        _err("--certified and --uncertified are mutually exclusive")
    paths = _resolve_db(vault_path)
    items = list_concepts(
        paths["corpus_db"], limit=limit, offset=offset,
        is_orphan=True if orphans_only else None,
        is_certified=True if certified_only else (False if uncertified_only else None),
        status=status,
        tags=list(tags) if tags else None,
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
    _emit(result, as_json=as_json)


@concepts.command(name="delete")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("slug")
@click.option("--dry-run/--no-dry-run", default=True,
              help="默认先 dry-run 显示会删什么; --no-dry-run 直接删")
@click.option("--json", "as_json", is_flag=True)
def concepts_delete(
    vault_path: Path, slug: str, dry_run: bool, as_json: bool,
) -> None:
    """删 concept (默认 dry-run preview, --no-dry-run 真删)."""
    paths = _resolve_db(vault_path)
    from .storage import _parse_json_list
    try:
        if not dry_run:
            res = delete_concept(paths["corpus_db"], slug)
            wiki = paths["wiki_concept"] / f"{slug}.md"
            try:
                wiki.unlink()
                res["wiki_file_removed"] = True
            except OSError:
                res["wiki_file_removed"] = False
        else:
            from .storage import connect
            with connect(paths["corpus_db"]) as c:
                row = c.execute(
                    "SELECT source_ids FROM concepts WHERE slug=?", (slug,),
                ).fetchone()
            sids = _parse_json_list(row["source_ids"]) if row else []
            res = {"slug": slug, "dry_run": True, "would_delete": True, "sources_count": len(sids)}
        _emit(res, as_json=as_json)
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))


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
