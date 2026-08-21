# corpus — Agent Instructions

LLM-driven wiki builder, **CLI-first + LLM-decoupled**。Python ≥3.11，依赖仅 `click`。

## 架构（必读）

```
用户 ─对话─▶ Agent (LLM) ─Bash tool─▶ corpus CLI ──▶ vault 目录 + .wiki-meta/corpus.db
            │                    │
            │                    └─ storage.py (纯 Python 函数，无 LLM)
            │
            └─ 🔥 自己用 OpenAI/Anthropic SDK 抽 concepts / 评分
                （不在 corpus 进程里）
```

**corpus 不调任何 LLM**——extract / compile / 评分都是 agent 自己的责任。

## 关键约定

- **content-hash identity**：`source_id = sha256(content)[:16]`（详见 `.legacy/docs/content-hash-identity.md` 保留思路）
- **七条 validate_source_path**（详见 `src/corpus_bot/vault.py`）：存在 / 非 symlink / 常规文件 / `.md`/`.markdown`/无后缀 / ≤50 MiB / canonicalize 后在 vault 内 / **在 `raw/` 子树下**
- **Vault = 用户拥有的本地目录**：daemon 只读不锁定
- **同 hash → 拒收**；同名不同内容 → 自动改名 `<stem>_<unix_ts>_<4hex>.md`
- **Schema 是 DB 单源真源**：运行期以 SQLite 表为准

## 常用命令

完整 CLI 参考见 `.agents/skills/corpus-bot/SKILL.md`，以下是核心 5 个：

```bash
# 初始化
corpus vault init <path>

# 落源
corpus sources ingest <vault> <file>
corpus sources batch <vault> <dir> --glob "*.md"

# 写 / 查 concept（agent 自己用 LLM 生成 body + **必传 quote_span**）
corpus concepts write <vault> --slug X --title Y \
    --body "..." --extractions '[{"source_id":"SID","quote_span":"原文片段..."}]' \
    --prompt-version extract-v1
corpus concepts show <vault> <slug>
corpus concepts search <vault> <query>
corpus concepts evidence <vault> <slug>  # 查抽取证据

# LLM 抽 concept 时推荐走 **临时文件**, 避免在 shell 里转义多行 markdown / JSON:
corpus concepts write <vault> --slug X --title Y \
    --body-file .tmp/concept-X.md \
    --extractions-file .tmp/concept-X-extr.json \
    --prompt-version extract-v1
# --body / --body-file 互斥; --extractions / --extractions-file 互斥
# (concepts update 同理: --body-file / --add-extractions-file)
# 文件上限 1 MiB; 文件路径不存在 → click.Path 自动 exit 2

# 维护 vault
corpus sources delete <vault> <sid>     # 默认 dry-run，看 orphan 影响
corpus concepts list <vault> --orphans    # 看无源 concept
corpus concepts add-source <vault> <slug> --source-id SID --quote-span "..."
corpus index sync <vault>               # 导出 wiki/index/*.json

# 质检
corpus concepts uncertified <vault>
corpus concepts certify <vault> <slug> --score 0.85 --issues ... --suggestions ...
corpus stats <vault>
```

## 跑测试

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

当前 28 个 test case 覆盖 ids / vault 七条校验 / storage 五张表。

## 环境前置

corpus 通过 `[project.scripts]` 注册为 `corpus` 全局命令。

```bash
# 验证安装
corpus --version
# corpus, version 0.2.0

# 安装 (PyPI 发布后):
pip install corpus   # 从 PyPI 装
# 或 uv tool install corpus  (uv 隔离)

# 开发期本地源码安装 (项目根):
uv tool install -e .   # editable, 改了 src/ 立即生效

# git 命令必须装 (vault 强制 git init): macOS brew install git, Linux apt/yum install git

```

`vault init <path>` 等价 `mkdir -p`, vault root 不存在会自动建。

## 代码地图

| 模块 | 职责 |
|---|---|
| `cli.py` | Click CLI（10+ 子命令）+ `--json` 友好输出 |
| `storage.py` | SQLite 五张表（sources / concepts / links / cooccurrence / certification_log）+ 纯函数 |
| `vault.py` | 七条 validate_source_path + 文件布局 |
| `ids.py` | content-hash + slugify |
| `errors.py` | 错误分层（ConfigError / ValidationError / ConflictError / StorageError）|



