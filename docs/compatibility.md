# 资产与长期兼容

## 什么是资产，什么是外壳

| 部分 | 类别 | 能否再生 | 寿命预期 |
|---|---|---|---|
| 原始会话转录（`transcript-archive/`） | 资产 | 不能 | 分层保留；用户可显式清除 |
| 对话层（`transcript-archive/conversation/`） | 资产 | 能从原始转录重建，但有损 | 长期保留；项目清除时一并删除 |
| 整理后的笔记（vault 的 `profile/` `rules/` `projects/`） | 资产 | 能从转录重新提炼，但有损 | 持续维护 |
| 格式约定（`FORMAT.md`） | 资产的说明书 | — | 随 schema 版本演进 |
| 同步层（Syncthing、rsync、git） | 外壳 | — | 可随时替换 |
| agent 接入（各 agent 指令文件里的一两行） | 外壳 | — | agent 换代时重写 |
| 协议文字（`AGENT_PROTOCOL.md`） | 外壳 | — | 模型变强后精简 |
| 检索层（mem0 / Hindsight） | 外壳 | 能从 vault 重建 | 随时可丢弃 |

原则：外壳坏了只需重写外壳，资产不受影响。任何外壳都不能成为某条信息的唯一存放处。

## 两层资产的关系

整理后的笔记是当下模型按当下判断提炼的，有损。转录是 ground truth（Anthropic《The components of agent memory》用转录校验记忆）。

未来模型更强时，可以从转录重新提炼出更好的笔记；反过来不行。所以转录的长期价值可能高于笔记，必须单独归档，见 `transcript-archive.md`。

但“归档”不等于“永久保留全量”：原始转录按热窗口、压缩、对话层分级保留，项目不再需要时由用户明确指令清除。对话层只含 user/assistant 文本，压缩后体积很小，用来保证删除原始工具输出后仍能重新提炼；单条 assistant 文本截到 2000 字符，逐字全文仍需原始压缩层。完整策略见 `retention.md`。

## 格式兼容规则

1. **每个文件标 `schema_version`。** 当前为 `1`。
2. **只增不改。** 可以加字段；不重命名字段，不改已有字段的含义，不改已有取值的含义。
3. **破坏性变更走迁移。** 必须改时：升 `schema_version`，同时提交 `scripts/migrate/v<N>-to-v<N+1>.py`，由脚本批量转换并提交 git，不靠人工逐个改。
4. **正文能脱离 frontmatter 独立读懂。** 所有工具都不认这些字段时，文字本身仍然有用。
5. **只用通用语法。** 标准 Markdown + YAML frontmatter。`[[name]]` 链接退化成普通文字也能读懂，允许使用；不引入只有某个工具能解析的语法。
6. **转录原样保存。** 保留期内不改写、不转换格式；压缩必须能解压回字节一致的内容。各 agent 的转录格式会变，解析器放在 `amem/parsers/`，按 agent 和版本区分，原文件不动。

## 外壳替换预案

| 场景 | 处理 |
|---|---|
| 换掉 Syncthing | vault 是普通文件夹，换任何同步工具；git 历史在服务器上不受影响 |
| 某个 agent 停用 / 新 agent 出现 | 在 `agent-integration.md` 增删一节，`install.sh` 增删一段 |
| agent 改了配置文件位置 | 同上 |
| 检索层过时 | 删掉，从 vault 重建新的 |
| 服务器更换 | 拷贝 `~/memory-vault`（含 `.git`）和 `~/transcript-archive` |
