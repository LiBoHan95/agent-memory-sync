# agent 接入

目标：每个 agent 启动时都加载 `~/memory-vault/AGENT_PROTOCOL.md`，并能读写 `~/memory-vault/`。

支持文件引用的 agent 用引用；不支持的用一行指示让 agent 自己去读。

## Claude Code

`~/.claude/CLAUDE.md` 末尾加：

```markdown
@~/memory-vault/AGENT_PROTOCOL.md
@~/memory-vault/MEMORY.md
```

Claude Code 自带的 auto memory（`~/.claude/projects/*/memory/`）是按项目、按设备的，与共享 vault 并存：项目内的细节留在 auto memory，关于用户本人的写入 vault。迁移时把现有 auto memory 里关于用户的条目搬进 vault。

访问权限：vault 在项目目录外，需要在 `~/.claude/settings.json` 的 `permissions.additionalDirectories` 加 `~/memory-vault`，否则每次读写都要确认。

## Codex

`~/.codex/AGENTS.md`（当前为空文件）写入：

```markdown
开始任何任务前，先读取并遵守 ~/memory-vault/AGENT_PROTOCOL.md，再读 ~/memory-vault/MEMORY.md。
```

Codex 的 AGENTS.md 不支持 `@` 引用，所以用指示句。sandbox 模式下需确认 `~/memory-vault` 可写（`config.toml` 的 `sandbox_workspace_write.writable_roots`）。

## Gemini CLI

`~/.gemini/GEMINI.md`（不存在则新建）：

```markdown
@~/memory-vault/AGENT_PROTOCOL.md
@~/memory-vault/MEMORY.md
```

## Pi

`~/.pi/agent/AGENTS.md`（不存在则新建），内容同 Codex 的指示句。

## Cursor

Cursor 没有全局指令文件。在 Settings → Rules → User Rules 里填入 Codex 那句指示句。

## 其他 agent

只要能读本地文件并有全局指令入口，都按"指示句"方式接入。

## 待验证

以下细节在本机未实测，接入时逐一确认：

- [ ] Claude Code `@~/` 路径引用是否展开 `~`
- [ ] Codex sandbox 下写 `~/memory-vault` 的配置项名称
- [ ] Gemini CLI 全局 GEMINI.md 的 `@` 引用
- [ ] Pi 是否读取 `~/.pi/agent/AGENTS.md`
- [ ] 每个 agent 新开会话后问"我是谁、我偏好什么"，能否答对

验证脚本计划放在 `scripts/check-agents.sh`。
