#!/usr/bin/env bash
# 卸载本地 corpus (uv tool uninstall corpus)
#
# 用法:
#   ./scripts/uninstall.sh

set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "!! uv 未安装, 不能卸载"
  exit 1
fi

if ! uv tool list 2>/dev/null | grep -q '^corpus '; then
  echo "==> corpus 命令未通过 uv tool 安装 (可能用 pip install -e . 或别的方式装), 跳过"
  echo ""
  echo "    如要全局找: which corpus"
  exit 0
fi

echo "==> uv tool uninstall corpus"
uv tool uninstall corpus

echo ""
echo "==> 验证 (which corpus 应找不到)"
if command -v corpus >/dev/null 2>&1; then
  echo "!! corpus 仍能找到 ($(command -v corpus)), 可能 PATH 还有残留"
  exit 1
fi

echo "    OK, 卸载完成"
