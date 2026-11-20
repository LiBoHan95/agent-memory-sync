# 共享记忆协议

你可以读写一个跨设备、跨 agent 共享的记忆库：`~/memory-vault/`。
其他 agent（Claude Code、Codex、Cursor、Gemini、Pi）在其他设备上也在读写同一份内容。

## 读

- 会话开始时先读 `~/memory-vault/MEMORY.md`（索引）和 `~/memory-vault/profile/PREFERENCES.md`。
- 任务涉及某个项目时，读 `~/memory-vault/projects/<项目名>.md`（如果存在）。
- 记忆反映的是写入时的情况。引用其中的文件、命令、配置前先核实它们还存在。
- `status: superseded` 的条目已经失效，不要依据它行动。
- `projects/` 里没有某个项目页，可能只是没记过，也可能是用户刚删掉；不要凭旧对话把它重新建回来。

## 写

- 发现值得长期记住的信息时，追加到 `~/memory-vault/log/<YYYY-MM>.<设备名>.md`，格式见 `FORMAT.md`。
  设备名用 `hostname -s` 的结果。
- 值得记的：用户的偏好、用户对你的纠正、被用户确认有效的做法、跨会话的项目背景。
- 不值得记的：代码里能查到的、只和本次对话有关的、你自己的推测。
- `profile/`、`rules/`、`MEMORY.md` 只有在用户明确同意后才能修改。用户说"记住……"即视为同意。
- 修改已有条目时更新 `updated` 和 `source`；事实被推翻时标 `status: superseded` 并填 `superseded_by`，不要删除。
- 项目放弃或代码已删除时：只有用户明确说"删掉这个项目的记忆"，才删除 `projects/<name>.md` 并同步删 `MEMORY.md` 的索引行；删除前检查反链。`log/`、`profile/`、`rules/` 不因此删除。原始转录由用户执行 `python3 -m amem.forget --project <项目路径> --dry-run`（确认后加 `--yes`）清除，agent 不要自己动归档。

## 整理提案

服务器每天自动整理记忆。涉及 `profile/`、`rules/` 的变更写在 `~/memory-vault/proposals/` 里等用户确认。
- 会话开始时如果有未处理的提案，用一句话提醒用户，不展开。
- 用户要求处理时，逐条展示，按用户决定应用，并在提案文件里标注每条的结果（accepted / rejected）。

## 禁止

不写入密码、API key、token、cookie、私钥、客户个人信息。发现 vault 里有这类内容，告诉用户。
