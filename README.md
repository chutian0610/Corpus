# corpus

LLM-driven wiki builder (CLI-first, LLM-decoupled). 把 markdown 资料入库，自动构建结构化 wiki（含质检）。

## 设计原则

- **CLI-first**：所有功能通过 `corpus <subcommand>` 调用
- **Skill-friendly**：CLI 设计让 agent 通过 Bash 自然使用
- **LLM-decoupled**：corpus 不调任何 LLM，extract / compile / 评分由 agent 端负责
- **Local-first**：vault 是用户拥有的本地 markdown 目录
- **Storage as pure functions**：未来需要 MCP 时只是 CLI 的薄包装

## Quick Start

```bash
# 1. 初始化 vault
corpus vault init ~/my-wiki

# 2. 落源（content-hash dedup + 撞名改名）
corpus sources ingest ~/my-wiki ~/notes/postgresql.md
corpus sources batch ~/my-wiki ~/notes/ --glob "*.md"

# 3. Agent 自己用 LLM 抽 concepts（不在 corpus 里）
# （用你自己的 OpenAI / Anthropic key）

# 4. 写 concept
corpus concepts write ~/my-wiki \
    --slug postgresql-mvcc \
    --title "PostgreSQL MVCC" \
    --body "..." \
    --source-ids d607... \
    --links postgres-transactions,wal

# 5. 搜索 / 浏览
corpus concepts search ~/my-wiki "MVCC"
corpus concepts show ~/my-wiki postgresql-mvcc

# 6. 质检
corpus concepts uncertified ~/my-wiki
corpus concepts certify ~/my-wiki postgresql-mvcc --score 0.85 \
    --issues "缺源" --suggestions "补 WAL 段"

# 7. 看统计
corpus stats ~/my-wiki
```

## Vault 目录结构

```
my-vault/
├── raw/                  # 用户源资料（content-hash 唯一副本）
│   └── <source_id>.md
├── wiki/
│   ├── concept/          # daemon 生成的 wiki 页
│   │   └── <slug>.md
│   └── index/
└── .wiki-meta/           #  daemon 元数据（自动 gitignore）
    ├── .gitignore
    └── corpus.db         # SQLite: sources / concepts / links / cooccurrence / certification_log
```

## 安装

```bash
pip install -e .[dev]
```

只依赖 `click`（CLI 框架），不需要 httpx / fastapi / LLM SDK。

## 跑测试

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
# 或安装后
corpus --version
pytest
```

## Agent 使用

SKILL.md 在 `.agents/skills/corpus-bot/`，Codex / Claude Desktop 等 agent 会自动加载。

详细工作流（入库 / dedup / 质检）见 SKILL.md。

## 项目结构

```
src/corpus_bot/
├── __init__.py        # version + 设计原则 docstring
├── __main__.py        # python -m corpus_bot 入口
├── cli.py             # Click CLI（10+ 子命令）
├── storage.py         # SQLite 五张表 + 纯函数
├── vault.py           # 七条 validate_source_path + 文件布局
├── ids.py             # content-hash + slugify
└── errors.py          # 错误分层

tests/                  # pytest 28 个 case
.agents/skills/corpus-bot/SKILL.md  # agent 使用指南
.legacy/                # 旧版本（v0.1 daemon + HTTP server）保留作参考
```

## Roadmap

- **Stage 1（当前）**：CLI + Skill，corpus 是纯数据层
- **Stage 2**：wiki 健康检查 & 优化（自动合并重复、修复链接、staleness 检测）
- **Stage 3**：多路查询（FTS5 全文检索 + 语义搜索）
- **Stage 4（可选）**：MCP thin wrapper（如果需要跨 agent 平台）

## 阶段一交付清单

- ✅ CLI（vault / sources / concepts / search / stats）
- ✅ SKILL.md（agent 工作流 + 决策启发式）
- ✅ 28 个 pytest 测试
- ✅ Content-hash identity（dedup + 撞名改名）
- ✅ 七条 validate_source_path
- ✅ SQLite 五张表
- ✅ 端到端跑通（vault init → ingest → dedup → certify）

## License

MIT OR Apache-2.0
