# agent-memory-sync

让多个 agent、多台设备共享同一份关于你的记忆。

Claude Code、Codex、DeepSeek Harness、Gemini、Pi、Cursor 都会记住一些东西，但它们各记各的：换一个工具、换一台设备，就要把「我是谁、我偏好什么、我在做什么」重新讲一遍。这个项目把记忆从 agent 里拿出来，放进一份你自己拥有的 Markdown 仓库，让所有 agent 通过同一份协议读写。

自托管、纯文本、可 diff、可回滚；核心只依赖 Python 标准库。

## 它做什么

| 能力 | 说明 |
|---|---|
| 共享记忆 | 所有设备用 Syncthing 同步同一个 vault；agent 读的是同一份事实 |
| 会话归档 | 各 agent 的原始转录只增不删地备份到服务器，永久保存 |
| 自动整理 | 服务器每天回放新日志和转录，低风险变更自动提交，偏好与规则的变更生成提案 |
| 人工闸门 | 提案里的路径、补丁、格式和疑似密钥都会复核，你逐条批准后才写回 |
| 查看与批准 | 服务器 Web 看设备活动、记忆文件、归档统计和提案；逐条批准或驳回 |
| 桌面客户端 | macOS / Windows 的 `amem`，常驻菜单栏看连接、同步和待处理提案，并选择本机接入的 agent |

## 怎么工作

```
        Claude Code   Codex   DeepSeek Harness   Gemini   Pi
              └─────────┴───────────┴────────────┴───────┘
                                 │ 读 AGENT_PROTOCOL.md，按需读 MEMORY.md
                                 ▼
                     vault（纯 Markdown + git）
                        profile/  rules/
                        projects/ log/
                                 ▲
                                 │ Syncthing 双向同步
                     设备 A / 设备 B

vault ──每天 dreaming──▶ 提案（profile/、rules/）
                            │ 你逐条批准
                            ▼
                  闸门复核 → 写回 + git commit
```

- **vault**：一份 Markdown 目录，`profile/` 放身份和偏好，`rules/` 放工作规则，`projects/` 放项目上下文，`log/` 放按设备追加的原始观察。
- **AGENT_PROTOCOL.md**：各 agent 的全局指令文件只引用它，告诉 agent 什么时候读、怎么写、哪些文件不能直接改。
- **dreaming**：每天在服务器上复制一份 vault，让 headless agent 只改副本，最后由确定性代码按路径分级写回。
- **gate**：所有写回都走 `amem/gate.py`，检查路径、补丁上下文、格式和疑似密钥；不通过就留在提案里。
- **转录归档**：`rsync` 单向备份，不带 `--delete`，agent 清理本地会话也不影响归档。

## 设计原则

- **记忆只有一份**：不把记忆分散到各个 agent 的私有存储里。
- **agent 只是客户端**：agent 可以读、可以写日志和项目页，但 `profile/`、`rules/` 只能通过提案改。
- **只注入索引**：启动时读 `MEMORY.md`（控制在 200 行以内），细节按需打开，避免每轮都塞满上下文。
- **写回有闸门**：路径限定、上下文比对、密钥扫描、格式校验，任何一项不过都不写。
- **转录只增不删**：它是唯一不能再生的资产。
- **本地优先**：服务器、Web、归档都在自己的机器上；核心包不引入第三方 Python 依赖。

## 快速开始

需要：一台 Linux 服务器（示例用 Debian/Ubuntu）、Python 3.10+、Syncthing，以及至少一台日常使用的 macOS / Linux 设备。

### 1. 服务器：一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/LiBoHan95/agent-memory-sync/main/scripts/bootstrap-server.sh | bash
```

脚本会克隆仓库到 `~/agent-memory-sync`，从模板建立 vault，安装 Syncthing、Node 和 gitleaks，启用 Web 与 dreaming，配置每小时自动提交和 linger，并在首次生成 Web 令牌。

不想直接把远程脚本交给 bash，可以先克隆再执行：

```bash
git clone https://github.com/LiBoHan95/agent-memory-sync ~/agent-memory-sync
~/agent-memory-sync/scripts/setup-server.sh --init-vault
```

完成后按结尾提示配对 Syncthing 设备；Pi 需要按官方方式单独安装并登录。完整步骤见 [docs/setup-server.md](docs/setup-server.md)。

### 2. 其他设备：配对并同步

```bash
brew install syncthing          # macOS；Linux 用发行版的包
brew services start syncthing
```

打开 <http://127.0.0.1:8384>，添加服务器为远程设备，接受 `memory-vault` 共享，本地路径设为 `~/memory-vault`。

### 3. 接入各 agent

先写归档配置，再跑安装脚本（幂等，改前自动备份）：

```bash
mkdir -p ~/.config/amem
cat > ~/.config/amem/archive.env <<'EOF'
AMEM_ARCHIVE_HOST=user@server
AMEM_DEVICE=my-device
EOF
chmod 600 ~/.config/amem/archive.env

