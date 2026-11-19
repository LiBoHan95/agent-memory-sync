#!/usr/bin/env bash
# 新设备接入共享记忆库。幂等，可重复运行；修改配置前备份为 <文件>.bak.<YYYY-MM-DD>。
# 只修改本脚本列出的文件和键，不读取、不复制任何密钥。
#
# 用法: scripts/install.sh [--dry-run] [--vault PATH] [--agents "auto|all|LIST"]
set -euo pipefail

VAULT="${AMEM_VAULT:-$HOME/memory-vault}"
AGENTS="${AMEM_AGENTS:-auto}"
DRY_RUN=0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"

usage() {
    cat <<'EOF'
用法: scripts/install.sh [选项]

  --dry-run         只显示要做什么，不写任何文件
  --vault PATH      记忆库路径（默认 ~/memory-vault）
  --agents "LIST"   要接入的 agent，空格分隔
                    可选: auto claude-code codex dsh gemini pi all
                    auto（默认）只接入本机已安装的；all 接入全部
  -h, --help        显示本帮助

环境变量: AMEM_VAULT, AMEM_AGENTS, AMEM_PROJECT_ROOTS, DSH_HOME
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

agent_config_dir() {
    case "$1" in
        claude-code) printf '%s' "$HOME/.claude" ;;
        codex) printf '%s' "$HOME/.codex" ;;
        dsh) printf '%s' "$DSH_HOME" ;;
        gemini) printf '%s' "$HOME/.gemini" ;;
        pi) printf '%s' "$HOME/.pi/agent" ;;
    esac
}

agent_installed() {
    [[ -d "$(agent_config_dir "$1")" ]]
}

want() {
    local a
    for a in $AGENTS; do
        [[ "$a" == "all" ]] && return 0
        [[ "$a" == "$1" ]] && return 0
        if [[ "$a" == "auto" ]] && agent_installed "$1"; then
            return 0
        fi
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

managed_update() {
    local file="$1" block="$2" mode="$3"
    python3 - "$file" "$VAULT" "$block" "$mode" <<'PY'
import re
import sys

path, vault, block, mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
begin, end = "<!-- amem:begin -->", "<!-- amem:end -->"
legacy = "<!-- amem: 共享记忆 -->"
protocol_re = re.compile(r"@.+/AGENT_PROTOCOL\.md")
memory_re = re.compile(r"@.+/MEMORY\.md")
instruction_re = re.compile(
    r"开始任何任务前，先读取并遵守 .+/AGENT_PROTOCOL\.md，再读 .+/MEMORY\.md。"
)

try:
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
except FileNotFoundError:
    text = ""
lines = text.splitlines(keepends=True)


def is_protocol_ref(line):
    return bool(protocol_re.fullmatch(line.strip()))


def is_memory_ref(line):
    return bool(memory_re.fullmatch(line.strip()))


def is_instruction(line):
    return bool(instruction_re.fullmatch(line.strip()))


# begin/end 必须严格配对；不配对时不写文件，避免把用户内容夹在块里删掉。
in_block = False
for line in lines:
    stripped = line.strip()
    if stripped == begin:
        if in_block:
            print("malformed")
            raise SystemExit(0)
        in_block = True
    elif stripped == end:
        if not in_block:
            print("malformed")
            raise SystemExit(0)
        in_block = False
if in_block:
    print("malformed")
    raise SystemExit(0)

# 只移除可确认由 amem 生成的形状：受管块、旧 marker 后紧跟的精确 @ 引用、
# 固定句式指示句，以及旧版 Gemini 的相邻 @ 引用对。其他内容原样保留。
out = []
index = 0
has_managed = False
while index < len(lines):
    stripped = lines[index].strip()
    if stripped == begin:
        has_managed = True
        index += 1
        while index < len(lines) and lines[index].strip() != end:
            index += 1
        end_index = index
        if all(not line.strip() for line in lines[end_index + 1:]):
            if out and not out[-1].strip():
                out.pop()
        index += 1
        continue
    if stripped == legacy:
        has_managed = True
        index += 1
        removed = 0
        while (index < len(lines) and removed < 2
               and (is_protocol_ref(lines[index]) or is_memory_ref(lines[index]))):
            index += 1
            removed += 1
        if removed == 0 and index < len(lines) and is_instruction(lines[index]):
            index += 1
        continue
    if is_instruction(lines[index]):
        has_managed = True
        index += 1
        continue
    if (is_protocol_ref(lines[index]) and index + 1 < len(lines)
            and is_memory_ref(lines[index + 1])):
        has_managed = True
        index += 2
        continue
    out.append(lines[index])
    index += 1

cleaned = "".join(out)
newline = "\r\n" if "\r\n" in text else "\n"
body = block.strip().replace("\n", newline)
block_text = f"{begin}{newline}{body}{newline}{end}{newline}"
if cleaned.strip():
    new_text = cleaned
    if not new_text.endswith("\n"):
        new_text += newline
    new_text += newline + block_text
else:
    new_text = block_text

if new_text == text:
    print("current")
elif has_managed:
    print("update")
else:
    print("append")

if mode == "write" and new_text != text:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
PY
}

append_block() {
    local file="$1" block="$2" state result
    state="$(managed_update "$file" "$block" check)"
    case "$state" in
        current)
            say "已接入: $file"
            return 0
            ;;
        append|update) ;;
        malformed)
            echo "发现不完整的 amem 标记（begin/end 不配对），请先手工修复: $file" >&2
            exit 1
            ;;
        *)
            echo "处理失败: ${file}（${state}）" >&2
            exit 1
            ;;
    esac
    if (( DRY_RUN )); then
        if [[ "$state" == "update" ]]; then
            plan "更新 $file 中的协议引用（移除旧标记/旧 vault 引用）"
        else
            plan "向 $file 追加协议引用"
        fi
        return 0
    fi
    mkdir -p "$(dirname "$file")"
    backup "$file"
    result="$(managed_update "$file" "$block" write)"
    case "$result" in
        current) say "已接入: $file" ;;
        update) say "已更新: $file" ;;
        append) say "已写入: $file" ;;
        malformed)
            echo "发现不完整的 amem 标记（begin/end 不配对），请先手工修复: $file" >&2
            exit 1
            ;;
        *)
            echo "处理失败: ${file}（${result}）" >&2
            exit 1
            ;;
    esac
}

