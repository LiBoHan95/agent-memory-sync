# 分期计划

## 第一期：共享 vault + 转录归档（当前）

转录归档放在第一期：Claude Code 默认删除 30 天前的转录，晚一天做就多丢一天。

- [x] 架构、格式规范、协议、兼容规则文档
- [x] vault 模板（含 `schema_version`）
- [x] **立即**：本机 `~/.claude/settings.json` 设 `cleanupPeriodDays: 36500`，止住转录删除
- [x] 补全 `profile/USER.md`（身份、设备清单）
- [x] 迁移现有记忆：`~/.claude/CLAUDE.md`、`~/.claude/projects/*/memory/`、`~/.codex/memories` 里关于用户的内容（活跃项目已迁，旧存量由 dreaming 从转录重建）
- [x] 服务器部署 Syncthing + 自动提交（`setup-server.md`，一键脚本 `scripts/setup-server.sh`）
- [ ] 服务器建 `~/transcript-archive`，定加密方案（`transcript-archive.md`；目录已建，加密未定）
- [x] `scripts/archive-transcripts.sh` + launchd/cron（`install.sh` 自动安装）
- [x] 本机转录首次全量上传（约 2.4G jsonl）
- [x] 本机接入 Claude Code、Codex、DeepSeek Harness、Gemini、Pi、Cursor（`agent-integration.md`；Cursor 需手动填 User Rules）
- [x] `scripts/install.sh`：新设备接入（协议引用、cleanupPeriodDays、additionalDirectories）
- [x] `scripts/check-agents.sh`：检查各 agent 是否已接入
- [x] `scripts/autocommit.sh`：服务器每小时自动提交
- [x] 服务器 Web（`docs/web.md`）：设备活动、归档状态、记忆/报告/提案查看；提案可逐条批准/驳回
- [x] 桌面客户端 amem（`docs/client.md`）：macOS 已验证；Windows 构建配置就绪，待实机验证；本机构建后上传 CNB Release
- [x] `scripts/setup-server.sh`：服务器一键部署（固定版本、timer、cron）
- [x] `docs/migration.md`：备份、迁移与灾备清单
- [ ] 第二台设备接入
- [ ] 试用两周：切换 agent/设备后偏好是否保持

## 第二期：自动整理（dreaming）

- [x] `amem/parsers/claude_code.py`
- [x] `amem/parsers/codex.py`、`amem/parsers/pi.py`
- [x] `amem/prompts/dreaming.md`
- [x] `scripts/dream.sh`（调用 `amem.dream`，前置检查 gitleaks/claude）
- [x] 先手动运行几次，评估 A/B/C 三级产出质量（本机 dry-run + 服务器真实跑均通过）
- [x] 上 cron，每天一次（systemd timer，每天 04:30，Persistent=true）
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
