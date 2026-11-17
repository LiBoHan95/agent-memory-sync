#!/usr/bin/env bash
# 把各 agent 的原始会话转录单向归档到服务器或移动硬盘。
#
# 原则：原样保存、只增不删。rsync 不带 --delete，本地被 agent 清理掉的文件
# 在归档里继续保留；也不带 --ignore-existing，因为转录会持续追加。
#
# 用法: scripts/archive-transcripts.sh [--dry-run]
#
# 配置（按优先级）：
#   1. 环境变量
#   2. ~/.config/amem/archive.env（每行 KEY=VALUE，由 launchd/cron 使用）
#
# 必填其一：
#   AMEM_ARCHIVE_TARGET   直接指定 rsync 目标，如 user@server:~/transcript-archive
#                         或本地路径 /Volumes/backup/transcript-archive
#   AMEM_ARCHIVE_HOST     服务器地址，如 user@server；配合 AMEM_ARCHIVE_DIR
#                         （默认 ~/transcript-archive）
# 可选：
#   AMEM_DEVICE           设备名，默认 hostname -s
#   AMEM_ARCHIVE_LOG      日志，默认 ~/amem-archive.log
#   AMEM_ARCHIVE_LOCK     锁目录，默认 $TMPDIR/amem-archive.lock
#   AMEM_ARCHIVE_RSYNC_OPTS  额外 rsync 参数，如 '-e "ssh -p 2222"'
#   AMEM_ARCHIVE_SSH_OPTS    建远端目录时传给 ssh 的额外参数，如 '-p 2222'
set -euo pipefail

usage() {
    cat <<'EOF'
用法: scripts/archive-transcripts.sh [--dry-run]

  --dry-run   只显示会同步什么，不传输文件
  -h, --help  显示本帮助

目标通过 AMEM_ARCHIVE_TARGET 或 AMEM_ARCHIVE_HOST 指定，也可写入
~/.config/amem/archive.env。详见脚本头部注释。
EOF
}

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

# launchd 的 PATH 很短，先补上常见位置
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

