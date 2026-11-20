# 边缘代码接口

核心代码在 `amem/`（格式库、闸门、dreaming 主流程、Claude Code 解析器），Python 侧只依赖 ≥3.10 标准库；读取 `.jsonl.zst` / `.jsonl.zstd` 和 DSH 转录需要外部 `zstd` 命令（v1.5+）。
以下边缘代码交给其他模型实现，必须遵守这里的约定。不要修改 `amem/format.py` 和 `amem/gate.py`；需要改的话先提出来。

验收：`python3 -m unittest discover -s tests -t .` 全部通过，新代码附带测试。

## 1. 转录解析器 `amem/parsers/<agent>.py`

参考 `amem/parsers/claude_code.py`，实现 `amem/parsers/base.py` 里的 `Parser` 协议：

- `agent` 属性与归档目录名一致：`codex`、`pi`、`gemini`……
- `parse(path, offset, device=, rel=)`：从偏移继续，只消费完整记录，返回 `(list[Message], 新偏移)`；文件变短时从 0 开始。明文 `offset` 是字节偏移；压缩转录的 `offset` 是**已消费的完整行数**（`amem.parsers.base.read_lines` 统一处理）。
- 只输出用户亲手输入的消息（`role="user", origin="human"`）和 agent 的文字回复（`role="assistant"`）。
  脚本/SDK 发来的提示词标 `origin="automated"`。工具调用、工具结果、系统注入、思考过程一律丢弃。
- `cwd` 含 `DREAM_WORKDIR_MARK` 的会话必须跳过（那是 dreaming 自己的会话）。
- 在 `amem/parsers/__init__.py` 的 `PARSERS` 里注册。
- 先用真实转录统计字段分布再写过滤规则（做法见 `claude_code.py` 文件头注释），测试用构造的样例，不要把真实转录放进仓库。

本机位置：Codex `~/.codex/sessions/`、Pi `~/.pi/agent/sessions/`。

## 2. 筛选器 `amem/screen_<name>.py`（可选，如 TypeSafe Jev）

实现 `amem/screen.py` 的 `Screener`：只能删减消息，不能改写；`name` 带固定的模型版本；
调用失败时全部放行（降级为 PassThrough），不能让 dreaming 失败。它不是安全边界。

## 3. `scripts/archive-transcripts.sh` + launchd/cron

- rsync 单向推到 `server:~/transcript-archive/<hostname -s>/<agent>/`，用 `rsync -a`，**不加 `--delete`**，也不加 `--ignore-existing`（转录会持续追加，变长的文件要重新同步）。
- 源：`~/.claude/projects/` → `claude-code/`，`~/.claude/transcripts/` → `claude-code/transcripts/`，`~/.codex/sessions/` → `codex/`，`~/.codex/archived_sessions/` → `codex/archived_sessions/`，`~/.dsh/sessions/` → `dsh/`，`~/.pi/agent/sessions/` → `pi/`。
- `AMEM_ARCHIVE_HOT_DAYS=N` 时只上传最近 N 天修改过的文件；旧文件由服务器的 `amem.archive compact` 压缩/清除，避免明文被反复上传。默认 0 = 全部上传。
- 目标从 `~/.config/amem/archive.env` 或环境变量读取（`AMEM_ARCHIVE_HOST` / `AMEM_ARCHIVE_TARGET`）。
- 每小时一次：macOS 由 `install.sh` 渲染并加载 `scripts/launchd/io.github.amem.archive.plist`，Linux 写入 cron。失败只记日志，不弹窗。
- 详见 `docs/transcript-archive.md` 和 `docs/retention.md`。

## 4. `scripts/dream.sh`（服务器 cron 入口）

```sh
cd ~/agent-memory-sync && python3 -m amem.dream --vault ~/memory-vault --archive ~/transcript-archive
```

- 运行前检查 `gitleaks` 和 `claude` 在 PATH 上；缺 gitleaks 时 `amem.dream` 本身也会拒绝写回。
- 失败时尝试 `notify-send` / `osascript` / `logger` 发通知。报告在 `~/dream-reports/`，日志默认 `~/dream.log`。
- 环境变量：`AMEM_VAULT`、`AMEM_ARCHIVE`、`AMEM_AGENT_CMD`（默认 `claude -p {prompt} ...`，`{prompt}` 必须是独立参数）、`AMEM_AGENT_TIMEOUT`、`AMEM_BUDGET_CHARS`。
- 必须和每小时自动提交错开（例如自动提交在整点，dreaming 在 04:30）。

## 5. 服务器部署

- Syncthing 与 `.stignore`：按 `docs/setup-server.md`。
- `scripts/autocommit.sh`：每小时 `git add -A && git commit`，提交信息前缀 `auto:`（dreaming 用 `dream:`）。
- vault 初始化：复制 `vault-template/`，`git init`。

## 6. `scripts/install.sh`（新设备接入）

幂等，可重复运行，改任何配置文件前先备份为 `<文件>.bak.<日期>`。

- 检查 `~/memory-vault` 存在（Syncthing 已同步）；`--vault` 必须是绝对路径，含空格时给出警告。
- 默认只接入本机已安装的 agent；`--agents auto` 只写已安装的，`--agents all` 或显式列表会为未安装的 agent 创建配置目录并写入。
- 各 agent 的指令文件里加入带 `<!-- amem:begin -->` / `<!-- amem:end -->` 标记的协议引用，具体写法见 `docs/agent-integration.md`；已存在且指向当前 vault 就跳过，换路径时移除旧块和旧格式引用后重写。标记不配对时拒绝改动并提示手工修复。
- 客户端 Agent 页复用同一套标记和路径约定；关闭接入时只移除 amem 生成的块或旧格式引用。
- `~/.claude/settings.json` 设 `cleanupPeriodDays`（默认 36500，可用 `AMEM_CLEANUP_DAYS` 指定），并把 vault 加入 `permissions.additionalDirectories`（用 JSON 解析修改，不要 sed）；开启时清掉其他 amem vault 的旧权限目录。
- 归档脚本就绪后安装归档定时任务（macOS launchd / Linux cron）；归档目标由 `~/.config/amem/archive.env` 提供。
- 不读取、不复制任何 agent 配置里的密钥。

## 7. `scripts/check-agents.sh`

只读。逐个 agent 检查：是否已引用协议、vault 是否可写、Claude Code 的 `cleanupPeriodDays`、归档任务最近一次成功时间。输出一张表。未显式设置 `AMEM_CLEANUP_DAYS` 时，自定义的正整数清理天数视为有效；设置后要求精确匹配。