## source ↔ concept 映射（核心数据模型）

| 表 | 记录什么 | 用途 |
|---|---|---|
| `concepts.source_ids` | concept 来自哪些 source 的 ID（JSON array，set 语义） | 反向索引、UI 显示 |
| `extractions` | source 抽 concept 的**元数据 + 证据**（quote_span / extracted_at / prompt_version / source_content_hash）| 审计、级联删除、重新抽取 |
| `concepts.is_orphan` | 1 = source_ids 为空 | 标记"无源 concept"——需要补 source 或整篇删 |
| `cooccurrence` | 两个 concept 在同一 source 里同时出现过 | Stage 2 synthesis 候选发现 |
| `links` | concept → concept 的 wikilink | 知识图谱 |

**关键操作语义**：

- `sources ingest <vault> <file>` / `sources batch`：
  - content-hash dedup：同 hash 已 active (staged/committed) → `ConflictError` (exit 1)
  - 同 hash 已 soft-deleted → 默认 `ConflictError` 提示 `--force-revive`；加 flag → 复用原 `source_id`，status='staged'，刷新 `raw_path`/`content_hash`
  - 文件名生成（`pick_raw_target` in `vault.py`）：**所有 ingest 都加** `-ingest-<UTC compact ISO>` 后缀（例 `postgresql-mvcc-ingest-20260820-183000.md`），不再依赖撞名检测；同内容二次入库由 content_hash dedup 在 stage_source 拦下
- `sources ingest` 接受 **vault 外**路径（自动 cp 到 `raw/<stem>-ingest-<UTC><ext>`），不再要求源已在 `raw/` 内。vault 内文件（含 `raw/` 子树 + `wiki/` / `.wiki-meta/`）会被 `assert_source_outside_vault` 拒绝，hint 提示：raw/ 是 ingest 产物目录（防重复 ingest）/ vault 内部目录禁止 ingest。`vault.py:validate_source_path` 旧七条规则保留给未来 in-vault 严格校验场景
- `sources delete <sid>`（软删）：`status='deleted'`，自动从引用它的 concept.source_ids 移除；**不级联删 concept**
- 如果删后 concept.source_ids 变空 → 自动 `is_orphan=1`
- `concepts write --extractions '[...]'`：**强制每个 source 一段 quote_span 原文证据**
- `concepts write` 物理写 `wiki/concept/<slug>.md` 失败时 → 自动 `delete_concept()` 回滚 DB 行 (避免 concept 存在但 wiki 文件缺的不一致)
- `concepts update --add-extractions` / `--add-links`：增量 (append-only)；想重写请先 `delete` 再 `write`
- `concepts delete` (默认 dry-run, --no-dry-run 真删): hard delete concept + extractions + links + wiki/concept/<slug>.md，不动 source 表
- `concepts list --orphans / --certified / --uncertified`: 三个 flag 互斥 (certified + uncertified 不能同传)
- `links` 校验：`write_concept` / `update_concept` 都拒绝自引用 + 非 slug-safe 字符串 (用 `slugify()` 反向检查)
- `remove_extraction(extraction_id)`: 细粒度撤一次抽取 — 删 extractions 行 + sync concept.source_ids (该 sid 无其它抽取时移除), 按需 `is_orphan=1`. 与 `remove_source_from_concept` (粗粒度) 互为补充
- `mark_certified` partial update: `score / issues / suggestions` 都可选. `None` = 保留旧值; 传 list (含 `[]`) = 覆盖; 至少一个非 None 必传. 首次认证必传 `score` (旧值是 None)
- `concepts` 表加 `version` 字段 (optimistic concurrency control, schema v3): 每次 update_concept / write_concept 后 +1. agent 端 read-modify-write 用 `--expected-version` (CLI) / `expected_version` (storage), 不匹配抛 OptimisticLockError 让 agent 重新 read + merge (防 multi-agent 覆盖丢失)
- `find_concept_by_link` 加 `match_score` (1.0 exact / 0.9 startswith / 0.5 contains / 0.4 title), 完全不相关过滤掉, 按 score DESC + slug 长度 ASC 排序
- `mark_certified` 用 microsecond 精度时间戳 (`_utc_now_iso()` 是 seconds 精度, 同秒两次认证会撞 `certification_log` 的 (concept_id, certified_at) PK)
- 同一 (source, concept) 可多次抽取，每次都记 extractions 一行（audit history）
- **concept 生命周期 (schema v5)**: `status` 列 = `draft` (默认，LLM 新写未审查) / `evergreen` (人工或 QA 标记为长期有效知识) / `stale` (源改了/过期待重新抽取). CLI: `corpus concepts update <vault> <slug> --status evergreen`. 过滤: `corpus concepts list --status evergreen`.
- **DB ↔ markdown 一致性** (任何 DB 写入都要同步):
  - `concepts write` 把 status/aliases/tags 直接透传给 `write_concept_file` 写进 frontmatter
  - `concepts update` 触发 markdown 重写的条件 = body/title/status/extractions/links 任一非 None (任意一项改了都重 sync, 防 drift)
  - `certify` / `unmark` 无条件 sync (score / issues / suggestions 必更新)
  - `wiki/source/<slug>.md` 是 extraction manifest (只有 `## Concepts extracted from this source` 段), **不复制原文**. single source of truth 永远是 `raw/<file>.md` (git 跟踪, 含原文 + frontmatter). 想读原文打开 raw/, 想看 source ↔ concepts 关系看 wiki/source.
  - 历史 bug 残留: 之前版本 `update_source_page_concepts` 可能把原文写进 wiki/source, 现在每次 sync 都按 single-source-of-truth 回归——自动清掉多余 body, frontmatter 保留.
