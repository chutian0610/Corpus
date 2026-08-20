---
name: corpus-ingest
description: >
  完整 ingest 工作流: source markdown → vault ingest → LLM 抽 concept → 
  dedup (find-by-link match_score) → concepts write/update (with CAS) → index sync.
  Use this skill when the user has source content (markdown files, articles, notes, 
  design docs) to ingest into a corpus vault. 触发词: "入库 X", "抽 concept", 
  "build knowledge base", "把 N 个 markdown 入库", "process sources", 
  "ingest directory", "把 corpus 填满", "跑 ingest 流程", "把这篇文章入库".
  
  Covers the full pipeline:
    1. Pre-flight: vault 已 init (corpus-init skill), git 在 PATH
    2. sources ingest / batch (raw_path 自动 <stem>-ingest-<UTC>.<ext>)
    3. 对每个 source_id, Read raw/<file> 抽取概念
    4. corpus concepts find-by-link 查 dedup (match_score >= 0.9 → 已存在)
    5. corpus concepts write (新) / update --expected-version (已有, 防覆盖丢失)
    6. corpus index sync (写/update 时已自动 export_index, 兜底手动跑)
  
  Not for: vault setup (→ corpus-init), config (→ corpus-config), audit log (→ corpus skill), 
  maintenance (→ corpus-maintain, 未来). Multi-agent 并发: write_concept 是 idempotent upsert, 
  update_concept 支持 --expected-version CAS.
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
- 查 audit log / 看操作历史 → 主 `corpus` skill 的 `corpus audit`
- 删除 concept / 清理 orphan → 主 `corpus` skill 的 `corpus delete` 等

## Pre-flight

```bash
# 1. corpus 可用
corpus --version    # corpus, version 0.2.0
# 失败 → 提示 pip install corpus (PyPI, 不再提本地装)

# 2. git 可用 (vault 强制 git init)
git --version       # git version 2.x+
# 失败 → 提示装 git (brew/apt/yum/git-scm.com)

# 3. vault 已建
corpus vault info <vault> --json
# 失败 → 提示用 corpus-init skill
```

## Step 1 — Sources ingest (vault 外 → raw/)

```bash
# 单文件
corpus sources ingest <vault> /path/to/note.md --json
# 返回: {"action": "staged", "source_id": "abc123", "raw_path": ".../note-ingest-20260820-183000.md", "content_hash": "..."}

# 批量 (整个目录)
corpus sources batch <vault> /path/to/notes/ --glob "*.md" --json
# 返回: {"total": N, "staged": M, "duplicates": K, "failed": 0, ...}
```

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

## Step 3 — Dedup 检查 (find-by-link)

每个 candidate slug 跑 `find-by-link` 看是否已存在:

```bash
corpus concepts find-by-link <vault> <candidate-slug> --json
```

返回 match_score 排序的候选:
- `score >= 0.9`: 高度相似, 大概率是同一 concept → 走 `concepts update` 路径
- `score 0.4-0.9`: 部分相关, 仔细看 candidate 决定 merge 还是新写
- `score 0` (空数组): 没相关, 走 `concepts write` 路径

```python
result = json.loads(subprocess.run(["corpus", "concepts", "find-by-link", vault, slug, "--json"], ...).stdout)
if not result:
    # 全新, 走 write
    action = "write"
elif result[0]["match_score"] >= 0.9:
    # 高度相似, 走 update (加新 source, 不丢旧)
    existing_slug = result[0]["slug"]
    action = f"update:{existing_slug}"
```

## Step 4 — Write / Update concept

**新 concept (write)**:
```bash
corpus concepts write <vault> \
  --slug postgresql-mvcc \
  --title "PostgreSQL MVCC" \
  --body "## 定义\n..." \
  --extractions '[{"source_id":"abc123","quote_span":"..."}]' \
  --links wal,transaction-isolation \
  --prompt-version extract-v1 \
  --json
```

**已有 concept (update with CAS)**: 防 multi-agent 覆盖丢失.
```bash
# 1. 读当前 version
v=$(corpus concepts show <vault> postgresql-mvcc --json | jq .version)

# 2. agent 自己做 merge (current body + LLM 新内容)

# 3. 提交 with CAS
corpus concepts update <vault> postgresql-mvcc \
  --body "<merged>" \
  --add-extractions '[{"source_id":"new_sid","quote_span":"..."}]' \
  --expected-version $v \
  --json
# 失败 → OptimisticLockError, 提示 'read_concept again, merge, then update_concept with new expected_version'
# → 回到 1 重新 read + merge
```

**write_concept 也是 idempotent upsert** (schema v3): slug 不存在 → INSERT, 存在 → UPDATE 合并 source_ids. multi-agent 并发不会撞 UNIQUE.

## Step 5 — Index sync (自动)

`concepts write` / `update` / `delete` 内部已经调 `export_index`, 自动写 `wiki/index/concepts.json` + `sources.json`. 手动兜底:

```bash
corpus index sync <vault> --json
```

## Multi-agent 并发要点

- **不同 source + 同 concept**: write_concept 自动 upsert 合并, 不丢数据
- **同 concept update race**: 用 --expected-version CAS, 失败重试
- **多 agent 并行 ingest 不同 source**: SQLite WAL + busy_timeout=30s 自动串行化, raw_path unique (pick_raw_target 加 ingest timestamp), 不撞
- **vault 跨进程互斥**: 不需要 flock (之前 flock 是过度防御, 已移除), SQLite 自己处理

## 完整例子: batch ingest 一个目录

```bash
# 1. ingest 整个目录
corpus sources batch ~/my-wiki /path/to/articles/ --glob "*.md" --json
# → 8 sources staged

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
    existing=$(corpus concepts find-by-link ~/my-wiki $slug --json | jq -r '.[0].slug // empty')
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
        --links "$(echo "$c" | jq -r '.links | join(",")')" \
        --json
    fi
  done
done

# 6. 验收
corpus stats ~/my-wiki --json
corpus audit ~/my-wiki --op stage --limit 20
```

## Out of scope

- vault 初始化 → `corpus-init` skill
- corpus 配置 (是否 git / auto commit 等) → `corpus-config` skill (未来)
- audit log 查询 → 主 `corpus` skill 的 `corpus audit`
- 维护 (delete concept / 修 orphan / staleness) → `corpus-maintain` skill (未来)
- 认证 / 评分 (certify) → 主 `corpus` skill (在 source 全部 ingest 完后, agent 自己做评分, 不在 ingest 工作流)
