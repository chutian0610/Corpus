"""corpus-bot CLI。

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
from .errors import CorpusBotError
from .ids import source_id_from_content
from .storage import (
    certification_stats,
    commit_source,
    find_concept_by_link,
    init_db,
    is_initialized,
    list_concepts,
    list_sources,
    list_uncertified_concepts,
    mark_certified,
    read_concept,
    read_source,
    search_concepts,
    stage_source,
    unmark_certified,
    update_concept,
    write_concept,
)
from .vault import ensure_vault, validate_source_path, vault_paths


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
    """解析 vault 路径，返回 db_path。"""
    if not vault_root.exists():
        _err(f"vault does not exist: {vault_root}", hint="run `corpus-bot vault init <path>` first")
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


# ---------- top-level group ----------

@click.group()
@click.version_option(version=__version__, prog_name="corpus-bot")
def cli() -> None:
    """corpus-bot: LLM-driven wiki builder (CLI-first, LLM-decoupled).

    \b
    Quick start:
      corpus-bot vault init ~/my-wiki
      corpus-bot sources ingest ~/my-wiki ~/notes/postgresql.md
      corpus-bot concepts write ~/my-wiki \\
        --slug postgres-mvcc --title "PostgreSQL MVCC" \\
        --body "..." --source-ids <sid> --links postgres
      corpus-bot stats ~/my-wiki

    LLM 调用（extract / compile / 评分）由 agent 自己做，corpus-bot 不装 LLM。
    """


# ---------- vault ----------

@cli.group()
def vault() -> None:
    """vault 生命周期：init / info / stats。"""


@vault.command(name="init")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="以 JSON 格式输出")
def vault_init(vault_path: Path, as_json: bool) -> None:
    """初始化 vault 目录结构（创建 raw/、wiki/、.wiki-meta/、corpus.db）。"""
    paths = ensure_vault(vault_path)
    if not is_initialized(paths["corpus_db"]):
        init_db(paths["corpus_db"])
    _emit(
        {
            "vault": str(vault_path),
            "raw": str(paths["raw"]),
            "wiki": str(paths["wiki"]),
            "wiki_concept": str(paths["wiki_concept"]),
            "wiki_index": str(paths["wiki_index"]),
            "meta": str(paths["meta"]),
            "corpus_db": str(paths["corpus_db"]),
            "schema_version": 1,
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
@click.option("--json", "as_json", is_flag=True)
def sources_ingest(vault_path: Path, source_file: Path, as_json: bool) -> None:
    """单文件入库：content-hash dedup + 撞名改名。

    源必须放在 <vault>/raw/ 下（Rule 7）。
    同 hash 已存在 → 抛 ConflictError（exit 1）。
    """
    paths = _resolve_db(vault_path)
    # 先校验源在 vault raw/ 内
    try:
        canonical = validate_source_path(vault_path, str(source_file))
    except Exception as e:
        _err(str(e))

    content = _read_file(canonical)
    raw_path = paths["raw"] / canonical.name

    # 如果文件名是 source_id 形式且同 hash 已存在 → 拒收
    # 否则 stage_source 会通过 content_hash dedup 处理
    try:
        result = stage_source(
            paths["corpus_db"],
            raw_path=raw_path,
            content=content,
            original_filename=canonical.name,
        )
        # 物理写入
        raw_path.write_text(content, encoding="utf-8")
        _emit(
            {
                "action": "staged",
                "source_id": result["source_id"],
                "raw_path": str(raw_path),
                "size_bytes": result["size_bytes"],
                "content_hash": result["content_hash"],
            },
            as_json=as_json,
        )
    except Exception as e:
        _err(str(e), hint=getattr(e, "hint", None))


@sources.command(name="batch")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--glob", "glob_pattern", default="*.md", help="glob 模式（默认 *.md）")
@click.option("--recursive/--no-recursive", default=True, help="是否递归子目录")
@click.option("--json", "as_json", is_flag=True)
def sources_batch(
    vault_path: Path, source_dir: Path, glob_pattern: str, recursive: bool, as_json: bool
) -> None:
    """批量入库：glob 匹配 + 逐个 stage。

    返回每文件的处理结果（success / duplicate / failed）。
    """
    from .errors import ValidationError
    paths = _resolve_db(vault_path)

    matches = list(source_dir.rglob(glob_pattern) if recursive else source_dir.glob(glob_pattern))
    if not matches:
        _emit({"total": 0, "staged": 0, "duplicates": 0, "failed": 0, "results": []}, as_json=as_json)
        return

    results = []
    n_staged = n_dup = n_fail = 0
    for src in matches:
        try:
            content = _read_file(src)
            sid = source_id_from_content(content)
            # 撞名检测：raw/ 下是否已有同名的同 sid？
            target = paths["raw"] / src.name
            if target.exists() and source_id_from_content(_read_file(target)) != sid:
                # 撞名不同内容 → 改名
                from .ids import rename_suffix
                stem = src.stem
                suffix = src.suffix
                target = paths["raw"] / f"{stem}_{rename_suffix()}{suffix}"

            existing = read_source(paths["corpus_db"], sid)
            if existing:
                results.append({"source": str(src), "action": "duplicate", "existing_id": existing["source_id"]})
                n_dup += 1
                continue

            result = stage_source(
                paths["corpus_db"],
                raw_path=target,
                content=content,
                original_filename=src.name,
            )
            target.write_text(content, encoding="utf-8")
            results.append({"source": str(src), "action": "staged", "source_id": result["source_id"]})
            n_staged += 1
        except ValidationError as e:
            results.append({"source": str(src), "action": "failed", "rule": e.rule, "message": e.message})
            n_fail += 1
        except CorpusBotError as e:
            results.append({"source": str(src), "action": "failed", "message": str(e)})
            n_fail += 1

    _emit(
        {
            "total": len(matches),
            "staged": n_staged,
            "duplicates": n_dup,
            "failed": n_fail,
            "results": results,
        },
        as_json=as_json,
    )


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


# ---------- concepts ----------

# 顶层 stats alias（agent 常用）
cli.add_command(vault_stats, name="stats")

@cli.group()
def concepts() -> None:
    """Wiki concept 管理：write / show / list / search / find-by-link / update / 认证。"""


@concepts.command(name="write")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--slug", required=True, help="filesystem-safe slug")
@click.option("--title", required=True)
@click.option("--body", required=True, help="wiki body (markdown)")
@click.option("--source-ids", "source_ids", default="", help="逗号分隔的 source_id 列表")
@click.option("--links", "links", default="", help="逗号分隔的 wikilink slug 列表")
@click.option("--json", "as_json", is_flag=True)
def concepts_write(
    vault_path: Path, slug: str, title: str, body: str, source_ids: str, links: str, as_json: bool
) -> None:
    """写一篇 wiki concept。slug 已存在 → ConflictError。

    调用方（agent）自己用 LLM 生成 title / body / links。
    """
    paths = _resolve_db(vault_path)
    sid_list = [s.strip() for s in source_ids.split(",") if s.strip()]
    link_list = [s.strip() for s in links.split(",") if s.strip()]

    try:
        result = write_concept(
            paths["corpus_db"],
            slug=slug, title=title, body=body,
            source_ids=sid_list, links=link_list,
        )
        # 物理写文件
        wiki_path = paths["wiki_concept"] / f"{slug}.md"
        frontmatter = f"---\nslug: {slug}\ntitle: {title}\n---\n\n"
        wiki_path.write_text(frontmatter + body, encoding="utf-8")
        result["wiki_path"] = str(wiki_path)
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
@click.option("--limit", type=int, default=50)
@click.option("--offset", type=int, default=0)
@click.option("--json", "as_json", is_flag=True)
def concepts_list(vault_path: Path, limit: int, offset: int, as_json: bool) -> None:
    paths = _resolve_db(vault_path)
    items = list_concepts(paths["corpus_db"], limit=limit, offset=offset)
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
    """解析 wikilink → candidate concept list（dedup 用）。"""
    paths = _resolve_db(vault_path)
    items = find_concept_by_link(paths["corpus_db"], link)
    _emit(items, as_json=as_json)


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
@click.option("--score", type=float, required=True, help="0.0-1.0")
@click.option("--issues", default="", help="逗号分隔的问题列表")
@click.option("--suggestions", default="", help="逗号分隔的改进建议")
@click.option("--by", "certified_by", default="agent")
@click.option("--json", "as_json", is_flag=True)
def concepts_certify(
    vault_path: Path, slug: str, score: float, issues: str, suggestions: str,
    certified_by: str, as_json: bool,
) -> None:
    paths = _resolve_db(vault_path)
    issue_list = [s.strip() for s in issues.split(",") if s.strip()]
    sugg_list = [s.strip() for s in suggestions.split(",") if s.strip()]
    try:
        result = mark_certified(
            paths["corpus_db"],
            slug=slug, score=score, issues=issue_list,
            suggestions=sugg_list, certified_by=certified_by,
        )
    except Exception as e:
        _err(str(e))
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
