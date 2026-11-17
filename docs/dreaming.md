# dreaming：自动整理记忆

来源：Anthropic《The components of agent memory》。会话中判断"什么值得记"不可靠，需要离线回放来纠错、合并、提升。

## 能自动到什么程度

按风险分三级。低风险的全自动，高风险的自动生成提案、等人确认。

| 级别 | 操作 | 方式 |
|---|---|---|
| A 全自动 | 重建 `MEMORY.md` 索引；给已处理的 log 条目标 `[done]`；合并 log 中的重复条目；修复断链；`schema_version` 迁移 | 直接提交 git |
| B 自动 + 可撤销 | 更新 `projects/` 的项目背景；把过期事实标 `superseded`（有转录或新 log 作证据时） | 直接提交，每次一个独立 commit；周报列出，`git revert` 一键撤销 |
| C 需确认 | 新增或修改 `profile/`、`rules/` | 写入 `proposals/`，用户确认后应用 |

C 级不自动的原因：这两个目录会注入每个 agent 的每次会话，错一条就会被所有 agent 信任并放大。文章里 Pokémon 的例子就是一条错误笔记导致反复失败。

试用一段时间后，如果提案的接受率很高（例如连续 4 周超过 90%），可以把部分 C 级放到 B 级。

## 输入

- `log/*.md`：上次运行后新增的条目
- `transcript-archive/`：上次运行后新增的转录，用来核实 log 和找出会话中没被记下的偏好（见 `transcript-archive.md`）
- 当前的 `profile/` `rules/` `projects/` `MEMORY.md`

## 流程

1. **收集**：取上次运行（记录在 `.dream-state`）之后的新 log 和新转录。都为空则退出。
2. **解析**：用 `amem/parsers/` 把各 agent 的转录转成统一的"用户说了什么、agent 做了什么"文本，只看用户消息和纠正。
3. **提炼**：headless agent 读取材料，对照现有笔记，产出 A/B/C 三类变更。
4. **校验**：
   - 所有改动过 `gitleaks`，命中则丢弃该条并记入报告。
   - frontmatter 字段检查，不符合 `FORMAT.md` 的丢弃。
   - `MEMORY.md` 超 200 行则回到第 3 步，要求下沉细节。
5. **应用**：A 级一个 commit，B 级每条一个独立 commit（message 以 `dream:` 开头）；C 级写 `proposals/YYYY-MM-DD.md`。
6. **报告**：写到 `~/dream-reports/YYYY-MM-DD-HHMMSS.md`（可用 `Config.report_dir` 改），包含本次摘要、A/B/C/拒绝明细和 agent 输出末尾。

## 确认提案

任意设备上对 agent 说"看一下记忆提案"。agent 按协议读取 `proposals/` 里最新的未处理文件，逐条展示，用户接受或拒绝，agent 应用接受的条目，并在提案文件里标注结果。被拒绝的条目也是信号：dreaming 下次运行时会读到，避免重复提同一条。

## 运行

服务器 cron，每天一次：

```bash
30 4 * * * ~/agent-memory-sync/scripts/dream.sh
```

`scripts/dream.sh` 先检查 `gitleaks`、`claude`、vault 和归档目录，再调用 `python3 -m amem.dream`；日志默认写 `~/dream.log`，失败时尝试 `notify-send` / `osascript` / `logger` 通知。

- agent 的工作目录是 vault 的临时副本，增量材料在 `input.md`；agent 改不到真实 vault。
- 不给 agent Bash 权限；git 提交、gitleaks 校验由 `amem.gate` 在 agent 结束后执行。
- 可用 `AMEM_AGENT_CMD` 换成 `codex exec` 等，dreaming 不依赖特定 agent。

## 成本

- 只处理增量。
- 转录先经解析器过滤，只保留用户消息和 agent 被纠正前后的片段，体积通常降到原来的几十分之一。
- 用便宜的模型做提炼；C 级提案量少，可以用强模型复核。

## 待补

- DeepSeek Harness 的转录是 zstd 压缩（`*.jsonl.zstd`），标准库不能直接读，解析器暂未实现。
