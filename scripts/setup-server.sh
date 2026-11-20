#!/usr/bin/env bash
# 服务器一键补齐运行环境。幂等，可重复运行。
# 安装依赖、固定 Node/gitleaks 版本、启动 Syncthing、写 dreaming/Web systemd 用户单元与 autocommit cron。
# 加 --init-vault 时会从 vault-template 建立 vault；已有提交的 vault 不会再次 commit。
# 不修改已有 vault 内容。
# Web 令牌只在首次生成时打印一次，其余敏感值只写入文件。
#
# 用法: scripts/setup-server.sh [--dry-run] [--init-vault] [--vault PATH] [--repo PATH]
#
# 环境变量:
#   AMEM_VAULT              vault 路径，默认 ~/memory-vault
#   AMEM_ARCHIVE            转录归档路径，默认 ~/transcript-archive
#   AMEM_WEB_HOST           Web 监听地址，默认 127.0.0.1
#   AMEM_WEB_PORT           Web 监听端口，默认 8787
#   AMEM_NODE_VERSION       固定 Node 版本，默认 24.22.3
#   AMEM_GITLEAKS_VERSION   固定 gitleaks 版本，默认 8.30.1
#   AMEM_DREAM_MODEL        dreaming 使用的模型，默认 deepseek/deepseek-flash
#   AMEM_DREAM_TIME         dreaming 每日运行时间，默认 04:30
set -euo pipefail

VAULT="${AMEM_VAULT:-$HOME/memory-vault}"
ARCHIVE="${AMEM_ARCHIVE:-$HOME/transcript-archive}"
REPO="${AMEM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NODE_VERSION="${AMEM_NODE_VERSION:-24.22.3}"
GITLEAKS_VERSION="${AMEM_GITLEAKS_VERSION:-8.30.1}"
DREAM_MODEL="${AMEM_DREAM_MODEL:-deepseek/deepseek-flash}"
DREAM_TIME="${AMEM_DREAM_TIME:-04:30}"
WEB_HOST="${AMEM_WEB_HOST:-127.0.0.1}"
WEB_PORT="${AMEM_WEB_PORT:-8787}"
PI_BIN="${PI_BIN:-$HOME/.local/bin/pi}"
DRY_RUN=0
INIT_VAULT=0

usage() {
    cat <<'EOF'
用法: scripts/setup-server.sh [选项]

  --dry-run    只打印将要做的事，不安装、不写文件
  --init-vault 从 vault-template 建立 vault（目录非空且不像 vault 时拒绝）
  --vault PATH 记忆库路径（默认 ~/memory-vault）
  --repo PATH  本仓库路径（默认脚本所在仓库）
  -h, --help   显示本帮助

环境变量: AMEM_VAULT, AMEM_ARCHIVE, AMEM_WEB_HOST,
          AMEM_WEB_PORT, AMEM_REPO, AMEM_NODE_VERSION,
          AMEM_GITLEAKS_VERSION, AMEM_DREAM_MODEL, AMEM_DREAM_TIME, PI_BIN
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --init-vault) INIT_VAULT=1; shift ;;
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

if [[ $EUID -eq 0 ]]; then
    warn "正在以 root 运行；建议用普通用户，避免 vault 和 systemd 用户单元落到 /root"
fi
if [[ "$VAULT" == "~/"* ]]; then
    VAULT="$HOME/${VAULT#\~/}"
fi
while [[ "$VAULT" == */ && "$VAULT" != "/" ]]; do
    VAULT="${VAULT%/}"
done
case "$VAULT" in
    *$'\n'*|*$'\r'*)
        echo "setup-server: vault 路径不能包含换行" >&2
        exit 2
        ;;
