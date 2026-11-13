---
schema_version: 1
name: user-preferences
description: 用户偏好：语言、沟通风格、工具链
type: preference
status: active
source: claude-code@migrated-from-CLAUDE.md
created: 2026-11-13
updated: 2026-11-13
---
# 偏好

## 语言与沟通
- 用中文回答。

## 工具链
- 网络搜索、抓取网页、浏览器交互优先用 TinyFish（CLI `tinyfish`，各 agent 的 MCP/skill 已配置）。
  从轻到重：`search` → `fetch content get` → `agent run` → `browser session`。
- 命令输出经 RTK 压缩，视为完整结果；只有结果明显不可用时才用 `rtk proxy <cmd>` 重跑。
