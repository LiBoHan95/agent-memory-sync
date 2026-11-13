# agent-memory-sync

让多台设备上的多个 agent（Claude Code、Codex、Cursor、Gemini、Pi 等）共享同一份关于用户的记忆：你是谁、偏好什么、在做哪些项目。

## 核心思路

- **记忆只有一份**：一个 Markdown 仓库（vault），放在自己的服务器上，各设备用 Syncthing 同步。
- **agent 只是客户端**：每个 agent 的全局指令文件引用同一份 `AGENT_PROTOCOL.md`，按协议读写 vault。
- **注入的是索引**：启动时只加载 `MEMORY.md`（200 行以内），细节按需读取。
- **检索层可选**：mem0 / Hindsight 只作为 vault 的索引，不当事实来源（第三期）。
- **dreaming**：服务器定期回放会话日志，提出记忆修正 diff，人工确认后合入（第二期）。

## 目录

```
docs/
  architecture.md       架构与设计决定
  setup-server.md       服务器 + Syncthing + git 部署
  agent-integration.md  各 agent 接入方法
  memory-format.md      记忆文件格式规范
  dreaming.md           离线整理流程
  roadmap.md            分期计划
vault-template/         vault 初始模板，部署时复制到 ~/memory-vault
scripts/                安装与维护脚本（待写）
```

## 状态

第一期：文档与 vault 模板。见 [docs/roadmap.md](docs/roadmap.md)。
