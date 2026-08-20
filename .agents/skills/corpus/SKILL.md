---
name: corpus
description: 把 markdown 资料入库到本地 vault，自动构建结构化 wiki（含质检）。corpus 是 LLM-decoupled CLI：所有 LLM 调用（extract / 评分）由 agent 端负责，corpus 只做纯数据操作。
---

# corpus Skill

## 何时用

用户提到以下场景时触发：

- 「把 X 入库到 wiki」「入库 markdown」「构建知识库」
- 「wiki 质量怎么样」「质检」「认证」
- 「查 X 相关内容」「搜 wiki」「找 concept」
- 「wiki 怎么优化」「删除概念」

## 核心架构

```
用户 ─对话─▶ Agent (LLM) ─Bash tool─▶ corpus CLI ──▶ vault 目录 + .wiki-meta/corpus.db
            │                    │
            │                    └─ storage.py (纯 Python 函数，无 LLM)
            │
            └─ 🔥 自己用 OpenAI/Anthropic API 抽 concepts / 评分
                （不在 corpus 进程里）
```

**关键**：corpus 不调任何 LLM。LLM 调用全在 agent 端。

## Quick Start（agent 视角）

按场景路由到子 skill:

| 想做什么 | 用哪个 skill / 命令 |
|---|---|
| 建 vault（第一次 / 新项目） | **`corpus-init`** skill (`.agents/skills/corpus-init/SKILL.md`) |
| 把 markdown / 文章入库到 vault | **`corpus-ingest`** skill (`.agents/skills/corpus-ingest/SKILL.md`) |
| 查 / 搜 / 读 / 删 concept | 用本 skill 的 CLI 速查, 直接调 `corpus concepts ...` |
| 认证 / 评分 concept | 用本 skill 的 `corpus concepts certify` |
| 看 audit log | 用本 skill 的 `corpus audit` |
| 改 vault 配置 (git / auto commit 等) | `corpus-config` skill (未来) |
| 维护 (delete orphan / staleness) | `corpus-maintain` skill (未来) |

完整 ingest 工作流 (source → LLM 抽 concept → write/update → index sync) 见 **`corpus-ingest`** skill.
本 skill 是主入口, 包含 路由 + 跨 skill 共享概念 (CAS / dedup / 错误) + CLI 速查.


## dedup 决策启发式

`concepts find-by-link vault "<wikilink 文本>"` 返回候选列表：

- **0 个候选** → 新概念，写
- **1 个候选** → 看 slug 相似度 + title 相似度，agent 自己判断
  - 完全同一概念 → `concepts update` 加 source_ids + add_links
  - 不同概念（虽然 wikilink 文本撞了）→ 起新 slug，写
- **2+ 个候选** → 可能撞了之前的命名，agent 自己挑最像的，或起新 slug

## 错误处理

corpus 的所有错误返回 exit code 1 + stderr：

```
error: <message>
  hint: <hint>
```

常见错误：
- `vault does not exist` → 先 `corpus vault init`
- `duplicate content already staged as ...` → 该 source_id 已存在，跳过
- `duplicate content exists but is deleted: ...` → 同 hash 已 soft-deleted,加 `--force-revive` 复活
- `concept slug already exists` → 用 `concepts update` 而非 `write`
- `link cannot be self-reference` → concept 不能 wikilink 自己
- `link not slug-safe` → links 必须是合法 slug (小写字母数字+连字符)
- `extraction not found` → `concepts remove-extraction` 的 id 不存在
- `concept ... was modified concurrently` → multi-agent CAS 失败 (--expected-version 不匹配), hint 提示 read_concept 重新 read + merge
- `concept slug already exists` (write_concept) → LLM 重新 find-by-link + read + merge + update_concept (--expected-version). 业务决策不在 storage 静默做.
- `no fields to update` → `concepts certify` 至少传一个 `--score / --issues / --suggestions`
- `score is required for first-time certification` → 首次认证必传 `--score` (后续 partial update 可省)
- `path is inside vault raw/` → `sources ingest` 不接受 vault 内文件. raw/ 是 ingest 产物目录, 想重新入库同一文件先 `sources delete <sid>`
- (audit log 报错) → `ingest_log` 表是 schema v4 加的, 旧 vault 跑 init_db 自动 migration. 用 `corpus audit` 查操作历史
- `score must be in [0, 1]` → 0-1 之间的数

sources.batch 的每个 result 也带 `hint` 字段 (deleted 行未带 `--force-revive` 时填)。

## JSON 输出

每个命令支持 `--json` 标志，agent 解析用：

```bash
corpus sources list vault --json | python3 -c "
import json, sys
items = json.load(sys.stdin)
for it in items:
    print(it['source_id'], it['status'])
"
```



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
| `sources ingest <vault> <file>` | 单文件入库 (vault **外**文件, content-hash dedup + `-ingest-<ts>` 后缀) |
| (上一行的反例) | `sources ingest <vault> <vault>/raw/X.md` → 报 `path is inside vault raw/` (不重复 ingest) |
| `sources ingest ... --force-revive` | 同 hash 已 soft-deleted → 复活该 source_id |
| `concepts write <vault> ...` | 写 concept (必传 --extractions, 每个 source 一段 quote_span) |
| `concepts update <vault> <slug> --body ... --add-extractions ...` | 增量更新 (改 title/body + 加 extraction/link) |
| `concepts update <vault> <slug> --expected-version N ...` | CAS 模式: 只在 concept 当前 version=N 时 update, 否则 OptimisticLockError. **multi-agent 推荐必传**. |
| `concepts delete <vault> <slug>` | 删 concept (默认 dry-run, --no-dry-run 真删, 同步清 wiki 文件) |
| `concepts list ... --orphans` / `--certified` / `--uncertified` | 过滤 (--certified 与 --uncertified 互斥) |
| `concepts remove-extraction <vault> <extraction_id>` | 细粒度撤一次抽取 (自动 sync concept.source_ids / is_orphan) |
| `concepts find-by-link ...` | 返回含 `match_score` 字段 (1.0 exact / 0.9 startswith / 0.5 contains / 0.4 title), 按 score DESC 排 |
| `concepts certify ... --score X --issues "..."` | 首次认证必传 `--score`; 后续 partial 可省 (score/issues/suggestions 都 None=保留); 传 `""` 清空 list; 全 None 报 `no fields to update` |
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
| `concepts evidence <vault> <slug> [--source-id SID]` | 查抽取证据 (quote_span + agent + prompt + time) |
| `concepts add-source <vault> <slug> --source-id SID --quote-span "..."` | 给 concept 加一个 source (自动写 extractions + 清 is_orphan) |
| `concepts remove-source <vault> <slug> --source-id SID` | 从 concept 移除一个 source (自动 is_orphan 同步) |
| `index sync <vault>` | 导出 wiki/index/concepts.json + sources.json (write/update/delete/remove-extraction 时已自动调, 这是兜底) |
