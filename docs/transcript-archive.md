# 转录归档

## 为什么必须做

- 转录是唯一无法再生的资产（见 `compatibility.md`）。
- Claude Code 未设置 `cleanupPeriodDays` 时，默认在启动时删除 30 天前的转录（按文件 mtime），不提示、不进回收站。见 anthropics/claude-code issue #62250、#62476、#64999。
- 本机（2026-11-13）检查：`~/.claude/settings.json` 没有 `cleanupPeriodDays`，处于默认状态。
- 设备丢失或更换时，本地转录随之丢失。

## 本机来源与体积（2026-11-13 实测）

| agent | 路径 | 体积 |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`（702 个） | 目录共 2.5G，jsonl 占比待测 |
| Claude Code | `~/.claude/transcripts/` | 1.1M |
| Codex | `~/.codex/sessions/`、`~/.codex/archived_sessions/` | 14M |
| DeepSeek Harness | `~/.dsh/sessions/**/*.jsonl.zstd` | 17M |
| Pi | `~/.pi/agent/sessions/` | 5.6M |
| Gemini、Cursor | 待确认 | — |

## 设计

```
设备                                     服务器
~/.claude/projects/**/*.jsonl  ──rsync──▶ ~/transcript-archive/<设备>/claude-code/
~/.claude/transcripts/         ──rsync──▶ ~/transcript-archive/<设备>/claude-code/transcripts/
~/.codex/sessions/             ──rsync──▶ ~/transcript-archive/<设备>/codex/
~/.codex/archived_sessions/    ──rsync──▶ ~/transcript-archive/<设备>/codex/archived_sessions/
~/.dsh/sessions/               ──rsync──▶ ~/transcript-archive/<设备>/dsh/
~/.pi/agent/sessions/          ──rsync──▶ ~/transcript-archive/<设备>/pi/
```

- **单向；客户端只增。** 用 `rsync`，不带 `--delete`。本地被 agent 清理掉的文件在服务器上保留；服务器再按热窗口/压缩策略处理旧文件。不用 Syncthing，因为它会把删除同步过去。
- **与 vault 分开。** 归档不同步回任何设备，不注入任何 agent。只有服务器上的 dreaming 和重新提炼会读它。
- **热窗口内原样保存。** 超过 `AMEM_ARCHIVE_HOT_DAYS` 的文件不再重复上传，由服务器压缩或清理；压缩会解压比对 sha256，保证内容不变。分层与清除见 [retention.md](retention.md)。
- **定时运行。** macOS 用 launchd 每小时一次（`scripts/archive-transcripts.sh` + `scripts/launchd/`；`scripts/install.sh` 会渲染并加载 plist，Linux 写入 cron）。
- **归档目标。** 写在 `~/.config/amem/archive.env`（`AMEM_ARCHIVE_HOST=user@server`，或直接用 `AMEM_ARCHIVE_TARGET` 指定本地/远端路径）。本地相对路径按运行脚本时的目录解析，不会写进源转录目录。首次全量上传前可先 `scripts/archive-transcripts.sh --dry-run`。
- **本地兜底。** `install.sh` 把 Claude Code 的 `cleanupPeriodDays` 设为 `36500`。注意 issue #90148：项目级 `.claude/settings.json` 里的该值会覆盖全局清理行为，install 时一并检查。想释放本机空间时，把它改成 90–180 天，并确认不再需要 resume 更早的会话；`check-agents.sh` 会把任何自定义正整数视为有效值。需要让 install/check 统一按某个值执行时，设置 `AMEM_CLEANUP_DAYS`。

## 敏感信息

转录里一定有密钥和公司数据（agent 读过的配置文件、命令输出都在里面）。

选择：**原样加密存储，在出口处拦截**，而不是上传前脱敏。

- 脱敏会改写 ground truth，而且脱敏规则会漏。
- 归档目录权限 `700`，所在磁盘加密（LUKS）或用 `age` 按月打包加密。
- 出口拦截：任何从转录提炼出、准备写入 vault 的内容，提交前都过一遍 `gitleaks`，命中则拒绝写入并报警。vault 会同步到所有设备，是真正需要保持干净的地方。

待决定：服务器是否可信到能存公司数据原文。不可信时，改为上传前用 `gitleaks` 检测并跳过命中的会话。

## 压缩与保留

原文不是永久保留：建议设置 90 天热窗口，之后服务器把冷文件压成
`.jsonl.zst`，长期保留只含 user/assistant 文本的对话层（单条 assistant
截到 2000 字符）；不再需要的项目用 `python3 -m amem.forget` 按 cwd 清除。
完整分层、命令和边界见 [retention.md](retention.md)。
