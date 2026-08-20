# corpus-bot — Agent Instructions

LLM-driven wiki builder, **CLI-first + LLM-decoupled**。Python ≥3.11，依赖仅 `click`。

## 架构（必读）

```
用户 ─对话─▶ Agent (LLM) ─Bash tool─▶ corpus-bot CLI ──▶ vault 目录 + .wiki-meta/corpus.db
            │                    │
            │                    └─ storage.py (纯 Python 函数，无 LLM)
            │
            └─ 🔥 自己用 OpenAI/Anthropic SDK 抽 concepts / 评分
                （不在 corpus-bot 进程里）
```

**corpus-bot 不调任何 LLM**——extract / compile / 评分都是 agent 自己的责任。

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
corpus-bot vault init <path>

# 落源
corpus-bot sources ingest <vault> <file>
corpus-bot sources batch <vault> <dir> --glob "*.md"

# 写 / 查 concept（agent 自己用 LLM 生成 body）
corpus-bot concepts write <vault> --slug X --title Y --body Z --source-ids ... --links ...
corpus-bot concepts show <vault> <slug>
corpus-bot concepts search <vault> <query>

# 质检
corpus-bot concepts uncertified <vault>
corpus-bot concepts certify <vault> <slug> --score 0.85 --issues ... --suggestions ...
corpus-bot stats <vault>
```

## 跑测试

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

当前 28 个 test case 覆盖 ids / vault 七条校验 / storage 五张表。

## 代码地图

| 模块 | 职责 |
|---|---|
| `cli.py` | Click CLI（10+ 子命令）+ `--json` 友好输出 |
| `storage.py` | SQLite 五张表（sources / concepts / links / cooccurrence / certification_log）+ 纯函数 |
| `vault.py` | 七条 validate_source_path + 文件布局 |
| `ids.py` | content-hash + slugify |
| `errors.py` | 错误分层（ConfigError / ValidationError / ConflictError / StorageError）|

## 行为规范

- **LLM 调用一律在 agent 端**——corpus-bot 进程里不 import 任何 LLM SDK
- **改 storage schema 必须同步 SKILL.md**——storage.py 是单源真源，但 CLI tool schema 决定了 agent 工作流
- **任何改 schema 的 PR 必须附测试**——`tests/test_storage.py` 是覆盖最厚的
- **不要把 `.wiki-meta/`、`corpus.db` 入 git**——已在 .gitignore

## 未来扩展

- **MCP**：当需要跨 agent 平台或 streaming 时，把 storage.py 的函数包一层 thin CLI wrapper（不需要重写业务逻辑）
- **Stage 2 健康检查**：基于 cooccurrence 表自动检测孤立 concept / 重复 concept
- **Stage 3 FTS5**：升级 search_concepts 从 LIKE 到 FTS5 全文检索
