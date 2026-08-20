---
name: corpus-bot
description: 把 markdown 资料入库到本地 vault，自动构建结构化 wiki（含质检）。corpus-bot 是 LLM-decoupled CLI：所有 LLM 调用（extract / 评分）由 agent 端负责，corpus-bot 只做纯数据操作。
---

# corpus-bot Skill

## 何时用

用户提到以下场景时触发：

- 「把 X 入库到 wiki」「入库 markdown」「构建知识库」
- 「wiki 质量怎么样」「质检」「认证」
- 「查 X 相关内容」「搜 wiki」「找 concept」
- 「wiki 怎么优化」「删除概念」

## 核心架构

```
用户 ─对话─▶ Agent (LLM) ─Bash tool─▶ corpus-bot CLI ──▶ vault 目录 + .wiki-meta/corpus.db
            │                    │
            │                    └─ storage.py (纯 Python 函数，无 LLM)
            │
            └─ 🔥 自己用 OpenAI/Anthropic API 抽 concepts / 评分
                （不在 corpus-bot 进程里）
```

**关键**：corpus-bot 不调任何 LLM。LLM 调用全在 agent 端。

## Quick Start（agent 视角）

```bash
# 1. 初始化 vault（一次性）
corpus-bot vault init ~/my-wiki

# 2. 落源（content-hash dedup，所有 ingest 都加 `-ingest-<UTC compact ISO>` 后缀；软删复活用 --force-revive）
corpus-bot sources ingest ~/my-wiki ~/notes/postgresql.md
# 同 hash 已 soft-deleted? 加 --force-revive 复活同一 source_id
corpus-bot sources ingest --force-revive ~/my-wiki ~/notes/postgresql.md
# 返回：{"action":"staged","source_id":"d607...","raw_path":"...","size_bytes":187}

# 3. 批量落源
corpus-bot sources batch ~/my-wiki ~/notes/ --glob "*.md"

# 4. 列源
corpus-bot sources list ~/my-wiki --status staged

# 5. Agent 自己用 LLM 抽 concepts（OpenAI/Anthropic）
# （用你自己的 API key，不在 corpus-bot 里）

# 6. 写 concept（**必传 --extractions**：每个 source 一段 quote_span 原文证据）
corpus-bot concepts write ~/my-wiki \
    --slug postgres-mvcc \
    --title "PostgreSQL MVCC" \
    --body "..." \
    --extractions '[{"source_id":"d607...","quote_span":"Each row carries xmin/xmax..."}]' \
    --prompt-version extract-v1 \
    --links postgres-transactions,wal

# 7. 标记源完成
corpus-bot sources commit ~/my-wiki d607...

# 8. 查 concept
corpus-bot concepts show ~/my-wiki postgres-mvcc

# 9. 搜索
corpus-bot concepts search ~/my-wiki "MVCC"

# 10. 质检（agent 自己用 LLM 评分）
corpus-bot concepts uncertified ~/my-wiki
corpus-bot concepts certify ~/my-wiki postgres-mvcc --score 0.85 \
    --issues "缺源" --suggestions "补 WAL 段"

# 11. 看统计
corpus-bot stats ~/my-wiki
```

## 标准工作流（agent 编排）

### 模式 A：入库单文件

```
1. sources ingest vault file.md        → source_id
2. read source content (Read tool)      → markdown 文本
3. 🔥 自己用 LLM 抽 concepts
4. for each concept:
   - concepts find-by-link vault slug    → dedup 决策依据
   - concepts write / concepts update
5. sources commit vault <source_id>
```

### 模式 B：批量入库 + 整批质检

```
1. sources batch vault ~/notes/all/      → staged N 个
2. sources list vault --status staged     → source_id 列表
3. for each source_id:
   - concepts show ... 不需要，直接读 vault/raw/<source_id>.md
   - 🔥 LLM 抽 concepts
   - concepts find-by-link dedup
   - concepts write --extractions '[{"source_id":"...","quote_span":"..."}]' \
     --prompt-version extract-v1
   - sources commit
4. concepts uncertified vault            → 待检 list
5. for each uncertified:
   - 🔥 自己调 LLM 给 score / issues / suggestions
   - concepts certify
6. stats vault                           → 输出报告给用户
```

### 模式 C：用户查询

```
1. concepts search vault "用户问题关键词"  → 候选 list
2. for each candidate:
   - concepts show vault slug            → 全文 + 反向链接
3. 整合回答用户
```

### 模式 D：质检审计（按需）

```
1. concepts uncertified vault            → 待检 list
2. for each:
   - concepts show vault slug            → 看全文
   - 🔥 LLM 评估：
     - score: 0.0-1.0
     - issues: ["缺源 X", "正文偏短"]
     - suggestions: ["补充 source Y", "加 wikilink Z"]
   - concepts certify vault slug --score X --issues ... --suggestions ...
3. stats vault                           → 输出覆盖率
```

## dedup 决策启发式

`concepts find-by-link vault "<wikilink 文本>"` 返回候选列表：

- **0 个候选** → 新概念，写
- **1 个候选** → 看 slug 相似度 + title 相似度，agent 自己判断
  - 完全同一概念 → `concepts update` 加 source_ids + add_links
  - 不同概念（虽然 wikilink 文本撞了）→ 起新 slug，写
- **2+ 个候选** → 可能撞了之前的命名，agent 自己挑最像的，或起新 slug

