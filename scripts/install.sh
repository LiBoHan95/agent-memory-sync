#!/usr/bin/env bash
# 新设备接入共享记忆库。幂等，可重复运行；修改配置前备份为 <文件>.bak.<YYYY-MM-DD>。
# 只修改本脚本列出的文件和键，不读取、不复制任何密钥。
#
# 用法: scripts/install.sh [--dry-run] [--vault PATH] [--agents "claude-code codex gemini pi"]
set -euo pipefail

VAULT="${AMEM_VAULT:-$HOME/memory-vault}"
AGENTS="${AMEM_AGENTS:-claude-code codex gemini pi}"
DRY_RUN=0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'EOF'
用法: scripts/install.sh [选项]

  --dry-run         只显示要做什么，不写任何文件
  --vault PATH      记忆库路径（默认 ~/memory-vault）
  --agents "LIST"   要接入的 agent，空格分隔
                    可选: claude-code codex gemini pi（默认全部）
  -h, --help        显示本帮助

环境变量: AMEM_VAULT, AMEM_AGENTS, AMEM_PROJECT_ROOTS
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --vault) VAULT="${2:?--vault 需要参数}"; shift ;;
        --agents) AGENTS="${2:?--agents 需要参数}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }
plan() { printf '[dry-run] %s\n' "$*"; }

want() {
    local a
    for a in $AGENTS; do
        [[ "$a" == "$1" ]] && return 0
    done
    return 1
}

backup() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    local b="$f.bak.$(date +%Y-%m-%d)"
    if [[ -e "$b" ]]; then
        return 0
    fi
    if (( DRY_RUN )); then
        plan "备份 $f -> $b"
    else
        cp -p "$f" "$b"
        say "已备份: $b"
    fi
}

append_block() {
    local file="$1" marker="$2" block="$3"
    if [[ -f "$file" ]] && grep -qF -- "$marker" "$file"; then
        say "已接入: $file"
        return 0
    fi
    if (( DRY_RUN )); then
        plan "向 $file 追加协议引用"
        return 0
    fi
    mkdir -p "$(dirname "$file")"
    backup "$file"
    {
        [[ -s "$file" ]] && printf '\n'
        printf '%s\n' "$block"
    } >>"$file"
    say "已写入: $file"
}

# ---------- 前置检查 ----------
command -v python3 >/dev/null 2>&1 || { echo "需要 python3" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "需要 Python 3.10+" >&2; exit 1; }
[[ -d "$VAULT" ]] || {
    echo "vault 不存在: ${VAULT}（先按 docs/setup-server.md 完成同步）" >&2
    exit 1
}

say "vault: $VAULT"
say "agents: $AGENTS"
(( DRY_RUN )) && say "模式: dry-run（不写文件）"

# ---------- Claude Code ----------
if want claude-code; then
    append_block "$HOME/.claude/CLAUDE.md" "AGENT_PROTOCOL.md" "<!-- amem: 共享记忆 -->
@$VAULT/AGENT_PROTOCOL.md
@$VAULT/MEMORY.md"

    SETTINGS="$HOME/.claude/settings.json"
    if (( DRY_RUN )); then
        plan "在 $SETTINGS 设置 cleanupPeriodDays=36500，并把 $VAULT 加入 permissions.additionalDirectories"
    else
        mkdir -p "$HOME/.claude"
        [[ -f "$SETTINGS" ]] && backup "$SETTINGS"
        result="$(python3 - "$SETTINGS" "$VAULT" <<'PY'
import json, os, sys

path, vault = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path) and os.path.getsize(path) > 0:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
if not isinstance(data, dict):
    raise SystemExit(f"settings.json 顶层不是对象: {path}")

changed = False
if data.get("cleanupPeriodDays") != 36500:
    data["cleanupPeriodDays"] = 36500
    changed = True

perms = data.setdefault("permissions", {})
if not isinstance(perms, dict):
    raise SystemExit("permissions 不是对象")
dirs = perms.setdefault("additionalDirectories", [])
if not isinstance(dirs, list):
    raise SystemExit("permissions.additionalDirectories 不是数组")
if vault not in dirs:
    dirs.append(vault)
    changed = True

if changed:
    tmp = path + ".amem-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
print("changed" if changed else "unchanged")
PY
)"
        case "$result" in
            changed) say "已更新: $SETTINGS" ;;
            unchanged) say "无需修改: $SETTINGS" ;;
            *) echo "settings 更新失败: $result" >&2; exit 1 ;;
        esac
    fi
fi

# ---------- 其他 agent ----------
INSTRUCTION="开始任何任务前，先读取并遵守 $VAULT/AGENT_PROTOCOL.md，再读 $VAULT/MEMORY.md。"

if want codex; then
    append_block "$HOME/.codex/AGENTS.md" "AGENT_PROTOCOL.md" "$INSTRUCTION"
fi
if want gemini; then
    append_block "$HOME/.gemini/GEMINI.md" "AGENT_PROTOCOL.md" "@$VAULT/AGENT_PROTOCOL.md
@$VAULT/MEMORY.md"
fi
if want pi; then
    append_block "$HOME/.pi/agent/AGENTS.md" "AGENT_PROTOCOL.md" "$INSTRUCTION"
fi

# ---------- 项目级 cleanupPeriodDays 覆盖检查 ----------
for root in ${AMEM_PROJECT_ROOTS:-$HOME/dev $HOME/workspace $HOME/projects}; do
    [[ -d "$root" ]] || continue
    while IFS= read -r f; do
        val="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("cleanupPeriodDays","unset"))' \
            "$f" 2>/dev/null || echo "读取失败")"
        if [[ "$val" != "unset" && "$val" != "36500" ]]; then
            say "警告: $f 的 cleanupPeriodDays=${val}，会覆盖全局设置（issue #90148）"
        fi
    done < <(find "$root" -maxdepth 4 -type f -path '*/.claude/settings.json' 2>/dev/null || true)
done

# ---------- 归档定时任务 ----------
if [[ -x "$REPO/scripts/archive-transcripts.sh" ]]; then
    say "归档脚本已存在：请按 docs/edge-interfaces.md §3 配置 launchd/cron（install.sh 暂不自动安装）"
else
    say "跳过归档定时任务: scripts/archive-transcripts.sh 尚未提供"
fi

say ""
say "完成。重启各 agent 后生效；用 scripts/check-agents.sh 复核。"
say "Cursor 没有全局指令文件，需要手动在 Settings → Rules → User Rules 填入上面的指示句。"
