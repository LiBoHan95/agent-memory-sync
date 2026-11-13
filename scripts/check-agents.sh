#!/usr/bin/env bash
# 只读检查本机各 agent 是否已接入共享记忆库。不修改任何文件。
#
# 用法: scripts/check-agents.sh
# 环境变量: AMEM_VAULT（默认 ~/memory-vault）
# 退出码: 0 全部通过；1 有检查未通过。
set -uo pipefail

VAULT="${AMEM_VAULT:-$HOME/memory-vault}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0

ok() { printf '  [ok]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAIL=1; }
warn() { printf '  [warn] %s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

has_ref() { [[ -f "$1" ]] && grep -qF 'AGENT_PROTOCOL.md' "$1"; }

json_get() {
    python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],"unset"))' \
        "$1" "$2" 2>/dev/null || echo "读取失败"
}

json_has_dir() {
    python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
raise SystemExit(0 if sys.argv[2] in d.get("permissions",{}).get("additionalDirectories",[]) else 1)' \
        "$1" "$2" 2>/dev/null
}

check_agent() {
    local name="$1" dir="$2" file="$3"
    if has_ref "$file"; then
        ok "$name: $file 已引用协议"
    elif [[ -d "$dir" ]]; then
        bad "$name: $file 未引用协议"
    else
        warn "$name: 未安装（$dir 不存在）"
    fi
}

# ---------- vault ----------
section "vault"
if [[ -d "$VAULT" ]]; then
    ok "vault 存在: $VAULT"
    if [[ -f "$VAULT/MEMORY.md" ]]; then ok "MEMORY.md 存在"; else bad "缺少 MEMORY.md"; fi
    if [[ -w "$VAULT" ]]; then ok "可写"; else bad "不可写"; fi
    if [[ -d "$VAULT/.git" ]]; then
        ok "是 git 仓库"
    else
        warn "不是 git 仓库（服务器上应初始化，见 docs/setup-server.md）"
    fi
else
    bad "vault 不存在: $VAULT"
fi

# ---------- Python ----------
section "Python"
if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
        ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
    else
        bad "python3 版本低于 3.10"
    fi
else
    bad "找不到 python3"
fi

# ---------- gitleaks ----------
section "gitleaks"
if command -v gitleaks >/dev/null 2>&1; then
    ok "已安装（dreaming 写回需要）"
else
    warn "未安装（dreaming 会 fail closed，refuse 写回）"
fi

# ---------- Claude Code ----------
section "Claude Code"
check_agent "Claude Code" "$HOME/.claude" "$HOME/.claude/CLAUDE.md"

SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$SETTINGS" ]]; then
    val="$(json_get "$SETTINGS" cleanupPeriodDays)"
    if [[ "$val" == "36500" ]]; then
        ok "cleanupPeriodDays=36500"
    else
        bad "cleanupPeriodDays=${val}（应为 36500，否则转录会被清理）"
    fi
    if json_has_dir "$SETTINGS" "$VAULT"; then
        ok "permissions.additionalDirectories 含 vault"
    else
        bad "permissions.additionalDirectories 缺少 $VAULT"
    fi
else
    warn "找不到 $SETTINGS"
fi

# 项目级设置可能覆盖全局清理行为（issue #90148）
for root in ${AMEM_PROJECT_ROOTS:-$HOME/dev $HOME/workspace $HOME/projects}; do
    [[ -d "$root" ]] || continue
    while IFS= read -r f; do
        val="$(json_get "$f" cleanupPeriodDays)"
        if [[ "$val" != "unset" && "$val" != "36500" && "$val" != "读取失败" ]]; then
            warn "$f 的 cleanupPeriodDays=${val}，会覆盖全局设置"
        fi
    done < <(find "$root" -maxdepth 4 -type f -path '*/.claude/settings.json' 2>/dev/null || true)
done

# ---------- 其他 agent ----------
section "其他 agent"
check_agent "Codex" "$HOME/.codex" "$HOME/.codex/AGENTS.md"
check_agent "Gemini" "$HOME/.gemini" "$HOME/.gemini/GEMINI.md"
check_agent "Pi" "$HOME/.pi/agent" "$HOME/.pi/agent/AGENTS.md"
warn "Cursor: 没有全局指令文件，需手动确认 Settings → Rules → User Rules"

# ---------- 转录归档定时任务 ----------
section "转录归档定时任务"
if [[ ! -x "$REPO/scripts/archive-transcripts.sh" ]]; then
    warn "scripts/archive-transcripts.sh 尚未提供"
else
    if [[ "$(uname -s)" == "Darwin" ]]; then
        plist="$HOME/Library/LaunchAgents/io.github.amem.archive.plist"
        if [[ -f "$plist" ]]; then ok "launchd plist 存在"; else warn "launchd plist 不存在: $plist"; fi
        if [[ "$(launchctl list 2>/dev/null)" == *"io.github.amem.archive"* ]]; then
            ok "launchd 已加载"
        else
            warn "launchd 未加载"
        fi
    else
        if crontab -l 2>/dev/null | grep -q 'archive-transcripts.sh'; then
            ok "cron 已配置"
        else
            warn "cron 未配置"
        fi
    fi
    log="$HOME/amem-archive.log"
    if [[ -f "$log" ]]; then
        ok "最近运行: $(date -r "$log" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || stat -c %y "$log" 2>/dev/null)"
    else
        warn "没有找到归档日志 $log"
    fi
fi

printf '\n'
if (( FAIL )); then
    echo "有检查未通过。"
    exit 1
fi
echo "全部通过。"
