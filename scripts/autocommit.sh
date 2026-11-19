#!/usr/bin/env bash
# 服务器每小时自动提交 vault 变更。
# dreaming 的提交前缀是 dream:，本脚本是 auto:，两者可以分开 revert。
#
# 环境变量：
#   AMEM_VAULT   vault 路径，默认 ~/memory-vault
#   AMEM_PUSH=1  提交后顺带 git push（需要事先配好远程）
set -euo pipefail

VAULT="${AMEM_VAULT:-$HOME/memory-vault}"

if [[ ! -d "$VAULT/.git" ]]; then
    echo "autocommit: $VAULT 不是 git 仓库" >&2
    exit 1
fi

cd "$VAULT"
git add -A

# 没有变更时 git diff --cached --quiet 返回 1，这里在 if 里判断是安全的
if git diff --cached --quiet; then
    exit 0
fi

git -c commit.gpgsign=false commit -q -m "auto: $(date +%Y-%m-%dT%H:%M:%S%z)"

if [[ "${AMEM_PUSH:-0}" == "1" ]]; then
    git push -q
fi
