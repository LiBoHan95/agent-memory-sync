#!/usr/bin/env bash
# 服务器一键补齐运行环境。幂等，可重复运行。
# 安装依赖、固定 Node/gitleaks 版本、写 dreaming systemd 用户单元与 autocommit cron。
# 不修改 vault 内容，不读取、不打印任何密钥。
#
# 用法: scripts/setup-server.sh [--dry-run] [--vault PATH] [--repo PATH]
#
# 环境变量:
#   AMEM_VAULT              vault 路径，默认 ~/memory-vault
#   AMEM_NODE_VERSION       固定 Node 版本，默认 24.22.3
#   AMEM_GITLEAKS_VERSION   固定 gitleaks 版本，默认 8.30.1
#   AMEM_DREAM_MODEL        dreaming 使用的模型，默认 deepseek/deepseek-flash
#   AMEM_DREAM_TIME         dreaming 每日运行时间，默认 04:30
set -euo pipefail

VAULT="${AMEM_VAULT:-$HOME/memory-vault}"
REPO="${AMEM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NODE_VERSION="${AMEM_NODE_VERSION:-24.22.3}"
GITLEAKS_VERSION="${AMEM_GITLEAKS_VERSION:-8.30.1}"
DREAM_MODEL="${AMEM_DREAM_MODEL:-deepseek/deepseek-flash}"
DREAM_TIME="${AMEM_DREAM_TIME:-04:30}"
PI_BIN="${PI_BIN:-$HOME/.local/bin/pi}"
DRY_RUN=0

usage() {
    cat <<'EOF'
用法: scripts/setup-server.sh [选项]

  --dry-run    只打印将要做的事，不安装、不写文件
  --vault PATH 记忆库路径（默认 ~/memory-vault）
  --repo PATH  本仓库路径（默认脚本所在仓库）
  -h, --help   显示本帮助

环境变量: AMEM_VAULT, AMEM_REPO, AMEM_NODE_VERSION,
          AMEM_GITLEAKS_VERSION, AMEM_DREAM_MODEL, AMEM_DREAM_TIME, PI_BIN
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --vault) VAULT="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

