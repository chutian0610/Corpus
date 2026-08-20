#!/usr/bin/env bash
# 本地安装 corpus 为全局 'corpus' 命令 (uv tool install -e .)
#
# 用法:
#   ./scripts/install.sh                 # 默认装 python3.14 (项目 requires-python >=3.11)
#   PYTHON=python3.13 ./scripts/install.sh  # 装到其它 Python
#
# 卸载:
#   uv tool uninstall corpus-bot
#
# 验证安装:
#   which corpus
#   corpus --version

set -euo pipefail

PYTHON="${PYTHON:-python3.14}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "==> corpus 本地安装 (editable mode)"
echo "    Python: $PYTHON"
echo "    Project: $PROJECT_ROOT"
echo ""

# 1. 检查 uv 是否装
if ! command -v uv >/dev/null 2>&1; then
  echo "!! uv 未安装. 推荐装 uv:"
  echo "   macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "   Homebrew:    brew install uv"
  echo "   pipx 备选:  pipx install --python '$PYTHON' -e '$PROJECT_ROOT'  (需要 pipx)"
  exit 1
fi

# 2. 检查 Python 版本 (requires-python >=3.11)
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "!! $PYTHON 未找到. 检查可用的 Python:"
  echo "   ls /opt/homebrew/bin/python3.*  (Homebrew)"
  echo "   uv python list                  (uv 管理的 Python)"
  echo ""
  echo "   备选: 用 uv 自动选 Python (无需 PYTHON 环境变量):"
  echo "   uv tool install -e ."
  exit 1
fi

PY_VERSION="$("$PYTHON" --version | awk '{print $2}')"
PY_MAJOR="$(echo "$PY_VERSION" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VERSION" | cut -d. -f2)"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
  echo "!! $PYTHON 是 $PY_VERSION, 需要 >=3.11 (项目 requires-python)"
  exit 1
fi

# 3. 装
echo "==> 跑 'uv tool install --python $PYTHON -e .'"
uv tool install --python "$PYTHON" -e .

# 4. 提示 PATH (uv tool 默认装到 ~/.local/bin)
UV_BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
case ":$PATH:" in
  *":$UV_BIN:"*) ;;
  *)
    echo ""
    echo "!! $UV_BIN 不在 PATH, 加到 shell rc:"
    echo "   echo 'export PATH=\"$UV_BIN:\$PATH\"' >> ~/.zshrc   # 或 ~/.bashrc"
    ;;
esac

# 5. 验证
echo ""
echo "==> 验证安装"
if command -v corpus >/dev/null 2>&1; then
  CB="$(command -v corpus)"
  VER="$("$CB" --version)"
  echo "    which: $CB"
  echo "    version: $VER"
else
  echo "    corpus-bot 命令不可用 (PATH 问题? 看上面 PATH 提示)"
  exit 1
fi

# 6. 跑测试基线 (quick sanity)
echo ""
echo "==> 跑 pytest 基线 (确认 install 后测试 OK)"
if uv run --python "$PYTHON" --with pytest pytest tests/ -q --no-header 2>&1 | tail -5; then
  echo ""
  echo "==> 完成. 下一步:"
  echo "   corpus vault init <path>           # 建 vault"
  echo "   corpus sources ingest <vault> <file>  # 入原材料 (vault 外)"
  echo "   详细用法: cat .agents/skills/corpus/SKILL.md"
else
  echo "!! 测试失败, install 可能不完整. 跑 'uv tool uninstall corpus-bot && ./scripts/install.sh' 重试"
  exit 1
fi
