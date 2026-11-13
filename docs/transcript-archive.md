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
| Pi | `~/.pi/agent/sessions/` | 5.6M |
| Gemini、Cursor | 待确认 | — |

## 设计

```
设备                                     服务器
~/.claude/projects/**/*.jsonl  ──rsync──▶ ~/transcript-archive/<设备>/claude-code/
~/.codex/sessions/             ──rsync──▶ ~/transcript-archive/<设备>/codex/
~/.pi/agent/sessions/          ──rsync──▶ ~/transcript-archive/<设备>/pi/
```

- **单向、只增。** 用 `rsync`，不带 `--delete`。本地被 agent 清理掉的文件在服务器上保留。不用 Syncthing，因为它会把删除同步过去。
- **与 vault 分开。** 归档不同步回任何设备，不注入任何 agent。只有服务器上的 dreaming 和重新提炼会读它。
- **原样保存。** 不改写，不转换格式。
- **定时运行。** macOS 用 launchd 每小时一次（`scripts/archive-transcripts.sh` + `scripts/launchd/*.plist`，待写）。
- **本地兜底。** `install.sh` 把 Claude Code 的 `cleanupPeriodDays` 设为 `36500`。注意 issue #90148：项目级 `.claude/settings.json` 里的该值会覆盖全局清理行为，install 时一并检查。

## 敏感信息

转录里一定有密钥和公司数据（agent 读过的配置文件、命令输出都在里面）。

选择：**原样加密存储，在出口处拦截**，而不是上传前脱敏。

- 脱敏会改写 ground truth，而且脱敏规则会漏。
- 归档目录权限 `700`，所在磁盘加密（LUKS）或用 `age` 按月打包加密。
- 出口拦截：任何从转录提炼出、准备写入 vault 的内容，提交前都过一遍 `gitleaks`，命中则拒绝写入并报警。vault 会同步到所有设备，是真正需要保持干净的地方。

待决定：服务器是否可信到能存公司数据原文。不可信时，改为上传前用 `gitleaks` 检测并跳过命中的会话。

## 压缩与保留

- 每月把上月目录用 `zstd` 压缩（jsonl 压缩率通常很高）。
- 永久保留。
