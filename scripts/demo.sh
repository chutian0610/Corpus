#!/bin/bash
# corpus-bot 端到端 demo
# 演示：5 篇 PostgreSQL markdown → vault 初始化 → 批量入库 → 模拟 LLM 抽取 → 写 concept → 质检

set -euo pipefail

VAULT="${DEMO_VAULT:-/tmp/corpus-bot-demo}"
NOTE_DIR="${DEMO_NOTE_DIR:-$VAULT/notes}"

echo "=== demo: corpus-bot end-to-end ==="
echo "vault: $VAULT"
echo "notes: $NOTE_DIR"
echo ""

# 1. 创建 vault
mkdir -p "$VAULT"
PYTHONPATH=src python3 -m corpus_bot vault init "$VAULT" --json > /dev/null

# 2. 创建示例 notes
mkdir -p "$NOTE_DIR"
cat > "$VAULT/notes/mvcc.md" <<'NOTE'
# PostgreSQL MVCC

MVCC is the technique PostgreSQL uses to handle concurrent transactions without locking.
Each row carries xmin and xmax system columns tracking visibility.

Key concepts:
- xmin: transaction id that created this row version
- xmax: transaction id that deleted/updated this row (NULL if current)
- VACUUM cleans up dead row versions
NOTE

cat > "$VAULT/notes/wal.md" <<'NOTE'
# Write-Ahead Log (WAL)

The Write-Ahead Log is PostgreSQL's durability mechanism.
Every change is written to WAL before touching the data file.

This guarantees crash recovery: on restart, Postgres replays WAL to restore committed transactions.
NOTE

cat > "$VAULT/notes/replication.md" <<'NOTE'
# Streaming Replication

PostgreSQL replication sends WAL records from primary to replicas asynchronously (or synchronously).

Replicas apply WAL records and can serve read queries.
NOTE

# 3. 批量落源
PYTHONPATH=src python3 -m corpus_bot sources batch "$VAULT" "$VAULT/notes" --glob "*.md"
echo ""

# 4. 列源
PYTHONPATH=src python3 -m corpus_bot sources list "$VAULT" --json
echo ""

# 5. 模拟 LLM 抽取 + 写 concept（实际使用时代替此处为真实 LLM 调用）
SIDS=$(PYTHONPATH=src python3 -m corpus_bot sources list "$VAULT" --json | python3 -c "
import json, sys
for it in json.load(sys.stdin):
    print(it['source_id'])
")

# 演示写一个 concept
FIRST_SID=$(echo "$SIDS" | head -1)
echo "=== writing concept (placeholder body, real usage: agent calls LLM) ==="
PYTHONPATH=src python3 -m corpus_bot concepts write "$VAULT" \
    --slug postgresql-mvcc \
    --title "PostgreSQL MVCC" \
    --body "MVCC explanation. Each row carries xmin/xmax tracking columns. Old versions kept until VACUUM." \
    --source-ids "$FIRST_SID" \
    --links wal,replication --json
echo ""

# 6. 搜索
echo "=== search 'MVCC' ==="
PYTHONPATH=src python3 -m corpus_bot concepts search "$VAULT" "MVCC"
echo ""

# 7. 认证（模拟 LLM 评分）
echo "=== certify ==="
PYTHONPATH=src python3 -m corpus_bot concepts certify "$VAULT" postgresql-mvcc \
    --score 0.85 --issues "短,缺 source 2" --suggestions "补 WAL 段"
echo ""

# 8. 看 stats
echo "=== stats ==="
PYTHONPATH=src python3 -m corpus_bot stats "$VAULT" --json
echo ""

# 9. 看实际 wiki 文件
echo "=== wiki file ==="
cat "$VAULT/wiki/concept/postgresql-mvcc.md"

# cleanup
python3 -c "import shutil; shutil.rmtree('$VAULT')"
echo ""
echo "=== demo done ==="
