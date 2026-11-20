# 迁移与灾备

服务器本身几乎无状态，真正要保护的是几样不可再生的东西。迁移做得好不好，取决于有没有把它们分开处理。

## 资产清单

| 资产 | 位置 | 能否再生 | 迁移方式 |
|---|---|---|---|
| 记忆内容 | `~/memory-vault` | Syncthing 有多副本，但仍是唯一事实来源 | Syncthing 自动同步 |
| vault git 历史 | `~/memory-vault/.git` | 不能 | 随备份搬运 |
| 原始转录归档 | `~/transcript-archive/` | **不能**，客户端只增、服务器可压缩/清除 | rsync 单独搬运，耗时长 |
| Syncthing 设备身份 | `~/.local/state/syncthing/`（旧版在 `~/.config/syncthing/`） | **不能**，丢失后设备 ID 改变 | 停服务后整目录搬 |
| Pi 凭据 | `~/.pi/agent/auth.json` | 能重新登录，但麻烦 | 加密备份 |
| dreaming 单元与配置 | `~/.config/systemd/user/`、`~/.config/amem/`、crontab | 能，脚本可重建 | 备份或重新生成 |
| agent 登录态（Claude/Codex） | 各设备 `~/.claude`、`~/.codex` 等 | 能重新登录 | **不建议跨机复制**，新设备各自登录 |
| Node / Pi / gitleaks | `/usr/local`、`~/.pi` | 能 | `scripts/setup-server.sh` 重建 |

## 备份（旧服务器）

先停掉会写配置的服务，再打包：

```bash
systemctl --user stop syncthing
mkdir -p ~/backups
tar czf ~/backups/amem-server-$(date +%F).tgz \
  -C "$HOME" \
  .local/state/syncthing \
  .pi/agent/auth.json \
  .config/amem \
  .config/systemd/user \
  .local/state/amem-autocommit.log \
  dream-reports \
  memory-vault/.git
systemctl --user start syncthing
```

归档单独搬，不要塞进上面的包：

```bash
rsync -a --info=progress2 ~/transcript-archive/ new-server:~/transcript-archive/
```

`amem-server-*.tgz` 里含 `auth.json` 和 Syncthing 证书，**必须加密存放**：

```bash
age -p -o ~/backups/amem-server-$(date +%F).tgz.age \
  ~/backups/amem-server-$(date +%F).tgz
rm ~/backups/amem-server-*.tgz
chmod 600 ~/backups/amem-server-*.tgz.age
```

没有 `age` 时用 `gpg -c`，同样删除明文包。

## 新服务器恢复

1. **装基础环境**

   ```bash
   git clone <本仓库> ~/agent-memory-sync
   ~/agent-memory-sync/scripts/setup-server.sh --dry-run   # 先看会做什么
   ~/agent-memory-sync/scripts/setup-server.sh
   ```

   脚本会装 Syncthing、git、Python、固定版本的 Node 和 gitleaks，写 dreaming systemd timer 和 autocommit cron。Pi 需要按官方方式单独安装。

2. **恢复 Syncthing 身份**

   先停服务，再解压配置，最后启动：

   ```bash
   systemctl --user stop syncthing
   tar xzf amem-server-YYYY-MM-DD.tgz -C "$HOME" \
     .local/state/syncthing .pi/agent/auth.json .config/amem \
     .config/systemd/user dream-reports memory-vault/.git
   systemctl --user start syncthing
   ```

   设备 ID 恢复后与旧服务器一致，**客户端不需要重新配对**，这是迁移里省事最多的一步。

3. **恢复 vault 工作区**

   `memory-vault/.git` 已恢复，工作区文件会由 Syncthing 从其他设备同步过来。若暂时没有其他在线设备：

   ```bash
   cd ~/memory-vault && git checkout -- .
   ```

4. **恢复归档**

   归档已用 rsync 搬过来，确认属主和权限：

   ```bash
   chmod 700 ~/transcript-archive
   du -sh ~/transcript-archive
   ```

5. **检查定时任务**

   ```bash
   systemctl --user list-timers | grep amem
   crontab -l | grep autocommit
   loginctl show-user "$USER" -p Linger
   ```

   `Linger=yes` 保证用户未登录时 Syncthing 和 timer 仍在运行。

## 客户端需要做什么

- **设备 ID 未变**（恢复了 Syncthing 配置）：不用动。
- **设备 ID 变了**（配置丢失或全新服务器）：每台设备在 Syncthing 里添加新设备 ID，重新接受 `memory-vault` 共享。
- 客户端归档目标 `~/.config/amem/archive.env` 里的服务器地址若变了，同步修改后跑一次：

  ```bash
  scripts/archive-transcripts.sh --dry-run
  ```

## 验证清单

1. vault 两端文件一致：任改一个文件，几秒内另一端可见。
2. `scripts/check-agents.sh` 在客户端全部通过。
3. 服务器手动跑一次 dreaming 预演：

   ```bash
   scripts/dream.sh --dry-run
   ```

   报告应生成，且不应出现「找不到 gitleaks / Pi」。
4. 等一个整点，确认 autocommit 产生了一条 `auto:` 提交。
5. 归档 dry-run 无待传文件：

   ```bash
   scripts/archive-transcripts.sh --dry-run
   ```

## 演练

每半年在一台临时 VM 或容器里按上面步骤走一遍，把漏掉的步骤补回 `setup-server.sh` 或本文档。迁移流程只有实际演练过才算数。
