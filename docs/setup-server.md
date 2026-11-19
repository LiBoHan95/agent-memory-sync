# 服务器与同步部署

前提：一台常在线的 Linux 服务器，各设备能通过公网或 Tailscale 访问它。

## 一键部署（推荐）

全新 Debian/Ubuntu 服务器，用普通用户执行：

```bash
curl -fsSL https://raw.githubusercontent.com/LiBoHan95/agent-memory-sync/main/scripts/bootstrap-server.sh | bash
```

不想直接把远程脚本交给 bash，可以先克隆再运行：

```bash
git clone https://github.com/LiBoHan95/agent-memory-sync ~/agent-memory-sync
~/agent-memory-sync/scripts/setup-server.sh --init-vault --dry-run   # 只预览
~/agent-memory-sync/scripts/setup-server.sh --init-vault             # 幂等安装
```

脚本会：

- 安装 Syncthing、git、rsync、Python、固定版本的 Node 和 gitleaks；
- 从 `vault-template` 建立 vault（目录非空且不像 vault 时拒绝覆盖）；已有提交的 vault 不会再次执行 `git add`/commit，避免把未提交改动混成 `init vault`；
- 启动 Syncthing，并尝试通过 `syncthing cli` 添加 `memory-vault` 文件夹；`--init-vault` 拒绝初始化时跳过添加；已有同名文件夹但路径不同时只提示，不自动改配置；
- 生成 `amem-web.service` 和 `~/.config/amem/web.env`（权限 600），首次生成令牌并打印一次；
- 写 dreaming 的 systemd timer 和每小时 autocommit cron，并启用 linger。

脚本不会替你做：Syncthing 设备配对、SSH 免密、Pi 安装与登录、各设备上的 `install.sh`。这些会在结尾列出来。Web 只要求 vault 里存在 `AGENT_PROTOCOL.md` 和 `MEMORY.md`；`.git` 影响 dreaming 的提交和 autocommit。vault 缺少这两个文件时，脚本仍会写入 Web 单元，但跳过启动；补齐后重新运行即可。由 Syncthing 同步来的 vault 即使没有 `.git`，Web 也会启动；dreaming 和 autocommit 需要加 `--init-vault` 或手工 `git init` 后重跑才会启用。下面的手工步骤用于理解流程或排错。

换服务器、备份与恢复见 [migration.md](migration.md)。

## 1. 手工建 vault

一键脚本已包含这一步；手工部署时执行：

```bash
git clone <本仓库> ~/agent-memory-sync
mkdir -p ~/memory-vault
cp -r ~/agent-memory-sync/vault-template/. ~/memory-vault/
cd ~/memory-vault && git init && git add -A && git commit -m "init vault"
```

## 2. 安装 Syncthing

```bash
# Debian/Ubuntu
sudo apt install syncthing
systemctl --user enable --now syncthing
loginctl enable-linger $USER   # 用户未登录时也保持运行
```

Web 管理界面默认只监听 `127.0.0.1:8384`，通过 SSH 隧道访问，不要暴露到公网：

```bash
ssh -L 8384:127.0.0.1:8384 user@server
```

在界面里：添加文件夹 `~/memory-vault`，文件夹 ID 固定为 `memory-vault`。

## 3. 各设备加入

macOS：

```bash
brew install syncthing
brew services start syncthing
```

在本机界面（`http://127.0.0.1:8384`）添加服务器为远程设备，接受 `memory-vault` 共享，本地路径设为 `~/memory-vault`。

建议设置：
- 文件版本控制：简易版本控制，保留 10 个版本（误删兜底）。
- 忽略模式（`.stignore`）：
  ```
  .git
  .DS_Store
  .dream-state
  .*.amem-tmp
  ```
  `.git` 和 `.dream-state` 只在服务器上存在，不同步到客户端，避免多端 git 状态互相覆盖。
  `.*.amem-tmp` 是闸门写文件时的临时文件。

## 4. 服务器自动提交

```bash
crontab -e
# 每小时提交一次变化
0 * * * * ~/agent-memory-sync/scripts/autocommit.sh
```

需要异地备份时，加一个私有远程仓库并在 cron 里 `git push`。

## 5. 记忆库 Web（可选）

一键脚本会自动安装并启动 Web，令牌在 `~/.config/amem/web.env`。手工部署或排错见 [web.md](web.md)；默认只监听 `127.0.0.1:8787`，通过 SSH 隧道访问，不暴露公网。

## 6. 网络

- 设备与服务器都在 Tailscale 里时最简单：Syncthing 直接走 tailnet 地址，无需开放端口。
- 不用 Tailscale 时开放 TCP/UDP 22000（同步端口），8384 保持仅本机。

## 7. 验证

1. 在设备 A 上 `echo test >> ~/memory-vault/log/test.md`。
2. 几秒内在服务器和设备 B 上能看到。
3. 删除测试文件。
