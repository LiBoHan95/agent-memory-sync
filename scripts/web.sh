#!/usr/bin/env bash
# 启动记忆库 Web。默认只监听 127.0.0.1:8787，通过 SSH 隧道访问；
# 除了查看，提案页可以批准/驳回提案。
#
# 用法: scripts/web.sh [--host 127.0.0.1] [--port 8787] [--token ...]
#       [--vault PATH] [--archive PATH] [--reports PATH]
#
# 配置也可用环境变量：AMEM_VAULT、AMEM_ARCHIVE、AMEM_REPORTS、
# AMEM_WEB_HOST、AMEM_WEB_PORT、AMEM_WEB_TOKEN。
set -euo pipefail

PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "找不到 $PYTHON" >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "amem 需要 Python 3.10 或更新版本" >&2
    exit 1
fi

cd "$REPO"
exec "$PYTHON" -m amem.web "$@"