- **`export_index` (opt-in)** — `corpus index sync <vault>` 手动生成 `wiki/index/concepts.json` + `sources.json`. 不再被 concepts write/update/delete 自动触发 (没人读, 自动写是噪音). 想要 snapshot 给外部 web UI / dashboard 用时手动跑. 默认 `.gitignore` 已加 `wiki/index/*.json` 防止误 commit snapshot.

**Schema 版本**：不要 hardcode 数字(version 变化时文档失修). 运行时查法:

```bash
python3 -c "from corpus.storage import SCHEMA_VERSION; print(SCHEMA_VERSION)"        # 代码侧
sqlite3 <vault>/.wiki-meta/corpus.db 'SELECT value FROM schema_meta WHERE key="schema_version"'   # 落库后
```

`init_db()` 检测 `schema_meta` 表:
- 无记录 → 视为 v1（旧 DDL 含 `UNIQUE(content_hash)`），跑 v1→v2 migration
- 有记录且 < 当前 → 按 `_MIGRATIONS` dict 顺序跑每一步
- 新版 DDL 不再有 `UNIQUE(content_hash)`，因为软删后同 hash 复活需要 UNIQUE 不存在；改由 `stage_source()` 在 storage 层查重 + 按 status 决定 raise / revive

## 行为规范

- **LLM 调用一律在 agent 端**——corpus 进程里不 import 任何 LLM SDK
- **改 storage schema 必须同步 SKILL.md**——storage.py 是单源真源，但 CLI tool schema 决定了 agent 工作流
- **任何改 schema 的 PR 必须附测试**——`tests/test_storage.py` 是覆盖最厚的
- **不要把 `.wiki-meta/`、`corpus.db` 入 git**——已在 .gitignore

## skill 体系

- **`corpus`** (主入口): 路由表 + 跨 skill 共享概念 (CAS / dedup / 错误) + CLI 速查
- **`corpus-init`**: vault setup + install 验证 + 强制 git 命令检查
- **`corpus-ingest`**: 完整 ingest 工作流 (source → LLM 抽 concept → dedup → write/update → index sync), 含 multi-agent 并发要点
- (未来) `corpus-config`: 改 vault 配置 (auto_git / auto_commit 等)
- (未来) `corpus-maintain`: 健康检查 (orphan / staleness / duplicates)

## 未来扩展

- **MCP**：当需要跨 agent 平台或 streaming 时，把 storage.py 的函数包一层 thin CLI wrapper（不需要重写业务逻辑）
- **Stage 2 健康检查**：基于 cooccurrence 表自动检测孤立 concept / 重复 concept
- **Stage 3 FTS5**：升级 search_concepts 从 LIKE 到 FTS5 全文检索