esac
[[ "$VAULT" == /* ]] || {
    echo "setup-server: vault 路径需要是绝对路径: ${VAULT}" >&2
    exit 2
}
case "$REPO$VAULT$ARCHIVE" in
    *" "*) warn "路径包含空格，systemd 单元可能无法正确解析" ;;
esac

if ! command -v apt-get >/dev/null 2>&1; then
    warn "未找到 apt-get：本脚本按 Debian/Ubuntu 编写，其他发行版请手工安装依赖"
fi

# ---------- 基础包 ----------
step "基础包"
need=()
for pkg in git rsync curl xz-utils ca-certificates python3 syncthing zstd; do
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

# ---------- vault ----------
step "vault"
VAULT_READY=0
AUTOMATION_READY=0
VAULT_REFUSED=0
if (( INIT_VAULT )); then
    looks_like_vault=0
    if [[ -f "$VAULT/AGENT_PROTOCOL.md" && -f "$VAULT/MEMORY.md" ]]; then
        looks_like_vault=1
    fi
    if [[ -e "$VAULT" && -n "$(ls -A "$VAULT" 2>/dev/null)" && $looks_like_vault -eq 0 ]]; then
        VAULT_REFUSED=1
        warn "目录非空且不是 vault，拒绝初始化: $VAULT"
    elif (( DRY_RUN )); then
        if git -C "$VAULT" rev-parse --verify -q HEAD >/dev/null 2>&1; then
            printf '  [dry-run] vault 已有提交，跳过 git init/commit: %s\n' "$VAULT"
        else
            printf '  [dry-run] 从 vault-template 建立 vault 并 git init/commit: %s\n' "$VAULT"
        fi
        VAULT_READY=1
        AUTOMATION_READY=1
    else
        if (( ! looks_like_vault )); then
            mkdir -p "$VAULT"
            cp -R "$REPO/vault-template/." "$VAULT/"
            ok "已从模板建立 vault"
        fi
        if [[ ! -f "$VAULT/.stignore" ]]; then
            printf '.git\n.DS_Store\n.dream-state\n.*.amem-tmp\n' >"$VAULT/.stignore"
            ok "已写入 .stignore"
        fi
        if ! git -C "$VAULT" rev-parse --git-dir >/dev/null 2>&1; then
            git -C "$VAULT" init -q
        fi
        if ! git -C "$VAULT" config user.email >/dev/null 2>&1; then
            git -C "$VAULT" config user.email "amem@localhost"
        fi
        if ! git -C "$VAULT" config user.name >/dev/null 2>&1; then
            git -C "$VAULT" config user.name "amem"
        fi
        if ! git -C "$VAULT" rev-parse --verify -q HEAD >/dev/null 2>&1; then
            git -C "$VAULT" add -A
            if git -C "$VAULT" diff --cached --quiet; then
                ok "vault 已存在，无需提交"
            else
                git -C "$VAULT" -c commit.gpgsign=false commit -qm "init vault"
                ok "已提交 vault 初始版本"
            fi
        else
            ok "vault 已有提交，跳过 git init/commit"
        fi
        VAULT_READY=1
        AUTOMATION_READY=1
    fi
elif [[ -f "$VAULT/AGENT_PROTOCOL.md" && -f "$VAULT/MEMORY.md" ]]; then
    VAULT_READY=1
    if [[ -d "$VAULT/.git" ]]; then
        AUTOMATION_READY=1
        ok "vault 是 git 仓库: $VAULT"
    else
        warn "vault 缺少 .git，dreaming/autocommit 不可用；可加 --init-vault 或手工 git init: $VAULT"
    fi
else
    warn "vault 不存在或缺少 AGENT_PROTOCOL.md/MEMORY.md: ${VAULT}（首次部署加 --init-vault）"
fi

# ---------- 归档目录 ----------
step "归档目录"
if [[ -d "$ARCHIVE" ]]; then
    ok "归档目录存在: $ARCHIVE"
elif (( DRY_RUN )); then
    printf '  [dry-run] mkdir -p %s 并设置权限 700\n' "$ARCHIVE"
else
    mkdir -p "$ARCHIVE"
    chmod 700 "$ARCHIVE"
    ok "已创建归档目录: $ARCHIVE"
fi

# ---------- Syncthing ----------
step "Syncthing"
SYNCTHING_READY=0
if (( DRY_RUN )); then
    printf '  [dry-run] systemctl --user enable --now syncthing（失败则 systemctl enable --now syncthing@%s）\n' "$USER"
    if (( VAULT_REFUSED )); then
        printf '  [dry-run] vault 初始化被拒绝，跳过 Syncthing 文件夹添加\n'
    else
        printf '  [dry-run] 通过 syncthing cli 添加文件夹 memory-vault -> %s\n' "$VAULT"
    fi
else
    if command -v systemctl >/dev/null 2>&1 &&
       systemctl --user enable --now syncthing >/dev/null 2>&1; then
        ok "syncthing 用户服务已启用"
        SYNCTHING_READY=1
    elif command -v systemctl >/dev/null 2>&1 &&
         sudo systemctl enable --now "syncthing@$USER" >/dev/null 2>&1; then
        ok "syncthing@$USER 系统服务已启用"
        SYNCTHING_READY=1
    else
        warn "Syncthing 服务启用失败，请手工启动"
    fi
fi
if (( SYNCTHING_READY )); then
    if (( VAULT_REFUSED )); then
        warn "vault 初始化被拒绝，跳过 Syncthing 文件夹添加: $VAULT"
    elif [[ ! -d "$VAULT" ]]; then
        warn "vault 不存在，跳过 Syncthing 文件夹添加"
    else
        folders=""
        for _ in {1..10}; do
            if folders="$(syncthing cli config folders list 2>/dev/null)"; then
                break
            fi
            sleep 1
        done
        if printf '%s\n' "$folders" | grep -qxF 'memory-vault'; then
            existing_path="$(syncthing cli config folders memory-vault path get 2>/dev/null || true)"
            if [[ -z "$existing_path" ]]; then
                warn "Syncthing 文件夹 memory-vault 已存在，但读取路径失败；请确认它指向 ${VAULT}"
            elif [[ "$existing_path" != "$VAULT" ]]; then
                warn "Syncthing 文件夹 memory-vault 已存在但路径不同: ${existing_path}（期望 ${VAULT}）"
                warn "请在 http://127.0.0.1:8384 修改路径，或调整 --vault 后重跑"
            else
                ok "Syncthing 文件夹已存在: memory-vault（${VAULT}）"
            fi
        elif syncthing cli config folders add \
            --id memory-vault --label memory-vault --path "$VAULT" >/dev/null 2>&1; then
            ok "已添加 Syncthing 文件夹: $VAULT"
        else
            warn "自动添加 Syncthing 文件夹失败，请在 http://127.0.0.1:8384 手工添加"
        fi
    fi
fi

# ---------- Node ----------
step "Node $NODE_VERSION"
NODE_ARCH=""
case "$(uname -m)" in
    x86_64|amd64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
esac
if [[ -z "$NODE_ARCH" ]]; then
    warn "不支持的 CPU 架构: $(uname -m)，跳过 Node 安装"
elif [[ -x /usr/local/bin/node && "$(/usr/local/bin/node -v 2>/dev/null)" == "v$NODE_VERSION" ]]; then
    ok "已安装 /usr/local/bin/node v$NODE_VERSION"
else
    tarball="node-v${NODE_VERSION}-linux-${NODE_ARCH}"
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
GL_ARCH=""
case "$(uname -m)" in
    x86_64|amd64) GL_ARCH="x64" ;;
    aarch64|arm64) GL_ARCH="arm64" ;;
esac
if [[ -z "$GL_ARCH" ]]; then
    warn "不支持的 CPU 架构: $(uname -m)，跳过 gitleaks 安装"
elif command -v gitleaks >/dev/null 2>&1 &&
   [[ "$(gitleaks version 2>/dev/null)" == "$GITLEAKS_VERSION" ]]; then
    ok "已安装 $(gitleaks version)"
else
    if (( DRY_RUN )); then
        printf '  [dry-run] 下载 gitleaks %s 并安装到 /usr/local/bin\n' "$GITLEAKS_VERSION"
    else
        tmp="$(new_tmp)"
        curl -fsSL \
            "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${GL_ARCH}.tar.gz" \
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
    warn "未找到可运行的 Pi（${PI_BIN}）"
    warn "请先按 Pi 官方方式安装；本脚本不代装第三方 agent"
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
Environment=AMEM_ARCHIVE=$ARCHIVE
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

# ---------- Web systemd 用户单元 ----------
step "Web 服务"
WEB_SERVICE="$UNIT_DIR/amem-web.service"
WEB_ENV_DIR="$HOME/.config/amem"
WEB_ENV="$WEB_ENV_DIR/web.env"
WEB_TOKEN=""
TOKEN_GENERATED=0
WEB_BIND_HOST="$WEB_HOST"
case "$WEB_BIND_HOST" in
    \[*\])
        WEB_BIND_HOST="${WEB_BIND_HOST#\[}"
        WEB_BIND_HOST="${WEB_BIND_HOST%\]}"
        ;;
esac
HEALTH_HOST="$WEB_HOST"
DISPLAY_HOST="$WEB_HOST"
for host_var in HEALTH_HOST DISPLAY_HOST; do
    case "${!host_var}" in
        0.0.0.0) printf -v "$host_var" '%s' "127.0.0.1" ;;
        "::"|"[::]") printf -v "$host_var" '%s' "[::1]" ;;
    esac
    case "${!host_var}" in
        \[*\]) ;;
        *:*) printf -v "$host_var" '%s' "[${!host_var}]" ;;
    esac
done

web_content="$(cat <<EOF
[Unit]
Description=amem memory web
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO
Environment=HOME=$HOME
Environment=AMEM_VAULT=$VAULT
Environment=AMEM_ARCHIVE=$ARCHIVE
Environment=AMEM_WEB_HOST=$WEB_BIND_HOST
Environment=AMEM_WEB_PORT=$WEB_PORT
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=-$WEB_ENV
ExecStart=$REPO/scripts/web.sh
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=-$VAULT
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=default.target
EOF
)"

if (( DRY_RUN )); then
    printf '  [dry-run] 写入 %s\n' "$WEB_SERVICE"
    printf '  [dry-run] 确保 %s 存在且权限 600（首次生成令牌）\n' "$WEB_ENV"
else
    mkdir -p "$UNIT_DIR"
    if [[ ! -d "$WEB_ENV_DIR" ]]; then
        mkdir -p "$WEB_ENV_DIR"
        chmod 700 "$WEB_ENV_DIR"
    fi
    if [[ -f "$WEB_ENV" ]]; then
        chmod 600 "$WEB_ENV"
    fi
    if [[ -s "$WEB_ENV" && -n "$(tail -c 1 "$WEB_ENV" 2>/dev/null || true)" ]]; then
        printf '\n' >>"$WEB_ENV"
    fi
    if [[ ! -f "$WEB_ENV" ]]; then
        WEB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
        (umask 077; printf 'AMEM_WEB_TOKEN=%s\n' "$WEB_TOKEN" >"$WEB_ENV")
        TOKEN_GENERATED=1
    elif ! grep -qE '^AMEM_WEB_TOKEN=.+' "$WEB_ENV"; then
        WEB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
        (umask 077; printf 'AMEM_WEB_TOKEN=%s\n' "$WEB_TOKEN" >>"$WEB_ENV")
        TOKEN_GENERATED=1
    fi
    if ! grep -qE '^AMEM_SYNCTHING_API_KEY=.+' "$WEB_ENV"; then
        api_key="$(syncthing cli config gui apikey get 2>/dev/null || true)"
        if [[ -n "$api_key" ]]; then
            (umask 077; printf 'AMEM_SYNCTHING_API_KEY=%s\n' "$api_key" >>"$WEB_ENV")
            ok "已接入 Syncthing API（设备状态可见）"
        else
            warn "未读取到 Syncthing API key，客户端会显示 Syncthing 未接入"
        fi
    fi
    chmod 600 "$WEB_ENV"
    printf '%s\n' "$web_content" >"$WEB_SERVICE"
    ok "已写入 $WEB_SERVICE 和 $WEB_ENV"
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if (( DRY_RUN )); then
    printf '  [dry-run] systemctl --user daemon-reload\n'
    if (( AUTOMATION_READY )); then
        printf '  [dry-run] systemctl --user enable --now amem-dream.timer\n'
    else
        printf '  [dry-run] vault 不可用或不是 git 仓库，跳过启用 amem-dream.timer\n'
    fi
    if (( VAULT_READY )); then
        printf '  [dry-run] systemctl --user enable amem-web && systemctl --user restart amem-web\n'
        printf '  [dry-run] curl %s\n' "http://${HEALTH_HOST}:${WEB_PORT}/healthz"
    else
        printf '  [dry-run] vault 不可用，跳过启用 amem-web\n'
    fi
elif systemctl --user daemon-reload >/dev/null 2>&1; then
    if (( AUTOMATION_READY )); then
        if systemctl --user enable --now amem-dream.timer >/dev/null 2>&1; then
            ok "amem-dream.timer 已启用"
        else
            warn "timer 启用失败，请手工运行: systemctl --user enable --now amem-dream.timer"
        fi
    else
        warn "vault 不可用或不是 git 仓库，已写入 unit 但未启用 amem-dream.timer；初始化后重跑"
    fi
    if (( ! VAULT_READY )); then
        warn "vault 不可用，已写入 unit 但未启动 amem-web；补齐 AGENT_PROTOCOL.md/MEMORY.md 后重新运行本脚本"
    elif systemctl --user enable amem-web >/dev/null 2>&1 &&
         systemctl --user restart amem-web >/dev/null 2>&1; then
        healthy=0
        for _ in {1..10}; do
            if curl -fsS "http://${HEALTH_HOST}:${WEB_PORT}/healthz" >/dev/null 2>&1; then
                healthy=1
                break
            fi
            sleep 1
        done
        if (( healthy )); then
            ok "amem-web 已启动，健康检查通过"
        else
            warn "amem-web 已启用但健康检查未通过，查看 journalctl --user -u amem-web"
        fi
    else
        warn "amem-web 启用失败，请手工运行: systemctl --user enable --now amem-web"
    fi
else
    warn "systemctl --user 不可用，请启用 linger 后手工启用 timer 和 amem-web"
fi

# ---------- autocommit cron ----------
step "autocommit"
if (( AUTOMATION_READY )); then
    cron_script="$REPO/scripts/autocommit.sh"
    line="0 * * * * AMEM_VAULT=$VAULT $cron_script >> $HOME/.local/state/amem-autocommit.log 2>&1"
    current_cron="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current_cron" | grep -qxF -- "$line"; then
        ok "crontab 已有 autocommit"
    elif printf '%s\n' "$current_cron" | grep -qF -- "$cron_script"; then
        if (( DRY_RUN )); then
            printf '  [dry-run] crontab 更新: %s\n' "$line"
        else
            mkdir -p "$HOME/.local/state"
            filtered="$(printf '%s\n' "$current_cron" | grep -vF -- "$cron_script" || true)"
            {
                [[ -n "$filtered" ]] && printf '%s\n' "$filtered"
                printf '%s\n' "$line"
            } | crontab -
            ok "已更新每小时 autocommit"
        fi
    else
        if (( DRY_RUN )); then
            printf '  [dry-run] crontab 追加: %s\n' "$line"
        else
            mkdir -p "$HOME/.local/state"
            (crontab -l 2>/dev/null || true; printf '%s\n' "$line") | crontab -
            ok "已添加每小时 autocommit"
        fi
    fi
else
    warn "vault 不可用或不是 git 仓库，跳过 autocommit；初始化后重跑"
fi

# ---------- linger ----------
step "linger"
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    ok "linger 已启用"
else
    if (( DRY_RUN )); then
        printf '  [dry-run] sudo loginctl enable-linger %s\n' "$USER"
    else
        if sudo loginctl enable-linger "$USER"; then
            ok "已启用 linger"
        else
            warn "linger 启用失败；注销后用户服务可能停止，请手工运行: sudo loginctl enable-linger $USER"
        fi
    fi
fi

# ---------- 摘要 ----------
step "摘要"
echo "  vault:  $VAULT"
if (( AUTOMATION_READY )); then
    echo "  自动化: autocommit 每小时；dreaming 每天 ${DREAM_TIME}（systemd timer，Persistent=true）"
else
    echo "  自动化: 本次未配置（vault 不可用或不是 git 仓库，见上方提示）"
fi
case "$WEB_HOST" in
    127.0.0.1|localhost|"::1"|"[::1]")
        WEB_NOTE="默认只监听回环，通过 SSH 隧道访问"
        ;;
    *)
        WEB_NOTE="监听 ${WEB_HOST}，请确认防火墙与访问控制"
        ;;
esac
if (( VAULT_READY )); then
    echo "  Web:    http://${DISPLAY_HOST}:${WEB_PORT}/（${WEB_NOTE}）"
else
    echo "  Web:    未启动（vault 不可用，见上方提示）"
fi
if (( DRY_RUN )); then
    echo "  Web 令牌: 首次运行时生成并写入 $WEB_ENV"
elif (( TOKEN_GENERATED )); then
    echo "  Web 令牌（只显示一次，也会写入 ${WEB_ENV}）: $WEB_TOKEN"
else
    echo "  Web 令牌: 已存在于 $WEB_ENV"
fi
echo "  日志:   ~/dream.log、~/.local/state/amem-autocommit.log、journalctl --user -u amem-dream -u amem-web"
echo "  仍需手工: Syncthing 设备配对、SSH 免密、Pi 登录（若未就绪）、在各设备运行 scripts/install.sh"
