---
name: corpus-ingest
description: >
  完整 ingest 工作流: source markdown → vault ingest → LLM 抽 concept → 
  dedup (concepts find match_score) → concepts write/update (with CAS).
  index snapshot 不再自动触发 (opt-in), 想给外部 web UI / dashboard 喂 snapshot
  用 `corpus index snapshot <vault>` 手动跑.
  Use this skill when the user has source content (markdown files, articles, notes, 
  design docs) to ingest into a corpus vault. 触发词: "入库 X", "抽 concept", 
  "build knowledge base", "把 N 个 markdown 入库", "process sources", 
  "ingest directory", "把 corpus 填满", "跑 ingest 流程", "把这篇文章入库".
  
  Covers the full pipeline:
    1. Pre-flight: vault 已 init (corpus-init skill), git 在 PATH
    2. `corpus sources add <vault> <path>` (file/dir auto-detect; raw_path 自动 <stem>-ingest-<UTC>.<ext>,
       同时写 wiki/source/<slug>.md; Stage 2 替代老的 sources ingest + sources batch)
    3. 对每个 source_id, Read raw/<file> 抽取概念
    4. corpus concepts find --by-link 查 dedup (match_score >= 0.9 → 已存在, 也匹配 aliases)
    5a. corpus concepts write (新, --status/--aliases/--tags 可选) — slug 撞抛 ConflictError
    5b. corpus concepts update (已有, --expected-version CAS 防 race) — 含 body / source_ids / status 合并
    5c. `corpus concepts link <slug> --source SID --quote-span "..."` (fold of 老的 add-source; 默认 INSERT 新 extractions row)
    5d. `corpus concepts unlink <slug> --source SID` (粗粒度: 删该 source 全部 extractions + source_ids 减)
       `corpus concepts unlink <slug> --extraction-id X` (细粒度: 删单条 extractions)
    6. (opt-in) `corpus index snapshot <vault>` (默认 vault.git 看不到这两个 JSON; 想给外部 web UI / dashboard 喂 snapshot 手动跑)
  
  Not for: vault setup (→ corpus-init), config (→ corpus-config), audit log (→ corpus skill), 
  maintenance (→ corpus-maintain, 未来). 操作审计查询 → 主 corpus skill 的 `corpus history`. Multi-agent 并发: write_concept 严格 INSERT (slug 撞抛 ConflictError), 
  update_concept 支持 --expected-version CAS (race-safe).
---

# corpus-ingest — 完整入库工作流

## When this skill applies

用户提到以下任一就触发:
- "把 X markdown / 文章 / 设计文档入库"
- "抽 concept" / "建 wiki" / "build knowledge base"
- "跑 ingest 流程" / "处理这些 sources"
- "把 corpus 填满"
- agent 自己决定"我要给 corpus 加新概念"时也走这个 skill

不适用:
- 第一次建 vault → `corpus-init` skill
- 改 corpus 配置 (是否 git / auto commit 等) → `corpus-config` skill (未来)
- 操作审计查询 → 主 `corpus` skill 的 `corpus history`
- 删除 concept / 清理 orphan → 主 `corpus` skill 的 `corpus delete` 等


## 竞品参考 (body 结构参考)

corpus 不强制 concept body 模板, 但 agent 抽 concept 时可以参考这几家公认模式
择合适的写:

| 体系 | 一概念一文件 | 链接形态 | body 结构 | 何时适用 |
|---|---|---|---|---|
| **Andy Matuschak evergreen notes** / **Zettelkasten** | yes | wikilink in body | 极强 prose, dense links, 一段到位 | 抽象概念 / 跨笔记互引 |
| **Wikipedia lead** | yes | 内链 inline | lead 段定义 → 结构 → 字段说明 | 系统对象 / 文件 / 命令 |
| **Cheatsheet card** | yes | inline 或独立 links | 三段 — 定义 / 命令 + 选项表 / 示例 | 命令 / API 参考 |
| **Dendron schema** | yes + 强制 schema | frontmatter 主导 | 严格 YAML 模板 | corpus **不取这个** — frontmatter 只存元数据 |
| **Foam / Obsidian** | yes | wikilink in body | 自由 | 默认参考 (corpus 设计最贴近) |

