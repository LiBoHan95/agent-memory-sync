# 服务器与同步部署

前提：一台常在线的 Linux 服务器，各设备能通过公网或 Tailscale 访问它。

## 1. 服务器上建 vault

```bash
git clone <本仓库> ~/agent-memory-sync
cp -r ~/agent-memory-sync/vault-template ~/memory-vault
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
0 * * * * cd ~/memory-vault && git add -A && git diff --cached --quiet || git commit -qm "auto: $(date -Iseconds)"
```

需要异地备份时，加一个私有远程仓库并在 cron 里 `git push`。

## 5. 网络

- 设备与服务器都在 Tailscale 里时最简单：Syncthing 直接走 tailnet 地址，无需开放端口。
- 不用 Tailscale 时开放 TCP/UDP 22000（同步端口），8384 保持仅本机。

## 6. 验证

1. 在设备 A 上 `echo test >> ~/memory-vault/log/test.md`。
2. 几秒内在服务器和设备 B 上能看到。
3. 删除测试文件。