cd ~/agent-memory-sync
scripts/install.sh --dry-run
scripts/install.sh
scripts/check-agents.sh
```

`install.sh` 默认只接入本机已安装的 agent；也可以指定：

```bash
scripts/install.sh --agents "claude-code codex"   # 只接入这两个
scripts/install.sh --agents all                   # 全部接入
```

Cursor 需要手动配置：它没有全局指令文件，在 Settings → Rules → User Rules 填入：

```markdown
开始任何任务前，先读取并遵守 ~/memory-vault/AGENT_PROTOCOL.md，再读 ~/memory-vault/MEMORY.md。
```

详见 [docs/agent-integration.md](docs/agent-integration.md) 和 [docs/transcript-archive.md](docs/transcript-archive.md)。

### 4. 可选：Web 与桌面客户端

Web 默认只监听 `127.0.0.1:8787`，通过 SSH 隧道访问：

```bash
ssh -N -L 8787:127.0.0.1:8787 user@server
# 浏览器打开 http://127.0.0.1:8787/
```

桌面客户端 `amem` 自己维护 SSH 隧道，显示连接、Syncthing 设备、最近整理和待处理提案，令牌存系统钥匙串。它还有一个本机 Agent 页，可以勾选要接入的工具并直接写入协议引用。安装与构建见 [docs/client.md](docs/client.md)。

### 5. 验证

1. 在设备 A 向 `~/memory-vault/log/` 写一条测试记录；
2. 几秒内在服务器和设备 B 上看到同一文件；
3. 在设备 A、B 的新会话里问 agent「我是谁、我偏好什么」；
4. 打开 Web，确认设备活动和归档统计不为空。

## 桌面客户端

`amem` 是 Tauri 2 应用（Rust + TypeScript），macOS 已验证，Windows 构建配置就绪。它不内嵌远程页面，只调用受限 JSON API；需要读长文或完整 diff 时用系统浏览器打开带令牌的网页。macOS 安装包在 GitHub Actions 的 `macos-15` runner 上构建，推 `client-v*` 标签会创建 Release。

## 项目状态

已完成：vault 与协议、转录解析与归档、dreaming 与闸门、服务器 Web、桌面客户端（macOS 构建与实机验证，含 Agent 接入页）。

待补：第二台设备实测、Windows 实机验证、客户端签名与自动更新、`dsh` 转录解析器（`*.jsonl.zstd`）。进度见 [docs/roadmap.md](docs/roadmap.md)。

## 开发

```bash
python3 -m unittest discover -s tests -t .

cd client
npm ci
npm run build          # tsc + vite build
npm run tauri build    # 本机安装包
```

## 文档

| 文档 | 内容 |
|---|---|
| [architecture.md](docs/architecture.md) | 架构与设计决定 |
| [setup-server.md](docs/setup-server.md) | 服务器、Syncthing、git 部署 |
| [agent-integration.md](docs/agent-integration.md) | 各 agent 的接入方法 |
| [memory-format.md](docs/memory-format.md) | 记忆文件格式规范 |
| [dreaming.md](docs/dreaming.md) | 自动整理流程与分级 |
| [transcript-archive.md](docs/transcript-archive.md) | 转录归档 |
| [web.md](docs/web.md) | 服务器 Web |
| [client.md](docs/client.md) | 桌面客户端 |
| [migration.md](docs/migration.md) | 备份、迁移与灾备 |
| [compatibility.md](docs/compatibility.md) | 资产划分与长期兼容规则 |
| [edge-interfaces.md](docs/edge-interfaces.md) | 边界接口 |
| [roadmap.md](docs/roadmap.md) | 分期计划 |

## 许可证

[MIT](LICENSE)
