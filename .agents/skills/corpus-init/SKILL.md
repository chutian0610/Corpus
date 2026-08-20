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

If it fails, install via `uv tool install -e .` from the project root (or `./scripts/install.sh`).

## Step 1 — Pick the vault path

| Scenario | Suggested path |
|---|---|
| Per-project vault (in a git repo) | `<project>/.corpus/<project-name>/` |
| Personal scratch vault | `~/corpus/<name>/` |
| Shared vault across projects | `~/shared-wiki/` or similar (outside any repo) |

If the vault lives **inside a git repo**, ensure `.corpus/` is in `.gitignore` (default `corpus` repo has this).

## Step 2 — Initialize

```bash
corpus vault init <vault_path> --json
```

Idempotent. Running on an existing vault returns schema info without errors. The vault root is created if missing (`mkdir -p` semantics).

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
  "schema_version": 2
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

- **Ingest sources**: `corpus sources ingest <vault> <external-file>` (see main `corpus` skill)
- **Batch ingest**: `corpus sources batch <vault> <dir> --glob "*.md"`
- **Read raw paths**: `corpus vault info <vault> --json` shows the canonical paths
- **Stats baseline**: `corpus stats <vault> --json` (will show `total_sources: 0` initially)
