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
  "schema_version": 2,
  "git": {"git_initialized": true, "git_path": "<path>/.git",
           "commit": {"committed": true, "commit_sha": "abc...", "commit_message": "chore: init corpus vault"}}
}
```

## Step 3 — Verify

```bash
corpus vault info <vault_path> --json
```

Expect:
- `db_initialized: true`
- `schema_version: 2` (auto-migrated from v1 if older DB present)
- `has_sources: false` (initially)
- `has_concepts: false` (initially)

## Vault layout

```
<vault_path>/
├── raw/                   # ingest 产物 (自动 rename <stem>-ingest-<UTC compact ISO>.<ext>)
├── wiki/
│   ├── concept/           # corpus concepts write 生成的 wiki 页 (<slug>.md)
│   └── index/             # 自动 export_index: concepts.json + sources.json
└── .wiki-meta/
    └── corpus.db          # SQLite (sources / concepts / extractions / links /
                           # cooccurrence / certification_log)
                           # ⚠️ 不要入 git (含本地 SQLite, 已在 .gitignore)
```

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Init in vault that already has data | (no-op, idempotent — but doesn't reset) | `corpus concepts delete` / `sources delete` for cleanup |
| Trying `sources ingest` with file inside vault | `path is inside vault raw/` | Use external path; `raw/` is ingest product dir |
| `.wiki-meta/` checked into git | repo size bloat, conflicts on local SQLite | Add `.wiki-meta/` to `.gitignore` |
| Old install conflicts (`corpus-bot` + `corpus` packages) | `which corpus` → `corpus-bot` binary still installed | `uv tool uninstall corpus-bot && uv tool install -e . --force` |
| Schema version mismatch | `db_initialized: true` but `schema_version: 1` | Re-run `corpus vault init <path>` (auto-migrates v1→v2) |

## Next steps

After `vault init` succeeds:

- **Ingest sources**: ```bash
corpus sources ingest <vault_name> <external-file>      # see main corpus skill
corpus sources batch <vault_name> <dir> --glob "*.md"
corpus vault info <vault_name> --json                    # 看 canonical paths
corpus stats <vault_name> --json                        # baseline (total_sources: 0)
```
