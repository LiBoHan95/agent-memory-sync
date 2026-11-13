# agent-memory-sync

让多台设备上的多个 agent（Claude Code、Codex、Cursor、Gemini、Pi 等）共享同一份关于用户的记忆：你是谁、偏好什么、在做哪些项目。

## 核心思路

- **记忆只有一份**：一个 Markdown 仓库（vault），放在自己的服务器上，各设备用 Syncthing 同步。
- **agent 只是客户端**：每个 agent 的全局指令文件引用同一份 `AGENT_PROTOCOL.md`，按协议读写 vault。
- **注入的是索引**：启动时只加载 `MEMORY.md`（200 行以内），细节按需读取。
- **检索层可选**：mem0 / Hindsight 只作为 vault 的索引，不当事实来源（第三期）。
- **转录归档**：各 agent 的原始会话转录单向备份到服务器，永久保存。它是唯一不能再生的资产。
- **自动整理（dreaming）**：服务器每天回放新日志和转录，低风险变更自动提交，偏好与规则的变更生成提案等你确认（第二期）。

## 接入指南

最短路径：先在服务器上放好 vault，再让每台设备用 Syncthing 同步它，最后让各 agent 引用它。下面命令以 macOS/Linux 为例，完整说明见对应文档。

### 1. 服务器：建立 vault 和同步中枢

```bash
git clone <本仓库> ~/agent-memory-sync
cp -r ~/agent-memory-sync/vault-template ~/memory-vault
cd ~/memory-vault && git init && git add -A && git commit -m "init vault"

# Debian/Ubuntu
sudo apt install syncthing
systemctl --user enable --now syncthing
loginctl enable-linger "$USER"     # 未登录时也保持运行
```

在服务器上配置 Syncthing 文件夹：ID 固定为 `memory-vault`，路径为 `~/memory-vault`。GUI 只监听本机，通过 SSH 隧道打开：

```bash
ssh -L 8384:127.0.0.1:8384 user@server
```

再给 vault 加每小时自动提交：

```bash
crontab -e
# 加入下面一行：
0 * * * * ~/agent-memory-sync/scripts/autocommit.sh
```

详见 [docs/setup-server.md](docs/setup-server.md)。

### 2. 其他设备：配对并同步 vault

macOS 示例（Linux 用发行版的 Syncthing 包）：

```bash
brew install syncthing
brew services start syncthing
```

打开 <http://127.0.0.1:8384>，添加服务器为远程设备，接受 `memory-vault` 共享，本地路径设为 `~/memory-vault`。服务器和客户端都确认后，改动会在几秒内双向同步。

### 3. 各 agent：引用共享记忆

先配置转录归档目标；`install.sh` 会顺带安装每小时的归档任务：

```bash
mkdir -p ~/.config/amem
cat > ~/.config/amem/archive.env <<'EOF'
AMEM_ARCHIVE_HOST=user@server
AMEM_DEVICE=my-device
EOF
chmod 600 ~/.config/amem/archive.env

cd ~/agent-memory-sync
scripts/install.sh --dry-run   # 先看会改哪些文件
scripts/install.sh             # 幂等，改前自动备份
scripts/check-agents.sh        # 只读检查接入结果
```

`install.sh` 会为 Claude Code、Codex、Gemini、Pi 写入协议引用或指示句，把 vault 加入 Claude Code 的可访问目录，并设置 `cleanupPeriodDays=36500`，避免转录被自动清理。Cursor 没有全局指令文件，需要在 Settings → Rules → User Rules 手动填入：

```markdown
开始任何任务前，先读取并遵守 ~/memory-vault/AGENT_PROTOCOL.md，再读 ~/memory-vault/MEMORY.md。
```

新设备可先跑一次归档预演，确认目标路径：

```bash
~/agent-memory-sync/scripts/archive-transcripts.sh --dry-run
```

详见 [docs/agent-integration.md](docs/agent-integration.md) 和 [docs/transcript-archive.md](docs/transcript-archive.md)。

### 4. 只读 Web（可选）

服务器上可安装只读 Web，用来看设备活动、记忆文件、git 历史、归档统计和整理报告。默认只监听 `127.0.0.1:8787`，通过 SSH 隧道访问，不暴露公网：

```bash
ssh -N -L 8787:127.0.0.1:8787 user@server
# 浏览器打开 http://127.0.0.1:8787/
```

安装步骤和令牌配置见 [docs/web.md](docs/web.md)。

### 5. 验证

1. 在设备 A 上向 `~/memory-vault/log/` 写一条测试记录。
2. 几秒内在服务器和设备 B 上看到同一文件。
3. 分别在设备 A、B 的新会话里问 agent“我是谁、我偏好什么”，确认它能读到 vault。
4. 在服务器上打开 Web，确认设备活动和归档统计不为空。

## 目录

```
docs/
  architecture.md       架构与设计决定
  setup-server.md       服务器 + Syncthing + git 部署
  agent-integration.md  各 agent 接入方法
  memory-format.md      记忆文件格式规范
  dreaming.md           自动整理流程与分级
  transcript-archive.md 转录归档
  compatibility.md      资产划分与长期兼容规则
  web.md                服务器只读 Web
  roadmap.md            分期计划
.cnb.yml                CNB 流水线：push / PR 时跑测试
amem/                   核心 Python 包（只依赖标准库）
  format.py             记忆文件格式：frontmatter 与 vault 校验、log 读写
  gate.py               写回闸门：A/B/C/拒绝分级、密钥扫描、校验后写回
  dream.py              dreaming 主流程：收集增量 → headless agent → 闸门 → 报告
  render.py             只读 Web 的轻量 Markdown 渲染（安全转义）
  web.py                只读 Web：数据加载、页面、令牌认证、HTTP 服务
  parsers/              各 agent 转录解析器（目前只有 Claude Code）
  screen.py             可选的转录预筛
  prompts/dreaming.md   给整理 agent 的指令
tests/                  测试：python3 -m unittest discover -s tests -t .
vault-template/         vault 初始模板，部署时复制到 ~/memory-vault
scripts/                运维脚本（自动提交、转录归档、dreaming/Web 入口、设备接入、状态检查）
```

## 状态

第一期文档与 vault 模板、第二期核心代码、服务器只读 Web 已完成，见 [docs/roadmap.md](docs/roadmap.md)。
待补：Codex / Pi 解析器、客户端 Syncthing 配对与各 agent 的实际接入验证。