观察:
- 唯一共识 = wikilink 写在 body (frontmatter links 都是冗余, corpus 与 Obsidian 都不显示)
- 模板差异 = 对 "原子" 的理解差异 (一概念 = 一个声明 / 一个系统对象 / 一组命令)
- corpus 输出 markdown + 单源真 raw/ + 表格化 Concept 引出段, 这套与 Obsidian v
  Vault 完全兼容 — 直接用 Obsidian 打开 wiki/ 就能看到 backlinks panel, 不需要额外桥接


## Pre-flight

```bash
# 1. corpus 可用
corpus --version    # corpus, version 0.2.0
# 失败 → 提示 pip install corpus (PyPI, 不再提本地装)

# 2. git 可用 (vault 强制 git init)
git --version       # git version 2.x+
# 失败 → 提示装 git (brew/apt/yum/git-scm.com)

# 3. vault 已建
corpus vault inspect <vault> --json
# 失败 → 提示用 corpus-init skill
```

## Step 1 — Sources add (vault 外 → raw/)

```bash
# 单文件 — path 是文件
corpus sources add <vault> /path/to/note.md --json
# 返回: {"action": "staged", "source_id": "abc123", "raw_path": ".../note-ingest-...md", ...}

# 批量 — path 是目录, auto-detect
corpus sources add <vault> /path/to/notes/ --glob "*.md" --json
# 返回: {"total": N, "staged": M, "revived": R, "duplicates": K, "failed": 0, "results": [...]}
```

`sources add <vault> <path>` 一并替代了 `sources ingest` / `sources batch` (本 release 删除 alias).

重要: source path **必须在 vault 外**. vault 内的文件 (含 raw/) 不能 ingest (避免重复).

## Step 2 — 读 source, 抽 concept (LLM 自己做)

对每个新 source_id, agent 读 raw/<file>-ingest-<ts>.<ext>, 用自己的 LLM 抽 concept:

```python
# agent 工作流 (伪代码, agent 实际用 Read 工具读文件)
content = read_file(raw_path)  # Read tool
extracted = llm_call(
    prompt="""从以下 markdown 抽取结构化 concept 列表.
    对每个 concept 必返回:
      - slug: filesystem-safe (例 'postgresql-mvcc')
      - title: 简短概念名
      - body: 包含 '## 定义', '## 不变量', '## 证据' 三段
      - extractions: [{source_id, quote_span}]  ← 从原文逐字复制
      - links: [其他 concept slug 列表, 形成知识图谱]

    不要凭模型训练背景加东西, 所有内容都必须在原文中找得到.
    quote_span 必填, 30-200 字.""",
    input=content,
    response_format="json",
)
```

LLM 返回结构:
```json
{
  "concepts": [
    {
      "slug": "postgresql-mvcc",
      "title": "PostgreSQL MVCC",
      "body": "## 定义\n...\n## 不变量\n...\n## 证据\n...",
      "extractions": [{"source_id": "abc123", "quote_span": "Each row carries xmin/xmax..."}],
      "links": ["wal", "transaction-isolation"]
    }
  ]
}
```

## Step 2.5 — 写 concept 时推荐走临时文件 (LLM shell 转义坑)

LLM 抽概念时, body 多含多行 markdown + shell-special chars(`$1`, `&`, `|`, `;`, 反引号,
单/双引号), extractions 是 inline JSON 又长又容易把 `'` 嵌错嵌套. 走 inline CLI 参数会
陷入 zsh / bash 引号地狱, 失败模式多:

```bash
# 反例: body 内有反引号 / $VAR, 在 --body '...' 里要复杂转义
corpus concepts write <vault> \
    --slug mvcc --title "MVCC" \
    --body '`xmin` 标识可见性; `$$` 在 bash 里是 PID (小心); \
            `${HOME}` 在单引号里不展开; \
            `&&` 在 markdown 里没事但 bash 会逻辑短路' \
    --extractions '[{"source_id":"abc","quote_span":"..."}]' \
# ↑ 引号一错 zsh 直接 `parse error near '&&'`, 写了一长串调试转义性价比极低
```

