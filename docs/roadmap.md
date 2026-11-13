# 分期计划

## 第一期：共享 vault（当前）

- [x] 架构、格式规范、协议文档
- [x] vault 模板
- [ ] 补全 `profile/USER.md`（身份、设备清单）
- [ ] 迁移现有记忆：`~/.claude/CLAUDE.md`、`~/.claude/projects/*/memory/`、`~/.codex/memories` 里关于用户的内容
- [ ] 服务器部署 Syncthing + 自动提交（`docs/setup-server.md`）
- [ ] 本机接入 Claude Code、Codex、Gemini、Pi、Cursor（`docs/agent-integration.md`）
- [ ] `scripts/install.sh`：在新设备上一键写入各 agent 的引用
- [ ] `scripts/check-agents.sh`：检查各 agent 是否已接入
- [ ] 第二台设备接入
- [ ] 试用两周：切换 agent/设备后偏好是否保持

## 第二期：dreaming

- [ ] `scripts/dream.sh`
- [ ] 第一次手动运行，评估提案质量
- [ ] 上 cron

## 第三期：检索层（按需）

触发条件：`projects/` 和 log 多到靠索引找不到东西。

- [ ] 在服务器上部署 Hindsight 或 mem0（MCP 方式），只索引 vault
- [ ] 各 agent 加 MCP 连接
- [ ] 对比有无检索层的效果，再决定是否保留

## 选型备忘

| 方案 | 结论 |
|---|---|
| mem0 | 抽取式，有损；适合三期做检索，不做事实来源 |
| Hindsight | 检索质量最好，但每次操作都调 LLM；三期首选候选 |
| Letta | 是完整 agent 运行时，与现有 agent 不兼容 |
| Zep/Graphiti | 时间有效性思路借鉴为 superseded 标记 |
| Cognee | 图数据库维护成本高，不需要 |
