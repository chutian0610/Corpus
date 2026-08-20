#!/usr/bin/env bash
# 重装 corpus: 先 uninstall 再 install. 改了 pyproject.toml / 加依赖后用.
#
# 用法:
#   ./scripts/reinstall.sh                 # 默认 python3.14
#   PYTHON=python3.13 ./scripts/reinstall.sh

set -euo pipefail

PYTHON="${PYTHON:-python3.14}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "==> corpus 重装 (uninstall + install)"
echo "    Python: $PYTHON"
echo "    Project: $PROJECT_ROOT"
echo ""

# 1. uninstall (用现有 uninstall.sh 逻辑)
if [ -f "$SCRIPT_DIR/uninstall.sh" ]; then
  bash "$SCRIPT_DIR/uninstall.sh"
else
  # fallback: 简单 uninstall
  if command -v uv >/dev/null 2>&1; then
    uv tool uninstall corpus 2>/dev/null || true
  fi
fi

# 2. install (用现有 install.sh 逻辑)
if [ -f "$SCRIPT_DIR/install.sh" ]; then
  PYTHON="$PYTHON" bash "$SCRIPT_DIR/install.sh"
else
  # fallback: 简单 install
  if ! command -v uv >/dev/null 2>&1; then
    echo "!! uv 未安装, 装 uv: brew install uv 或 curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
  fi
  uv tool install --python "$PYTHON" -e .
fi

echo ""
echo "==> reinstall 完成. 跑 'corpus --version' 验证."