**推荐**: LLM 先用 `Write` 工具把 body 写到 `.tmp/<slug>.md` / `.tmp/<slug>-extr.json`,
然后 CLI 走 `--body-file` / `--extractions-file`:

```bash
corpus concepts write <vault> \
    --slug mvcc --title "PostgreSQL MVCC" \
    --body-file .tmp/mvcc.md \                       # 多行 markdown 原样写
    --extractions-file .tmp/mvcc-extr.json \         # JSON 文件原样读
    --prompt-version extract-v1 \
    --status draft
# 上限 1 MiB / file. 不存在 → click.Path exit 2. 与 inline 互斥 (不能同时传两个).
```

| 标志 | 互斥 | 适用 |
|---|---|---|
| `--body` / `--body-file` | yes | update 也支持 `--body-file` |
| `--extractions` / `--extractions-file` | yes | write required |
| `--add-extractions` / `--add-extractions-file` (update) | yes | update required-none |

**为什么 CLI 不吞 stdin**: corpus 走 click (不是 stdin-JSON 设计), 走文件是最干净的.
Temp 路径 `.tmp/` 在 vault `.gitignore` 顶层 (.agents/skills/corpus-init/SKILL.md 有),
不会提交到 vault git.


## Step 3 — Dedup 检查 (concepts find / dedup-candidates)

**concepts find** (单 slug 查询, rename 自 find-by-link):
```bash
corpus concepts find --by-link <vault> <candidate-slug> --json
```

返回 match_score 排序的候选 (slug + title + match_score):
- `score >= 0.9`: 高度相似, 大概率是同一 concept → 走 `concepts update` 路径
- `score 0.4-0.9`: 部分相关, 仔细看 candidate 决定 merge 还是新写
- `score 0` (空数组): 没相关, 走 `concepts write` 路径

**5 维匹配** (schema v5+):
| 维度 | 分值 |
|---|---|
| slug exact match | 1.0 |
| alias exact match (schema v5) | 0.95 |
| slug startswith | 0.9 |
| alias partial match | 0.6 |
| slug contains (substring) | 0.5 |
| title contains (case-insensitive) | 0.4 |
| difflib.SequenceMatcher (fuzzy) | 0-0.3 bonus |

Aliases 让 'MVCC' / '多版本并发' / 'PG' 都能映射到 postgresql-mvcc.

**dedup-candidates** (多维度分数, 给 LLM 二次判断):
```bash
corpus concepts dedup-candidates <vault> <slug> [--limit N]
```

返每个 candidate 的 discrete / fuzzy / length_diff 分数, 让 LLM 看 'score=0.7' 怎么来的 (discrete 0.4 + fuzzy 0.3 vs discrete 0.9 + fuzzy 0.0 含义不同) 决定是否 merge.

```python
result = json.loads(subprocess.run(["corpus", "concepts", "find", vault, "--by-link", slug, "--json"], ...).stdout)
if not result:
    # 全新, 走 write
    action = "write"
elif result[0]["match_score"] >= 0.9:
    # 高度相似, 走 update (加新 source, 不丢旧)
    existing_slug = result[0]["slug"]
    action = f"update:{existing_slug}"
```

## Step 4 — Write / Update concept (write_concept 严格 INSERT, update_concept CAS)

**5a. 新 concept (write_concept, 严格 INSERT)** (schema v5):

```bash
corpus concepts write <vault> \
  --slug postgresql-mvcc \
  --title "PostgreSQL MVCC" \
  --body "<按内容自选的 prose, body 里含 [[wal]] [[transaction-isolation]] 表达 outgoing links>" \
  --extractions '[{"source_id":"abc123","quote_span":"..."}]' \
  --prompt-version extract-v1 \
  --status evergreen \
  --aliases "MVCC,多版本并发" \
  --tags "concept,database" \
  --json
```

> outgoing links 不再传 `--links` — corpus 从 body 里的 `[[wikilinks]]` 自动派生 (Obsidian
> 兼容). 想添链接 → 在 body 里写 `[[target-slug]]`. 详见 corpus SKILL 错误处理段.

slug 已存在 → 抛 `ConflictError`. **业务决策 (merge body / 保留哪些 source) 不应 storage 静默做, 由 LLM 决定**.
LLM 重新走 dedup 流程: find-by-link + read + merge + update_concept (5b).

