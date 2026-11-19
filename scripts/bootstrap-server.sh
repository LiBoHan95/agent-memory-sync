#!/usr/bin/env bash
# 全新 Debian/Ubuntu 服务器的一键入口：补 git/curl、克隆仓库，再交给 setup-server.sh。
# 用普通用户运行；需要 sudo 时脚本会自行调用。幂等，可重复运行。
#
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/LiBoHan95/agent-memory-sync/main/scripts/bootstrap-server.sh | bash
#   scripts/bootstrap-server.sh [--dry-run] [setup-server.sh 的其他参数...]
#
# 环境变量:
#   AMEM_REPO_URL  仓库地址，默认 https://github.com/LiBoHan95/agent-memory-sync.git
#   AMEM_REPO      克隆路径，默认 ~/agent-memory-sync
set -euo pipefail

REPO_URL="${AMEM_REPO_URL:-https://github.com/LiBoHan95/agent-memory-sync.git}"
REPO="${AMEM_REPO:-$HOME/agent-memory-sync}"
DRY_RUN=0
ARGS=()

usage() {
    cat <<'EOF'
用法: scripts/bootstrap-server.sh [选项]

  --dry-run    只打印将要做的事，不克隆、不安装
  -h, --help   显示本帮助

其余参数原样传给 scripts/setup-server.sh（例如 --vault PATH）。

环境变量: AMEM_REPO_URL, AMEM_REPO
EOF
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1; ARGS+=("$arg") ;;
        -h|--help) usage; exit 0 ;;
        *) ARGS+=("$arg") ;;
    esac
done

if [[ "$(uname -s)" != "Linux" && $DRY_RUN -eq 0 ]]; then
    echo "bootstrap-server: 只支持 Linux 服务器（macOS 上可用 --dry-run 预览）" >&2
    exit 2
fi
if [[ $EUID -eq 0 ]]; then
    echo "bootstrap-server: 请用普通用户运行，不要用 root" >&2
    exit 2
fi
if ! command -v apt-get >/dev/null 2>&1 && (( DRY_RUN == 0 )); then
    echo "bootstrap-server: 只支持 Debian/Ubuntu（找不到 apt-get）" >&2
    exit 2
fi

if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    if (( DRY_RUN )); then
        echo "  [dry-run] sudo apt-get update && sudo apt-get install -y git curl ca-certificates"
    else
        sudo apt-get update -qq
        sudo apt-get install -y --no-install-recommends git curl ca-certificates
    fi
fi

if [[ -d "$REPO/.git" ]]; then
    if (( DRY_RUN )); then
        echo "  [dry-run] git -C $REPO pull --ff-only"
    elif ! git -C "$REPO" pull --ff-only; then
        echo "bootstrap-server: 更新失败，继续使用现有代码: $REPO" >&2
    fi
elif [[ -e "$REPO" ]]; then
    echo "bootstrap-server: $REPO 已存在且不是 git 仓库，请移走或设置 AMEM_REPO" >&2
    exit 1
else
    if (( DRY_RUN )); then
        echo "  [dry-run] git clone --depth 1 $REPO_URL $REPO"
    else
        mkdir -p "$(dirname "$REPO")"
        GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$REPO_URL" "$REPO"
    fi
fi

SETUP="$REPO/scripts/setup-server.sh"
if [[ ! -f "$SETUP" ]]; then
    if (( DRY_RUN )); then
        echo "  [dry-run] 克隆后运行 bash $SETUP --init-vault ${ARGS[*]:-}"
        exit 0
    fi
    echo "bootstrap-server: 找不到 $SETUP" >&2
    exit 1
fi

if ((${#ARGS[@]})); then
    exec bash "$SETUP" --init-vault "${ARGS[@]}"
fi
exec bash "$SETUP" --init-vault
