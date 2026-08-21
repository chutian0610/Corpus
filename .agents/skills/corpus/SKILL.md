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
(links 相关的错已下架: --links / --add-links CLI flag 不再存在; outgoing links 全从 body 的 [[wikilinks]] 自动派生. 自引用 / unsafe slug 在写入时被 _extract_wikilinks 安全过滤掉, 不抛错.)
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

## CLI 速查 (Stage 2 ingest 简化后, 27 → 19 命令)

> 旧命令 (列在下面 `[deprecated]` 段) 走 alias 转发到新命令, 1 个 release 后删除. 写新 agent workflow **请用新名字**.

### vault
| 命令 | 作用 |
|---|---|
| `vault init <vault>` | 创建 vault 目录 + 初始化 SQLite (.gitignore + initial commit) |
| `vault inspect <vault>` | vault 健康度 + 内容统计: db_initialized / schema_version / concepts (total, certified, uncertified, orphans, avg_score, score_distribution) / sources (total, by_status) |

### sources
| 命令 | 作用 |
|---|---|
| `sources add <vault> <path>` | path 是文件 → 单文件 ingest; path 是目录 → 按 `--glob` (默认 *.md) batch. 撞 active hash → ConflictError; deleted + `--force-revive` → 复活 source_id |
| `sources list <vault>` | 列 source, `--status` 过滤 staged/committed/deleted |
| `sources show <vault> <sid>` | 源元数据 + raw_path |
| `sources delete <vault> <sid>` | 软删 (status=deleted; `--hard` 物理删; `--reason` 存 audit) |
| `sources mark-state <vault> <sid> --status staged\|committed\|deleted` | 通用化状态切换. committed 设 committed_at, deleted 设 deleted_at, re-staging 清两个时间 |

### concepts
| 命令 | 作用 |
|---|---|
| `concepts write <vault> --slug X --title Y --body-file body.md --extractions-file extr.json [--aliases] [--tags] [--status] [--prompt-version]` | 写 concept. body+extractions 强制走临时文件 (避 shell 转义) |
| `concepts update <vault> <slug> [--body-file] [--add-extractions-file] [--status] [--aliases] [--tags] [--expected-version N]` | 增量更新. `--expected-version N` = CAS (multi-agent 推荐必传) |
| `concepts link <vault> <slug> --source SID --quote-span "..." [--extraction-id X]` | 链接 source → concept. 默认 INSERT 新 extractions row; 传 `--extraction-id X` 复用现有 row (UPDATE quote_span) |
| `concepts unlink <vault> <slug> --source SID / --extraction-id X` | 解链. `--source` 粗粒度 (删该 source 全部 extractions + source_ids 减); `--extraction-id` 细粒度 (单条 row) |
| `concepts show <vault> <slug> [--source SID]` | 读 concept frontmatter + body. `--source` filter 退化成 evidence 视图 |
| `concepts list <vault> [--orphans / --certified / --uncertified] [--status] [--tag X [--tag Y ...]]` | 过滤. `--certified`/`--uncertified` 互斥; 其他叠加 |
| `concepts delete <vault> <slug>` | 默认 dry-run, `--no-dry-run` 真删 (同步清 wiki/concept/<slug>.md) |
| `concepts certify <vault> <slug> --score X [--issues a,b] [--suggestions c,d] [--by agent]` | 首次必传 `--score`; 后续 partial 用 None=保留 / `""`=清空 |
| `concepts find-by-link <vault> <link>` | wikilink → candidate list, 按 match_score DESC |
| `concepts search <vault> <query>` | LIKE 搜索 (stage 3 会升 FTS5) |
| `concepts unmark <vault> <slug>` | 撤销认证 |

### top-level (cross-cutting)
| 命令 | 作用 |
|---|---|
| `corpus history <vault>` | 操作审计 (ingest_log 表). `--op` / `--source-id` / `--since` / `--limit` filter |
| `corpus rebuild <vault>` | 从文件系统重建整个 DB (换电脑 / DB 损坏时用). `--dry-run` 默认 |
| `corpus index snapshot <vault>` | opt-in 写 `wiki/index/{concepts,sources}.json` 给外部消费者 |

### Deprecated aliases (旧名字可继续用, 1 release 后删)
| 旧命令 | 当前等价 |
|---|---|
| `vault info` / `vault stats` | `vault inspect` |
| `sources ingest <vault> <file>` | `sources add <vault> <file>` |
| `sources batch <vault> <dir>` | `sources add <vault> <dir>` |
| `sources commit <sid>` | `sources mark-state --status committed` |
| `concepts add-source` | `concepts link` |
| `concepts remove-source` | `concepts unlink --source SID` |
| `concepts remove-extraction` | `concepts unlink --extraction-id X` |
| `concepts evidence` | `concepts show <slug> --source SID` |
| `corpus audit` | `corpus history` |
| `corpus restore-from-files` | `corpus rebuild` |
| `corpus index sync` | `corpus index snapshot` |

