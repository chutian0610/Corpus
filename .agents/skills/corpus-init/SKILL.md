---
name: corpus-init
description: >
  Initialize a corpus vault — verify the `corpus` CLI is installed, create the
  vault directory layout, and confirm the local SQLite metadata is at schema
  version 2. Use this skill when the user says "init corpus", "create a new
  vault", "set up corpus", "start using corpus for X", "vault init", or the
  first time invoking corpus on a fresh project. Also triggers when
  `corpus --version` fails, when `corpus vault init` reports a missing
  root, or when the user wants to set up corpus-bot / corpus for a new
  wiki project. Does NOT ingest sources — use the `corpus` skill (or
  `corpus-ingest` once split out) for that.
---

# corpus-init — Vault Initialization

## When this skill applies

- "把 corpus 用到 X 项目"
- "建一个 vault"
- "corpus vault init" / "vault init" / "init corpus"
- "corpus 命令找不到" / `corpus --version` 失败
- 任何想用 corpus 但 vault 不存在的场景

## Pre-flight

Before initializing, confirm `corpus` is on PATH:

```bash
corpus --version
# expect: corpus, version 0.2.0
```

If it fails, install via `pip install corpus` (从 PyPI 装, 不在项目根).

**git 命令检查** (强制):

```bash
git --version
# expect: git version 2.x+
```

如果 git 不可用, 告诉用户: "corpus vault init 需要 git 装在 PATH 里 (macOS: brew install git; Linux: apt install git / yum install git; Windows: https://git-scm.com/download/win)". 跟 corpus 命令本身无关, 是 vault 强制的设计选择.

## Step 1 — Ask user for vault name

**不要预设名字, 必须主动问用户**. 例:

> "请提供新 vault 的名字 (slug-safe, 例 my-project / postgres-notes / team-wiki)"

vault 名建议:
- 小写字母 + 数字 + 连字符 (slug-safe, 例 `team-wiki-2026`)
- 不与 cwd 下已有目录冲突
- 不含路径分隔符 (agent 会自动把 name 当成 cwd 下的子目录名)

如果 vault 落在 git 仓库里, 提示用户把 `<vault_name>/` 加到 `.gitignore` (这是项目层决定, agent 不假设).

## Step 2 — Initialize (cwd 下创建 <vault_name>/, 默认 git init)

```bash
corpus vault init <vault_name> --json
# 实际例子: corpus vault init my-project --json
```

Idempotent. Running on an existing vault returns schema info without errors. The vault root is created if missing (`mkdir -p` semantics).

**默认同时跑 `git init --initial-branch=main` + initial commit**:
- 写 vault 根 `.gitignore` 排除 `*.db` / `.wiki-meta/corpus.db*` (SQLite 运行时数据不入 git)
- 给 `raw/` / `wiki/concept/` / `wiki/index/` 加 `.gitkeep` 占位 (git 不 track 空目录)
- `git -C <vault> config user.email/user.name` (local only, 设为 `corpus@localhost` / `corpus`)
- `git add -A && git commit -m 'chore: init corpus vault'`

**强制行为, 无法 opt-out**: 不再有 `--no-git` / `--no-git-commit` flag, git 永远是 default. (要 skip 只能不安装 git.)

返回 JSON `git.commit.commit_sha` 字段有 commit SHA (40-hex), 可用于后续 `git reset` 等.

Returns JSON:

```json
{
  "vault": "<path>",
  "raw": "<path>/raw",
  "wiki": "<path>/wiki",
  "wiki_concept": "<path>/wiki/concept",
  "wiki_index": "<path>/wiki/index",
  "meta": "<path>/.wiki-meta",
  "corpus_db": "<path>/.wiki-meta/corpus.db",
  "schema_version": <int>,                  // 当前 storage.SCHEMA_VERSION — 不要 hardcode 数字
  "git": {"git_initialized": true, "git_path": "<path>/.git",
           "commit": {"committed": true, "commit_sha": "abc...", "commit_message": "chore: init corpus vault"}}
}
```

## Step 3 — Verify

```bash
corpus vault inspect <vault_path> --json
```

Expect:
- `db_initialized: true`
- `has_sources: false` (initially)
- `has_concepts: false` (initially)

> **不要 hardcode schema_version**。vault_info JSON 里**根本没有** `schema_version` 字段
> (`db_initialized / has_sources / has_concepts` 才存在). 真要查 schema 版本:
>
> ```bash
> # CLI 侧 (init 返回的 schema_version)
> corpus vault init <vault> --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["schema_version"])'
>
> # 落库侧 (schema_meta 表)
> sqlite3 <vault>/.wiki-meta/corpus.db 'SELECT value FROM schema_meta WHERE key="schema_version"'
> ```
>
> 两个输出一致说明 init 完整跑完; 不一致说明 migration 中途, 看 `corpus --version` 对齐后再排查.

## Vault layout

```
<vault_path>/
├── .gitignore             # git igore 文件
├── raw/                   # ingest 产物
├── wiki/
│   ├── concept/           # corpus concepts write 生成的 wiki 页 (<slug>.md)
│   └── index/             # opt-in: `corpus index snapshot <vault>` 手动生成 snapshot (默认不入 git)
└── .wiki-meta/
    └── corpus.db          # SQLite
```

## Next steps

After `vault init` succeeds, **不要再走 `corpus` CLI 命令路线** — 该让对应 workflow skill 接管:

| 接下来要做什么 | Load this skill |
|---|---|
| 把已有 markdown / 文章 ingest 进 vault, LLM 抽 concept, dedup, write | **`corpus-ingest`** (本仓库 `.agents/skills/corpus-ingest/SKILL.md`) |
| 只查 schema / 列已存 concept, 不修改 | 直接 `corpus concepts list / show` (command-level, 不需 skill) |
| 调整 vault config (auto_git / auto_commit 等, schema v6+) | **`corpus-config`** (未来 skill, AGENTS.md 已预定) |
| 健康检查 / orphan / 重复 / staleness | **`corpus-maintain`** (未来 skill, AGENTS.md 已预定) |

> **为什么不再列 `corpus sources ingest ...` 之类的命令了**: ingest 不是一个命令
> 就能做完的 — 它涉及 `corpus sources ingest` + LLM 抽 concept + dedup + write/update +
> index sync + 双向同步 source page. 完整工作流写在 `corpus-ingest` 里, agent 应该
> load 那个 skill 而不是在 init skill 里复制一遍命令. 命令速查只在 main `corpus`
> skill 里维护 (`## CLI 速查` 段), 哪里是新事实的唯一来源 (init / ingest 都引那里).
