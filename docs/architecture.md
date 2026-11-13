# 架构

## 要解决的问题

多地点、多设备、每台设备多个 agent。切换 agent 后，新 agent 不知道用户是谁、偏好是什么。

根因：每个 agent 各自维护记忆（`~/.claude/projects/*/memory`、`~/.codex/memories`……），互不相通，也不跨设备。

## 分层

参考 Anthropic《The components of agent memory》（2026-11-06）的记忆层级：

| 层 | 内容 | 加载方式 | 对应文件 |
|---|---|---|---|
| 注入层 | 索引 + 最关键的偏好 | 每个会话启动时自动注入 | `MEMORY.md`（≤200 行） |
| 笔记层 | 用户画像、偏好、规则、项目上下文 | agent 按需读取 | `profile/` `rules/` `projects/` |
| 素材层 | 会话中随手记下的观察 | 只追加，dreaming 时读取 | `log/YYYY-MM.md` |
| 检索层（可选） | 对以上所有内容的语义索引 | MCP 工具调用 | mem0 / Hindsight |
| 归档层 | 原始会话转录，原样保存 | 不注入；只供 dreaming 和未来重新提炼 | 服务器 `~/transcript-archive/` |

资产与外壳的划分、长期兼容规则见 `compatibility.md`。

## 拓扑

```
            ┌──────────── 服务器（常在线）────────────┐
            │  ~/memory-vault   (Syncthing 节点)      │
            │  git 每小时自动提交（历史 + 灾备）       │
            │  ~/transcript-archive (rsync 单向只增)  │
            │  [三期] 检索服务 MCP                     │
            │  [二期] dreaming cron                    │
            └───────▲───────────────▲─────────────────┘
                    │ Syncthing     │ Syncthing
                    │ + rsync 转录  │ + rsync 转录
         ┌──────────┴───┐     ┌─────┴────────┐
         │ 设备 A        │     │ 设备 B        │
         │ ~/memory-vault│     │ ~/memory-vault│
         │ Claude Code   │     │ Codex         │
         │ Codex, Cursor │     │ Gemini, Pi    │
         └───────────────┘     └───────────────┘
```

## 设计决定

1. **Markdown 是事实来源。** 人能直接改，git 有历史，任何 agent 都能读，不绑定某个厂商的抽取格式。
2. **Syncthing 负责同步，git 负责历史。** 只用 git 需要每台设备记得 pull/push，多设备切换必然分叉。Syncthing 持续双向同步，服务器作为常在线中转。git 只在服务器上自动提交，不在客户端操作。
3. **一份协议，多处引用。** `AGENT_PROTOCOL.md` 放在 vault 里随同步分发，各 agent 的全局指令文件只引用它，改一处全部生效。
4. **分级写入。** agent 在会话中只往 `log/` 追加；提升到 `profile/` `rules/` 需要用户明确确认，或经 dreaming 提案后确认。避免一次误判被所有 agent 永久信任。
5. **每条记忆有来源和日期。** 方便排错和 dreaming 回溯。
6. **转录单独归档。** 转录是唯一不能再生的资产，单向 rsync 到服务器永久保存，不同步回设备（见 `transcript-archive.md`）。
7. **自动整理按风险分级。** 低风险变更由 dreaming 自动提交，`profile/` `rules/` 的变更需确认（见 `dreaming.md`）。
8. **过期标记而不是删除。** 用 `superseded` 标记保留历史，避免旧事实与新事实并列。

## 冲突处理

- 绝大多数写入是 `log/` 的追加，按设备分文件（`log/2026-11.macbook.md`）可完全避免冲突。
- `profile/` `rules/` 的修改频率低，偶发冲突时 Syncthing 生成 `*.sync-conflict-*` 文件；dreaming 或人工合并。

## 不做什么

- 不把完整会话转录注入上下文（转录只归档）。
- 不让每个 agent 维护独立记忆再合并。
- 第一期不上向量数据库。
- 不存任何密钥、token、客户数据（见 `memory-format.md` 的禁止项）。
