# dreaming：离线整理记忆

来源：Anthropic《The components of agent memory》。会话中判断"什么值得记"不可靠，需要离线回放日志来纠错、合并、提升。

## 输入

- `log/*.md`：各 agent 在会话中追加的原始观察
- `profile/` `rules/` `projects/`：当前的笔记
- （可选）会话转录：Claude Code 在 `~/.claude/projects/*/*.jsonl`，Codex 在 `~/.codex/sessions/`。转录不同步到服务器，dreaming 第一版只用 log。

## 输出

一份提案文件 `proposals/YYYY-MM-DD.md`，内容是对笔记层的 diff：

- **提升**：log 里反复出现（≥2 次、或用户明确说"记住"）的 preference / candidate-rule → 写进 `profile/` 或 `rules/`
- **纠错**：log 里的 correction 与现有笔记矛盾 → 修改笔记，旧内容标 superseded
- **合并**：重复或高度重叠的条目
- **下沉**：`MEMORY.md` 超过 200 行时，把细节移到子文件，索引只留链接
- **清理**：已处理的 log 条目标记 `[done]`（不删除）

## 确认

dreaming 不直接改笔记层。提案生成后：

1. 用户在任意设备上打开提案，逐条接受或拒绝（或让 agent 代为应用）。
2. 应用后提交 git。

原因：未经校验的记忆会被后续所有 agent 信任并放大错误。

## 运行方式

服务器 cron，每周一次：

```bash
0 3 * * 1 ~/agent-memory-sync/scripts/dream.sh
```

`dream.sh`（待写）调用一个 headless agent（如 `claude -p` 或 `codex exec`），输入 `docs/dreaming.md` 作为指令，vault 作为工作目录，只允许写 `proposals/`。

## 成本控制

- 只处理上次 dreaming 之后新增的 log。
- 用便宜模型。
- log 为空时跳过。
