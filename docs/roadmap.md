# 分期计划

## 第一期：共享 vault + 转录归档（当前）

转录归档放在第一期：Claude Code 默认删除 30 天前的转录，晚一天做就多丢一天。

- [x] 架构、格式规范、协议、兼容规则文档
- [x] vault 模板（含 `schema_version`）
- [ ] **立即**：本机 `~/.claude/settings.json` 设 `cleanupPeriodDays: 36500`，止住转录删除
- [ ] 补全 `profile/USER.md`（身份、设备清单）
- [ ] 迁移现有记忆：`~/.claude/CLAUDE.md`、`~/.claude/projects/*/memory/`、`~/.codex/memories` 里关于用户的内容
- [ ] 服务器部署 Syncthing + 自动提交（`setup-server.md`）
- [ ] 服务器建 `~/transcript-archive`，定加密方案（`transcript-archive.md`）
- [x] `scripts/archive-transcripts.sh` + launchd/cron（`install.sh` 自动安装）
- [ ] 本机转录首次全量上传（约 2.4G jsonl）
- [ ] 本机接入 Claude Code、Codex、Gemini、Pi、Cursor（`agent-integration.md`）
- [x] `scripts/install.sh`：新设备接入（协议引用、cleanupPeriodDays、additionalDirectories）
- [x] `scripts/check-agents.sh`：检查各 agent 是否已接入
- [x] `scripts/autocommit.sh`：服务器每小时自动提交
- [x] 服务器只读 Web（`docs/web.md`）：设备活动、归档状态、记忆/报告/提案查看
- [ ] 第二台设备接入
- [ ] 试用两周：切换 agent/设备后偏好是否保持

## 第二期：自动整理（dreaming）

- [x] `amem/parsers/claude_code.py`
- [ ] `amem/parsers/codex.py`、`amem/parsers/pi.py`
- [x] `amem/prompts/dreaming.md`
- [x] `scripts/dream.sh`（调用 `amem.dream`，前置检查 gitleaks/claude）
- [ ] 先手动运行几次，评估 A/B/C 三级产出质量
- [ ] 上 cron，每天一次
- [ ] 跟踪提案接受率，决定是否放宽分级

## 第三期：检索层（按需）

触发条件：`projects/` 和 log 多到靠索引找不到东西。

- [ ] 在服务器上部署 Hindsight 或 mem0（MCP 方式），只索引 vault 和转录归档
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
