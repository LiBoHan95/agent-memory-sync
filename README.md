# agent-memory-sync

让多台设备上的多个 agent（Claude Code、Codex、Cursor、Gemini、Pi 等）共享同一份关于用户的记忆：你是谁、偏好什么、在做哪些项目。

## 核心思路

- **记忆只有一份**：一个 Markdown 仓库（vault），放在自己的服务器上，各设备用 Syncthing 同步。
- **agent 只是客户端**：每个 agent 的全局指令文件引用同一份 `AGENT_PROTOCOL.md`，按协议读写 vault。
- **注入的是索引**：启动时只加载 `MEMORY.md`（200 行以内），细节按需读取。
- **检索层可选**：mem0 / Hindsight 只作为 vault 的索引，不当事实来源（第三期）。
- **转录归档**：各 agent 的原始会话转录单向备份到服务器，永久保存。它是唯一不能再生的资产。
- **自动整理（dreaming）**：服务器每天回放新日志和转录，低风险变更自动提交，偏好与规则的变更生成提案等你确认（第二期）。

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
  roadmap.md            分期计划
amem/                   核心 Python 包（只依赖标准库）
  format.py             记忆文件格式：frontmatter 与 vault 校验、log 读写
  gate.py               写回闸门：A/B/C/拒绝分级、密钥扫描、校验后写回
  dream.py              dreaming 主流程：收集增量 → headless agent → 闸门 → 报告
  parsers/              各 agent 转录解析器（目前只有 Claude Code）
  screen.py             可选的转录预筛
  prompts/dreaming.md   给整理 agent 的指令
tests/                  测试：python3 -m unittest discover -s tests -t .
vault-template/         vault 初始模板，部署时复制到 ~/memory-vault
scripts/                运维脚本（归档、定时任务、安装、检查；待写）
```

## 状态

第一期文档与 vault 模板、第二期核心代码已完成，见 [docs/roadmap.md](docs/roadmap.md)。
待补：`scripts/` 下的运维脚本、Codex / Pi 转录解析器、各 agent 接入。
