# 记忆文件格式

## 通用规则

- 一个文件一个主题。规则类一条一个文件。
- 文件头用 YAML frontmatter：

```markdown
---
name: prefer-chinese-replies
description: 所有回答用中文
type: preference        # profile | preference | rule | project | reference
status: active          # active | superseded
source: claude-code@macbook   # agent@设备
created: 2026-11-13
updated: 2026-11-13
superseded_by:          # 被替代时填新条目 name
---
正文：事实本身。

**Why:** 为什么（用户原话或背景）。
**How to apply:** 什么时候、怎么用。
```

- 相关条目用 `[[name]]` 互链。
- 日期一律写绝对日期，不写"昨天""下周"。

## 各目录

| 目录 | 放什么 | 谁能写 |
|---|---|---|
| `MEMORY.md` | 索引，一行一条：`- [标题](路径) — 一句话` | 用户确认后 agent 写 |
| `profile/USER.md` | 身份、角色、设备清单、常驻项目 | 用户确认后 agent 写 |
| `profile/PREFERENCES.md` | 语言、沟通风格、工具链偏好、禁忌 | 用户确认后 agent 写 |
| `rules/` | 经过验证的工作规则（被纠正过的、被确认有效的） | 用户确认后 agent 写 |
| `projects/` | 跨设备、跨 agent 的项目上下文（不是代码结构，代码里能查到的不写） | agent 可直接写 |
| `log/` | 原始观察，只追加 | agent 随时写 |

## log 格式

文件名：`log/YYYY-MM.<设备名>.md`，按设备分文件避免同步冲突。

```markdown
## 2026-11-13 14:20 · claude-code@macbook
- [preference] 用户要求先给结论再展开
- [correction] 用户纠正：xj 项目的测试环境不是 test2，是 staging
- [candidate-rule] 改禅道数据前必须确认（用户第二次强调）
```

标签：`preference` `correction` `fact` `candidate-rule` `project`。

## 禁止写入

- 密码、API key、token、cookie、私钥
- 客户个人信息、公司内部敏感数据原文
- 能从代码、git 历史、项目文档里直接查到的内容
- 只与当前对话有关的临时信息