# ---------- 前置检查 ----------
if [[ "$VAULT" == "~/"* ]]; then
    VAULT="$HOME/${VAULT#\~/}"
fi
while [[ "$VAULT" == */ && "$VAULT" != "/" ]]; do
    VAULT="${VAULT%/}"
done
case "$VAULT" in
    *$'\n'*|*$'\r'*)
        echo "vault 路径不能包含换行" >&2
        exit 2
        ;;
esac
[[ "$VAULT" == /* ]] || {
    echo "vault 路径需要是绝对路径: ${VAULT}" >&2
    exit 2
}
case "$VAULT" in
    *" "*) say "警告: vault 路径包含空格，部分 agent 可能无法解析引用" ;;
esac
command -v python3 >/dev/null 2>&1 || { echo "需要 python3" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "需要 Python 3.10+" >&2; exit 1; }
[[ -d "$VAULT" ]] || {
    echo "vault 不存在: ${VAULT}（先按 docs/setup-server.md 完成同步）" >&2
    exit 1
}

# ---------- Syncthing 忽略规则 ----------
# .stignore 不参与同步，每台设备都需要各自创建。
STIGNORE="$VAULT/.stignore"
if [[ ! -f "$STIGNORE" ]]; then
    if (( DRY_RUN )); then
        plan "创建 ${STIGNORE}（Syncthing 忽略规则）"
    else
        printf '.git\n.DS_Store\n.dream-state\n.*.amem-tmp\n' >"$STIGNORE"
        say "已创建: $STIGNORE"
    fi
fi

say "vault: $VAULT"
say "agents: $AGENTS"
SELECTED=""
for agent in claude-code codex dsh gemini pi; do
    if want "$agent"; then
        SELECTED="${SELECTED:+$SELECTED }$agent"
    fi
done
say "本次接入: ${SELECTED:-无（未检测到已安装的 agent，可用 --agents all 强制全部接入）}"
(( DRY_RUN )) && say "模式: dry-run（不写文件）"

# ---------- Claude Code ----------
if want claude-code; then
    append_block "$HOME/.claude/CLAUDE.md" "@$VAULT/AGENT_PROTOCOL.md
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

perms = data.get("permissions")
if perms is None:
    perms = {}
    data["permissions"] = perms
elif not isinstance(perms, dict):
    raise SystemExit("permissions 不是对象")
dirs = perms.get("additionalDirectories")
if dirs is None:
    dirs = []
    perms["additionalDirectories"] = dirs
elif not isinstance(dirs, list):
    raise SystemExit("permissions.additionalDirectories 不是数组")

def same_vault(value):
    return isinstance(value, str) and (
        os.path.expanduser(value).rstrip("/") == vault.rstrip("/")
    )

def is_amem_vault(value):
    if not isinstance(value, str):
        return False
    path = os.path.expanduser(value)
    return (os.path.isfile(os.path.join(path, "AGENT_PROTOCOL.md"))
            and os.path.isfile(os.path.join(path, "MEMORY.md")))

kept = [value for value in dirs if same_vault(value) or not is_amem_vault(value)]
removed = [value for value in dirs if value not in kept]
if removed:
    print("已从 settings 移除旧 vault 权限: " + ", ".join(map(str, removed)),
          file=sys.stderr)
    dirs[:] = kept
    changed = True
if not any(same_vault(value) for value in dirs):
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
    append_block "$HOME/.codex/AGENTS.md" "$INSTRUCTION"
fi
if want dsh; then
    append_block "$DSH_HOME/AGENTS.md" "$INSTRUCTION"
fi
if want gemini; then
    append_block "$HOME/.gemini/GEMINI.md" "@$VAULT/AGENT_PROTOCOL.md
@$VAULT/MEMORY.md"
fi
if want pi; then
    append_block "$HOME/.pi/agent/AGENTS.md" "$INSTRUCTION"
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
ARCHIVE_SCRIPT="$REPO/scripts/archive-transcripts.sh"
if [[ -x "$ARCHIVE_SCRIPT" ]]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
        TEMPLATE="$REPO/scripts/launchd/io.github.amem.archive.plist.template"
        PLIST="$HOME/Library/LaunchAgents/io.github.amem.archive.plist"
        if [[ ! -f "$TEMPLATE" ]]; then
            say "警告: 缺少 ${TEMPLATE}，跳过 launchd 安装"
        elif (( DRY_RUN )); then
            plan "渲染 $PLIST 并 launchctl load（每小时）"
        else
            mkdir -p "$HOME/Library/LaunchAgents"
            [[ -f "$PLIST" ]] && backup "$PLIST"
            sed -e "s|__SCRIPT__|$ARCHIVE_SCRIPT|g" \
                -e "s|__LOG__|$HOME/amem-archive.log|g" \
                "$TEMPLATE" >"$PLIST"
            if [[ "${AMEM_SKIP_SCHEDULER:-0}" == "1" ]]; then
                say "已写入 ${PLIST}（AMEM_SKIP_SCHEDULER=1，未加载）"
            else
                launchctl unload "$PLIST" >/dev/null 2>&1 || true
                if launchctl load "$PLIST" >/dev/null 2>&1; then
                    say "已安装并加载 launchd 任务: ${PLIST}（每小时）"
                else
                    say "已写入 ${PLIST}，但 launchctl load 失败，请手动加载"
                fi
            fi
        fi
    elif command -v crontab >/dev/null 2>&1; then
        if [[ "${AMEM_SKIP_SCHEDULER:-0}" == "1" ]]; then
            say "AMEM_SKIP_SCHEDULER=1，未修改 crontab"
        elif crontab -l 2>/dev/null | grep -qF "$ARCHIVE_SCRIPT"; then
            say "cron 归档任务已存在"
        elif (( DRY_RUN )); then
            plan "向 crontab 添加每小时任务: $ARCHIVE_SCRIPT"
        else
            (crontab -l 2>/dev/null; echo "0 * * * * $ARCHIVE_SCRIPT") | crontab -
            say "已安装 cron 归档任务（每小时）"
        fi
    else
        say "未找到 crontab，请手动配置归档定时任务"
    fi
    say "归档目标写入 ~/.config/amem/archive.env，例如:"
    say "  AMEM_ARCHIVE_HOST=user@server"
else
    say "跳过归档定时任务: $ARCHIVE_SCRIPT 尚未提供"
fi

say ""
say "完成。重启各 agent 后生效；用 scripts/check-agents.sh 复核。"
say "Cursor 没有全局指令文件，需要手动在 Settings → Rules → User Rules 填入上面的指示句。"