## 错误处理

corpus-bot 的所有错误返回 exit code 1 + stderr：

```
error: <message>
  hint: <hint>
```

常见错误：
- `vault does not exist` → 先 `corpus-bot vault init`
- `duplicate content already staged as ...` → 该 source_id 已存在，跳过
- `duplicate content exists but is deleted: ...` → 同 hash 已 soft-deleted,加 `--force-revive` 复活
- `concept slug already exists` → 用 `concepts update` 而非 `write`
- `link cannot be self-reference` → concept 不能 wikilink 自己
- `link not slug-safe` → links 必须是合法 slug (小写字母数字+连字符)
- `extraction not found` → `concepts remove-extraction` 的 id 不存在
- `no fields to update` → `concepts certify` 至少传一个 `--score / --issues / --suggestions`
- `score is required for first-time certification` → 首次认证必传 `--score` (后续 partial update 可省)
- `score must be in [0, 1]` → 0-1 之间的数

sources.batch 的每个 result 也带 `hint` 字段 (deleted 行未带 `--force-revive` 时填)。

## JSON 输出

每个命令支持 `--json` 标志，agent 解析用：

```bash
corpus-bot sources list vault --json | python3 -c "
import json, sys
items = json.load(sys.stdin)
for it in items:
    print(it['source_id'], it['status'])
"
```



## 删 source 的工作流

```bash
# 1. 先 dry-run 看会 orphan 哪些 concept
corpus-bot sources delete <vault> <sid>

# 2. 看到 "will orphan: [...]" 评估是否真的删
# 3. 真的删：
corpus-bot sources delete <vault> <sid> --yes --reason version-update

# 结果：
# - sources.status = 'deleted'（软删，physical file 保留）
# - concept.source_ids 自动移除这个 sid
# - 如果 concept 因此失去所有 source → 自动 is_orphan=1
# - agent 后续可以：
#   - concepts list <vault> --orphans  # 看哪些 concept 变成孤儿
#   - concepts add-source <vault> <slug> --source-id <new-sid> --quote-span "..."  # 补充 source
```

## 为什么 write_concept 强制 quote_span

`extractions` 表是 source ↔ concept 的中间表，记录**抽取时刻的元数据 + 原文证据**：

- `quote_span`：原始 markdown 里支撑这个 concept 存在的**具体文字片段**
- `extracted_at` / `extracted_by` / `prompt_version`：审计与重新抽取的依据
- `source_content_hash`：防止 source 改后审计失效

不传 quote_span = concept 是"无源之水"——质量认证时无法判断证据、无法追溯历史、无法精准重抽。

## 不做的事（agent 自己负责）

- ❌ 不调 LLM（extract / compile / 评分全在 agent 端）
- ❌ 不做 dedup 语义判断（agent 看到 candidates 后判断）
- ❌ 不维护 in-memory 状态（每次调用都是独立 SQLite 操作）
- ❌ 不主动重试 / repair / judge（失败立即返回给 agent）

## CLI 速查

| 命令 | 作用 |
|---|---|
| `vault init <path>` | 创建 vault 目录 + 初始化 SQLite |
| `vault info <path>` | vault 路径表 + 元信息 |
| `vault stats <path>` / `stats <path>` | source/concept 统计 + 认证覆盖率 |
| `sources ingest <vault> <file>` | 单文件入库（content-hash dedup + `-ingest-<ts>` 后缀）|
| `sources ingest ... --force-revive` | 同 hash 已 soft-deleted → 复活该 source_id |
| `concepts write <vault> ...` | 写 concept (必传 --extractions, 每个 source 一段 quote_span) |
| `concepts update <vault> <slug> --body ... --add-extractions ...` | 增量更新 (改 title/body + 加 extraction/link) |
| `concepts delete <vault> <slug>` | 删 concept (默认 dry-run, --no-dry-run 真删, 同步清 wiki 文件) |
| `concepts list ... --orphans` / `--certified` / `--uncertified` | 过滤 (--certified 与 --uncertified 互斥) |
| `concepts remove-extraction <vault> <extraction_id>` | 细粒度撤一次抽取 (自动 sync concept.source_ids / is_orphan) |
| `concepts find-by-link ...` | 返回含 `match_score` 字段 (1.0 exact / 0.9 startswith / 0.5 contains / 0.4 title), 按 score DESC 排 |
| `concepts certify ... --score X --issues "..."` | 首次认证必传 `--score`; 后续 partial 可省, 传 "" 清空 list |
| `sources batch <vault> <dir> [--glob]` | 批量入库 |
| `sources list <vault> [--status]` | 列源 |
| `sources show <vault> <source_id>` | 看源元数据 |
| `sources commit <vault> <source_id>` | 标记 committed |
| `concepts write <vault> --slug X --title Y --body Z --source-ids ... --links ...` | 写 wiki |
| `concepts show <vault> <slug>` | 读 wiki |
| `concepts list <vault>` | 列 wiki |
| `concepts search <vault> <query>` | 搜索（stage 1 是 LIKE，stage 3 是 FTS5）|
| `concepts find-by-link <vault> <link>` | wikilink 解析 → 候选 concept |
| `concepts uncertified <vault>` | 待认证 list |
| `concepts certify <vault> <slug> --score X --issues ... --suggestions ...` | 标记已认证 |
| `concepts unmark <vault> <slug>` | 撤销认证 |