**5b. 已有 concept (update_concept with CAS)** (schema v3+): 防 multi-agent 覆盖丢失.
```bash
# 1. 读当前 version
v=$(corpus concepts show <vault> postgresql-mvcc --json | jq .version)

# 2. agent 自己做 merge (current body + LLM 新内容)

# 3. 提交 with CAS (覆盖 body / source_ids / status, 不动其他字段)
corpus concepts update <vault> postgresql-mvcc \
  --body "<merged 内含 [[related-concept]] 自动派生 outgoing link>" \
  --add-extractions '[{"source_id":"new_sid","quote_span":"..."}]' \
  --status stale \
  --expected-version $v \
  --json
# 失败 → OptimisticLockError, 提示 'read_concept again, merge, then update_concept with new expected_version'
# → 回到 1 重新 read + merge
```

**5c. 链接 source → concept (concepts link)**:
```bash
# 默认 INSERT 新 extractions row (允许多 evidence per (concept, source))
corpus concepts link <vault> postgresql-mvcc \
  --source <new_sid> \
  --quote-span "xmin/xmax from PostgreSQL docs" \
  --prompt-version extract-v1 \
  --json

# 若已有同一 (concept, source) 的 extractions row, 传 --extraction-id 复用 + UPDATE quote_span
corpus concepts link <vault> postgresql-mvcc \
  --source <new_sid> \
  --quote-span "再次出现的 xmin/xmax 引用" \
  --extraction-id e_existing_xxxxxxxxxxxx \
  --json
# 自动: source_ids set union, is_orphan=0
# source page (wiki/source/<slug>.md) "## Concepts extracted" 段自动反查更新
```

**5d. 解链 (concepts unlink)**:
- `--source SID` 粗粒度: 删该 (concept, source) 全部 extractions + source_ids 减 + is_orphan 自动
- `--extraction-id X` 细粒度: 删单条 extractions row + sync source_ids

**multi-agent 并发同 slug race** (schema v5): write_concept 严格 INSERT 不静默 merge, 第二个等锁后 INSERT 撞 UNIQUE → ConflictError. LLM 重新走 find-by-link + read + merge + update_concept (--expected-version). 这是 read-modify-write 模式的典型应用, 业务决策归属 LLM.

## Step 5 — Index sync (自动)

**没有自动 index snapshot 了.** `concepts write` / `update` / `delete` 不再自动写 `wiki/index/{concepts,sources}.json` (没人读, opt-in). 想要 snapshot (e.g. 给 web UI 喂数据) 手动跑:
default `.gitignore` 含 `wiki/index/*.json`. 想要 snapshot (e.g. 给 web UI 喂数据) 手动跑:

```bash
corpus index snapshot <vault> --json
```

## Multi-agent 并发要点 (schema v5)

- **不同 source + 同 concept**: write_concept 严格 INSERT, slug 撞 UNIQUE → ConflictError
  → LLM 自己读 + merge + update_concept (--expected-version CAS). 业务决策在 LLM, storage 不静默 merge
- **同 concept update race**: 用 --expected-version CAS, OptimisticLockError 时重读重做
- **多 agent 并行 ingest 不同 source**: SQLite WAL + busy_timeout=30s 自动串行化
  raw_path unique (pick_raw_target 加 -ingest-<UTC> 后缀), 不撞
- **vault 跨进程互斥**: SQLite 自己处理 (busy_timeout 等), 不需要 flock (Phase 1.5 已移除过度防御)

## Source page 同步 (双向)

`corpus sources add <vault> <path>` 自动写 `wiki/source/<slug>.md` (obsidian 兼容).
slug = `slugify(canonical.stem)` (canonical 是 ingest 时的源文件路径). 重名时 pick_source_page_target 加 `-<short-hash>` 后缀.

`wiki/source/<slug>.md` frontmatter 包含:
- `source_id` (16 hex, 稳定 identifier)
- `slug` (易读别名, 跨 vault wikilink 引用)
- `content_hash` / `size_bytes` / `status` / `created_at`  (raw/<file>.md 的 basename 即原始名, schema v6 起了无 original_filename 列)

