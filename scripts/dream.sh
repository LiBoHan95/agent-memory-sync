#!/usr/bin/env bash
# 服务器 cron 入口：跑一次 dreaming。
# 报告写到 ~/dream-reports/（由 amem.dream 决定），日志默认 ~/dream.log。
#
# 用法: scripts/dream.sh [amem.dream 的参数，如 --dry-run]
# 环境变量见 docs/edge-interfaces.md §4（AMEM_REPO/AMEM_VAULT/AMEM_ARCHIVE/
# AMEM_AGENT_CMD/AMEM_AGENT_TIMEOUT/AMEM_BUDGET_CHARS）。
set -euo pipefail

usage() {
    cat <<'EOF'
用法: scripts/dream.sh [选项]

透传给 amem.dream 的选项：
  --dry-run     只跑到闸门，不写回、不推进状态
  --no-commit   写回但不做 git commit

环境变量：
  AMEM_REPO        仓库路径（默认脚本所在仓库）
  AMEM_VAULT       vault 路径（默认 ~/memory-vault）
  AMEM_ARCHIVE     转录归档路径（默认 ~/transcript-archive）
  AMEM_DREAM_LOG   日志路径（默认 ~/dream.log）
  AMEM_AGENT_CMD   headless agent 命令，必须含 {prompt} 占位符
EOF
}

for arg in "$@"; do
    case "$arg" in
        -h|--help) usage; exit 0 ;;
    esac
done

REPO="${AMEM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VAULT="${AMEM_VAULT:-$HOME/memory-vault}"
ARCHIVE="${AMEM_ARCHIVE:-$HOME/transcript-archive}"
LOG="${AMEM_DREAM_LOG:-$HOME/dream.log}"

note() {
    local msg="$1"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send "amem dreaming" "$msg" >/dev/null 2>&1 || true
    elif command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"${msg//\"/\\\"}\" with title \"amem dreaming\"" \
            >/dev/null 2>&1 || true
    else
        logger -t amem-dream "$msg" 2>/dev/null || true
    fi
}

fail() {
    echo "dream: $*" >&2
    note "$*"
    exit 1
}

# ---------- 前置检查 ----------
command -v python3 >/dev/null 2>&1 || fail "找不到 python3"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "需要 Python 3.10+"

# 闸门默认用 gitleaks，找不到会 fail closed，提前报出来
command -v gitleaks >/dev/null 2>&1 || fail "找不到 gitleaks（闸门会拒绝写回）"

if [[ -z "${AMEM_AGENT_CMD:-}" ]]; then
    command -v claude >/dev/null 2>&1 || fail "找不到 claude（或设置 AMEM_AGENT_CMD）"
fi

[[ -d "$VAULT" ]] || fail "vault 不存在: $VAULT"
[[ -d "$ARCHIVE" ]] || fail "转录归档不存在: $ARCHIVE"
[[ -d "$REPO/amem" ]] || fail "仓库不完整: $REPO"

mkdir -p "$(dirname "$LOG")"
cd "$REPO"

set +e
python3 -m amem.dream --vault "$VAULT" --archive "$ARCHIVE" "$@" >>"$LOG" 2>&1
code=$?
set -e

if (( code != 0 )); then
    tail -n 20 "$LOG" >&2 || true
    note "dreaming 失败（退出码 ${code}），详见 $LOG"
fi

exit "$code"