CONFIG="${AMEM_ARCHIVE_CONFIG:-$HOME/.config/amem/archive.env}"
if [[ -f "$CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG"
fi

LOG="${AMEM_ARCHIVE_LOG:-$HOME/amem-archive.log}"
LOCK="${AMEM_ARCHIVE_LOCK:-${TMPDIR:-/tmp}/amem-archive.lock}"
DEVICE="${AMEM_DEVICE:-$(hostname -s)}"

if [[ ! "$DEVICE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "设备名不合法: ${DEVICE}（可用 AMEM_DEVICE 覆盖）" >&2
    exit 2
fi

if [[ -n "${AMEM_ARCHIVE_TARGET:-}" ]]; then
    TARGET="$AMEM_ARCHIVE_TARGET"
elif [[ -n "${AMEM_ARCHIVE_HOST:-}" ]]; then
    TARGET="$AMEM_ARCHIVE_HOST:${AMEM_ARCHIVE_DIR:-~/transcript-archive}"
else
    echo "未配置归档目标：请设置 AMEM_ARCHIVE_HOST（如 user@server）" >&2
    echo "或 AMEM_ARCHIVE_TARGET（rsync 目标，见脚本头部注释）" >&2
    exit 2
fi
TARGET="${TARGET%/}"
if [[ -z "$TARGET" ]]; then
    echo "归档目标不合法: 不能是根路径" >&2
    exit 2
fi
REMOTE=0
[[ "$TARGET" == *:* ]] && REMOTE=1
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30)
if [[ -n "${AMEM_ARCHIVE_SSH_OPTS:-}" ]]; then
    # shellcheck disable=SC2206
    SSH_OPTS+=(${AMEM_ARCHIVE_SSH_OPTS})
fi

mkdir -p "$(dirname "$LOG")"
log() {
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    if [[ -t 1 ]]; then
        printf '%s\n' "$line" | tee -a "$LOG"
    else
        printf '%s\n' "$line" >>"$LOG"
    fi
}

# rsync 不会创建远端路径中的中间目录；每个源目录同步前先建好远端目标。
if (( REMOTE )); then
    REMOTE_HOST="${TARGET%%:*}"
    REMOTE_BASE="${TARGET#*:}"
    case "$REMOTE_BASE" in
        "~/"*) REMOTE_BASE="\$HOME/${REMOTE_BASE#\~/}" ;;
        *)     REMOTE_BASE="$(printf '%q' "$REMOTE_BASE")" ;;
    esac
fi

ensure_remote_dir() {
    local rel="$1"
    ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" \
        "mkdir -p $REMOTE_BASE/$DEVICE/$(printf '%q' "$rel")" </dev/null
}

# 防止上一轮还没跑完又启动一轮
if ! mkdir "$LOCK" 2>/dev/null; then
    # 超过 6 小时的锁视为残留
    if find "$LOCK" -maxdepth 0 -mmin +360 2>/dev/null | grep -q .; then
        log "清理超过 6 小时的残留锁: $LOCK"
        rmdir "$LOCK" 2>/dev/null || true
        mkdir "$LOCK" 2>/dev/null || { log "无法获取锁，跳过"; exit 0; }
    else
        log "另一个归档任务正在运行（${LOCK}），跳过"
        exit 0
    fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

if ! command -v rsync >/dev/null 2>&1; then
    log "找不到 rsync"
    exit 1
fi

log "开始归档 device=$DEVICE target=$TARGET$([[ $DRY_RUN == 1 ]] && echo ' (dry-run)')"

if (( ! REMOTE )) && (( ! DRY_RUN )); then
    mkdir -p "$TARGET"
    chmod 700 "$TARGET" 2>/dev/null || true
fi

# label|源目录|归档子目录
sources() {
    printf '%s|%s|%s\n' \
        "claude-code"            "$HOME/.claude/projects"           "claude-code" \
        "claude-code-transcripts" "$HOME/.claude/transcripts"       "claude-code/transcripts" \
        "codex-sessions"         "$HOME/.codex/sessions"            "codex" \
        "codex-archived"         "$HOME/.codex/archived_sessions"   "codex/archived_sessions" \
        "dsh-sessions"           "$HOME/.dsh/sessions"              "dsh" \
        "pi"                     "$HOME/.pi/agent/sessions"         "pi"
}

ARGS=(-a)
(( DRY_RUN )) && ARGS+=(-n)
# 无人值守时不要卡在 SSH 密码/主机指纹提示上；需要自定义时用 AMEM_ARCHIVE_RSYNC_OPTS
if (( REMOTE )) && [[ -z "${AMEM_ARCHIVE_RSYNC_OPTS:-}" ]]; then
    ARGS+=(-e "ssh -o BatchMode=yes -o ConnectTimeout=30")
fi
if [[ -n "${AMEM_ARCHIVE_RSYNC_OPTS:-}" ]]; then
    # 有意做单词拆分，允许传 '-e "ssh -p 2222"' 这类参数
    # shellcheck disable=SC2206
    ARGS+=(${AMEM_ARCHIVE_RSYNC_OPTS})
fi

failed=0
while IFS='|' read -r label src dest; do
    if [[ ! -d "$src" ]]; then
        log "跳过 $label: $src 不存在"
        continue
    fi
    dst="$TARGET/$DEVICE/$dest"
    if (( REMOTE )) && (( ! DRY_RUN )); then
        if ! ensure_remote_dir "$dest"; then
            failed=1
            log "FAIL $label -> $dst（无法创建远端目录）"
            continue
        fi
    fi
    if (( ! REMOTE )) && (( ! DRY_RUN )); then
        mkdir -p "$dst"
    fi
    if rsync "${ARGS[@]}" "$src/" "$dst/" >>"$LOG" 2>&1; then
        log "ok   $label -> $dst"
    else
        code=$?
        if (( code == 24 )); then
            # 源文件在传输过程中消失（agent 清理/滚动），不算失败
            log "warn $label -> ${dst}（rsync 24: 部分源文件已消失）"
        else
            failed=1
            log "FAIL $label -> ${dst}（rsync 退出码 ${code}，详见日志）"
        fi
    fi
done < <(sources)

if (( failed )); then
    log "归档结束，有失败"
    exit 1
fi
log "归档结束"