ok()   { printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
run() {
    if (( DRY_RUN )); then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

TMP_DIRS=()
cleanup_tmp() {
    local d
    for d in "${TMP_DIRS[@]:-}"; do
        if [[ -n "$d" ]]; then
            rm -rf "$d"
        fi
    done
    return 0
}
trap cleanup_tmp EXIT
new_tmp() {
    local d
    d="$(mktemp -d)"
    TMP_DIRS+=("$d")
    printf '%s' "$d"
}

if [[ "$(uname -s)" != "Linux" && $DRY_RUN -eq 0 ]]; then
    echo "setup-server: 只支持 Linux 服务器（macOS 上可用 --dry-run 预览）" >&2
    exit 2
fi

if ! command -v apt-get >/dev/null 2>&1; then
    warn "未找到 apt-get：本脚本按 Debian/Ubuntu 编写，其他发行版请手工安装依赖"
fi

# ---------- 基础包 ----------
step "基础包"
need=()
for pkg in git rsync curl xz-utils python3 syncthing; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        need+=("$pkg")
    fi
done
if ((${#need[@]})); then
    run sudo apt-get update -qq
    run sudo apt-get install -y --no-install-recommends "${need[@]}"
    ok "已安装: ${need[*]}"
else
    ok "基础包齐全"
fi

# ---------- Node ----------
step "Node $NODE_VERSION"
if [[ -x /usr/local/bin/node && "$(/usr/local/bin/node -v 2>/dev/null)" == "v$NODE_VERSION" ]]; then
    ok "已安装 /usr/local/bin/node v$NODE_VERSION"
else
    tarball="node-v${NODE_VERSION}-linux-x64"
    if (( DRY_RUN )); then
        printf '  [dry-run] 下载 %s 并安装到 /usr/local/lib/nodejs\n' "$tarball"
    else
        tmp="$(new_tmp)"
        curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}.tar.xz" \
            -o "$tmp/node.tar.xz"
        sudo mkdir -p /usr/local/lib/nodejs
        sudo tar -xJf "$tmp/node.tar.xz" -C /usr/local/lib/nodejs
        for b in node npm npx; do
            sudo ln -sf "/usr/local/lib/nodejs/${tarball}/bin/$b" "/usr/local/bin/$b"
        done
        ok "已安装 Node $(/usr/local/bin/node -v)"
    fi
fi

# ---------- gitleaks ----------
step "gitleaks $GITLEAKS_VERSION"
if command -v gitleaks >/dev/null 2>&1 &&
   [[ "$(gitleaks version 2>/dev/null)" == "$GITLEAKS_VERSION" ]]; then
    ok "已安装 $(gitleaks version)"
else
    if (( DRY_RUN )); then
        printf '  [dry-run] 下载 gitleaks %s 并安装到 /usr/local/bin\n' "$GITLEAKS_VERSION"
    else
        tmp="$(new_tmp)"
        curl -fsSL \
            "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
            -o "$tmp/gitleaks.tgz"
        tar xzf "$tmp/gitleaks.tgz" -C "$tmp" gitleaks
        sudo install -m 755 "$tmp/gitleaks" /usr/local/bin/gitleaks
        ok "已安装 $(gitleaks version)"
    fi
fi

# ---------- Pi ----------
step "Pi"
if [[ -x "$PI_BIN" ]] && "$PI_BIN" --version >/dev/null 2>&1; then
    ok "Pi $("$PI_BIN" --version)"
    if "$PI_BIN" auth check --model "$DREAM_MODEL" >/dev/null 2>&1; then
        ok "模型 $DREAM_MODEL 认证就绪"
    else
        warn "模型 $DREAM_MODEL 未就绪，请运行: $PI_BIN auth login <provider>"
    fi
else
    warn "未找到可运行的 Pi（$PI_BIN）"
    warn "请先按 Pi 官方方式安装；本脚本不代装第三方 agent"
fi

# ---------- vault 与归档 ----------
step "vault 与归档"
if [[ -d "$VAULT/.git" ]]; then
    ok "vault 是 git 仓库: $VAULT"
else
    warn "vault 不存在或不是 git 仓库: $VAULT（首次部署见 docs/setup-server.md）"
fi
if [[ -d "$HOME/transcript-archive" ]]; then
    ok "归档目录存在: $HOME/transcript-archive"
else
    warn "归档目录不存在: $HOME/transcript-archive（首次归档后自动创建）"
fi

# ---------- dreaming systemd 用户单元 ----------
step "dreaming systemd 用户单元"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE="$UNIT_DIR/amem-dream.service"
TIMER="$UNIT_DIR/amem-dream.timer"
if (( DRY_RUN )); then
    printf '  [dry-run] mkdir -p %s\n' "$UNIT_DIR"
else
    mkdir -p "$UNIT_DIR"
fi

service_content="$(cat <<EOF
[Unit]
Description=amem dreaming（离线整理共享记忆）
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
Environment=PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin
Environment="AMEM_AGENT_CMD=$PI_BIN -p {prompt} --model $DREAM_MODEL --no-session --no-context-files --no-extensions --no-mcp --no-skills --tools read,write,edit --mode text"
Environment=AMEM_VAULT=$VAULT
ExecStart=/bin/bash $REPO/scripts/dream.sh
TimeoutStartSec=3600
EOF
)"

timer_content="$(cat <<EOF
[Unit]
Description=每日运行 amem dreaming

[Timer]
OnCalendar=*-*-* $DREAM_TIME:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF
)"

if (( DRY_RUN )); then
    printf '  [dry-run] 写入 %s:\n%s\n' "$SERVICE" "$service_content"
    printf '  [dry-run] 写入 %s:\n%s\n' "$TIMER" "$timer_content"
else
    printf '%s\n' "$service_content" >"$SERVICE"
    printf '%s\n' "$timer_content" >"$TIMER"
    ok "已写入 $SERVICE 和 $TIMER"
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if (( DRY_RUN )); then
    printf '  [dry-run] systemctl --user daemon-reload\n'
    printf '  [dry-run] systemctl --user enable --now amem-dream.timer\n'
elif systemctl --user daemon-reload >/dev/null 2>&1; then
    if systemctl --user enable --now amem-dream.timer >/dev/null 2>&1; then
        ok "amem-dream.timer 已启用"
    else
        warn "timer 启用失败，请手工运行: systemctl --user enable --now amem-dream.timer"
    fi
else
    warn "systemctl --user 不可用，请启用 linger 后手工启用 timer"
fi

# ---------- autocommit cron ----------
step "autocommit"
if crontab -l 2>/dev/null | grep -qF 'autocommit.sh'; then
    ok "crontab 已有 autocommit"
else
    line="0 * * * * $REPO/scripts/autocommit.sh >> $HOME/.local/state/amem-autocommit.log 2>&1"
    if (( DRY_RUN )); then
        printf '  [dry-run] crontab 追加: %s\n' "$line"
    else
        mkdir -p "$HOME/.local/state"
        (crontab -l 2>/dev/null || true; printf '%s\n' "$line") | crontab -
        ok "已添加每小时 autocommit"
    fi
fi

# ---------- linger ----------
step "linger"
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    ok "linger 已启用"
else
    if (( DRY_RUN )); then
        printf '  [dry-run] sudo loginctl enable-linger %s\n' "$USER"
    else
        sudo loginctl enable-linger "$USER" && ok "已启用 linger"
    fi
fi

# ---------- 摘要 ----------
step "摘要"
echo "  自动化: autocommit 每小时；dreaming 每天 $DREAM_TIME（systemd timer，Persistent=true）"
echo "  日志:   ~/dream.log、~/.local/state/amem-autocommit.log、journalctl --user -u amem-dream"
echo "  仍需手工: Pi 登录（若未就绪）、Syncthing 设备配对、SSH key、agent 登录"