`corpus concepts link / unlink` 反向触发 `update_source_page_concepts`:
重写 source page 的 `## Concepts extracted from this source` 段 (从 DB extractions 表反查).

## Vault 完整 self-contained (跨电脑恢复)

```bash
# 换电脑 / DB 损坏时:
git clone <vault-repo> ~/my-vault
cd ~/my-vault
corpus rebuild .                       # 从 raw/ + wiki/concept/ + wiki/source/ 重建整个 DB
```

`corpus rebuild` 读所有 markdown frontmatter (含 source_id / content_hash / status / aliases / tags) 重建 sources / concepts / extractions / links 表. wiki/concept/<slug>.md (含 frontmatter sources / links / certified) 也恢复.

vault 完全 self-contained: `git` 是 source of truth, `.wiki-meta/corpus.db` 是 query cache, restore 重建.

## 完整例子: batch ingest 一个目录

```bash
# 1. ingest 整个目录 (path 自动 detect file/dir)
corpus sources add ~/my-wiki /path/to/articles/ --glob "*.md" --json
# → 8 sources staged (results[] 含 per-file outcome: staged / duplicate / revived / failed)

# 2. 列 source_ids
for sid in $(corpus sources list ~/my-wiki --json | jq -r '.[].source_id'); do
  # 3. 找 raw_path, 读文件
  raw=$(corpus sources show ~/my-wiki $sid --json | jq -r .raw_path)
  content=$(cat "$raw")

  # 4. LLM 抽取 (agent 自己用 Read 工具读 + 调 LLM)
  concepts=$(llm_extract "$content")  # 假设 LLM 调用返回 JSON

  # 5. 对每个 concept 写
  for c in $(echo "$concepts" | jq -c '.concepts[]'); do
    slug=$(echo "$c" | jq -r .slug)
    
    # dedup
    existing=$(corpus concepts find ~/my-wiki --by-link $slug --json | jq -r '.[0].slug // empty')
    if [ -n "$existing" ]; then
      v=$(corpus concepts show ~/my-wiki $existing --json | jq -r .version)
      corpus concepts update ~/my-wiki $existing \
        --body "$(echo "$c" | jq -r .body)" \
        --add-extractions "$(echo "$c" | jq -c .extractions)" \
        --expected-version $v --json
    else
      corpus concepts write ~/my-wiki \
        --slug $slug --title "$(echo "$c" | jq -r .title)" --body "$(echo "$c" | jq -r .body)" \
        --extractions "$(echo "$c" | jq -c .extractions)" \
        --json
    fi
  done
done

# 6. 验收
corpus stats ~/my-wiki --json
corpus history ~/my-wiki --op stage --limit 20
```

## Next steps

Ingest 跑完之后, **不要再列 `corpus` 命令让 agent 自己跑** — 该让对应 workflow skill 接管:

| 接下来要做什么 | Load this skill |
|---|---|
| 查 schema / list 已存 concept, 不改 | 直接 `corpus concepts list / show` (command-level) |
| 健康检查 / orphan / 重复 / staleness / 修 concept | **`corpus-maintain`** (未来 skill, AGENTS.md 已预定) |
| 认证 / 评分 (certify) | 主 `corpus` skill (`## CLI 速查` 段) |
| 调 vault config (auto_git / auto_commit 等) | **`corpus-config`** (未来 skill, AGENTS.md 已预定) |
| 跨电脑 restore (`corpus rebuild`) | 主 `corpus` skill (`## CLI 速查` 段) |
| 翻 ingest_log (history) | `corpus history` (command-level) |

> 命令速查只在主 `corpus` skill 维护一份 — ingest / init / maintain 都引那边,
> 别在每个子 skill 里复制一遍命令表. 这是 single source of truth 原则.


## Out of scope

- vault 初始化 → `corpus-init` skill
- corpus 配置 (auto_git / auto_commit 等, future) → `corpus-config` skill (未来)
- 操作审计查询 → 主 `corpus` skill 的 `corpus history`
- 维护 (delete concept / 修 orphan / staleness) → `corpus-maintain` skill (未来)
- 认证 / 评分 (certify) → 主 `corpus` skill (在 source 全部 ingest 完后, agent 自己做评分, 不在 ingest 工作流)
