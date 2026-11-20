# 转录留存与清除

原始转录不是永久资产，而是分层的：热数据保留给 resume 和调试，冷数据压缩，
对话层长期保留，真正无用的项目按明确指令清除。原则是**先蒸馏、后删除**，
并且只有用户明确要求时才删原始数据。

## 分层

| 层 | 内容 | 位置 | 默认保留 |
|---|---|---|---|
| L0 热数据 | 各 agent 原始转录 | 本机 `~/.claude/projects` 等 | 由各 agent 自己管理；Claude Code 用 `cleanupPeriodDays` |
| L1 原始归档 | 未压缩的原始转录 | 服务器 `transcript-archive/<设备>/<agent>/` | 最近 `AMEM_ARCHIVE_HOT_DAYS` 天（默认不限） |
| L2 压缩归档 | 原始转录的 `.jsonl.zst` | 同上 | 建议 180 天；之后可只保留对话层 |
| L3 对话层 | 只含 user/assistant 文本，zstd | `transcript-archive/conversation/` | 长期 |
| L4 记忆库 | `memory-vault` 的 Markdown | 各设备 + 服务器 | 持续维护 |

对话层通常只有原始体积的 4% 左右，压缩后不到 1%。2026-11-19 实测：
3.0G 归档全量压缩约 360MB；只留对话层再压缩约 14–50MB。工具输出占原始
体积约 54%，源码副本主要就在这里。

## 生成对话层

```bash
python3 -m amem.archive conversation --archive ~/transcript-archive --dry-run
python3 -m amem.archive conversation --archive ~/transcript-archive
```

对话层写到 `conversation/<设备>/<agent>/.../*.md.zst`，dreaming 会跳过这个
目录。原始转录按保留策略删除后，仍可以用它重新提炼；它不是 dreaming 的
输入，避免同一批消息被处理两次。文件头带格式版本标记；旧格式文件在原始
明文还在时会被 `conversation` 自动重建，旧格式也仍能被 `forget` 按 cwd
识别（尽力扫描，见下）。

## 压缩冷数据

先跑一次全量归档，再给客户端设置热窗口：

```bash
# ~/.config/amem/archive.env
AMEM_ARCHIVE_HOT_DAYS=90
```

客户端从此只上传最近 90 天修改过的文件，旧明文不会被下一轮 rsync 重新上传。
服务器上再执行：

```bash
python3 -m amem.archive compact --archive ~/transcript-archive \
  --older-than 180 --vault ~/memory-vault            # 只压缩
python3 -m amem.archive compact --archive ~/transcript-archive \
  --older-than 180 --delete-raw --vault ~/memory-vault  # 校验后删明文
```

`--vault` 在 `--delete-raw` 时把 `.dream-state` 里旧明文路径的偏移迁移成
压缩文件的行状态，避免下次 dreaming 把同一份文件重读一遍。只压缩不删明文
时不需要迁移，明文仍是 dreaming 的输入；`.jsonl.zst` 只在明文删除后读取，
不会和明文重复处理。压缩后会解压并比对 sha256，不一致就保留明文。不传
`--vault` 时默认用 `AMEM_VAULT`（通常是 `~/memory-vault`）；真找不到 vault
也不会丢消息，dreaming 会按旧偏移换算出剩余行重读一次。

`--delete-raw` 会拿与 dreaming 相同的 vault 锁，避免并发改写 `.dream-state`；
临时压缩文件也带进程号，多个 compact 不会互相覆盖。压缩和删除之间会重新
检查明文的 size/mtime：如果 rsync 正好追加了新内容，这些内容还不在 zst 里，
本轮保留明文，下一轮重新压缩。明文按追加式文件处理；检测到前缀与压缩内容
不一致（文件被替换）时会把进度重置为 0，不跳过新内容。

迁移后明文的旧偏移会留在 state 里，不会删掉：客户端 rsync 可能把同一份
明文重新上传（`AMEM_ARCHIVE_HOT_DAYS=0` 时每小时都会），或者 unlink 失败
留下明文。只要旧偏移还在，dreaming 就只读尾部，不会从 0 重放整个会话。
如果 dreaming 已经通过压缩层读过更多行，重新上传的明文会继承这个进度，
再次 compact 也不会把已读行数回滚。压缩文件末尾如果正好截在一条 JSON
记录中间，这一行不计入已消费行数，等追加补全后再解析。

## 按项目清除

```bash
# 默认 dry-run，只报告匹配到的会话和项目页
python3 -m amem.forget --project ~/dev/old-project --vault ~/memory-vault --dry-run

# 本机：删除本地各 agent 的会话
python3 -m amem.forget --project ~/dev/old-project --vault ~/memory-vault --yes

# 服务器：--project 写客户端记录的原路径，不是服务器的 ~/...
ssh server 'cd ~/agent-memory-sync && python3 -m amem.forget \
  --project /Users/vincent/dev/old-project --archive ~/transcript-archive --no-local --yes'
```

匹配依据是会话元数据里的 `cwd`，不是目录名，所以对 Claude Code、Codex、
Pi、DSH 和压缩后的转录都有效。只删除 `cwd` 等于项目路径或位于其下的文件。
归档里的 `cwd` 是**客户端记录时的绝对路径**；在服务器上执行时 `--project`
要写这个原始路径（例如 `/Users/vincent/dev/old-project`），写服务器上的
`~/dev/old-project` 通常匹配不到。多台设备的路径不同时分别执行。

对话层 `conversation/<设备>/<agent>/.../*.md.zst` 也按文件头里的 `cwd`
匹配并一并删除，否则原始转录删掉后它仍能还原 user/assistant 全文。新格式
所有会话头都在第一条消息之前，只有这段文件头里的 `- cwd:` 才算元数据；
消息正文里出现的 `- cwd:` 不会误伤其他项目。旧格式没有版本标记，只能按
相邻的 `- session:`/`- cwd:` 行尽力扫描，正文里恰好出现同样两行时仍可能
误判；原始明文还在时先跑一次 `conversation` 即可重建为新格式。没有 `cwd`
的转录（本机 `~/.claude/transcripts/ses_*.jsonl` 就是这种）无法归属项目，
不会被 `forget` 匹配，需要单独清理。

项目页 `projects/<name>.md` 和 `MEMORY.md` 的索引行只在本机执行并给了
`--vault` 时删除；如果项目页仍有反链，默认保留，加 `--force` 才强制删。
反链包括 `[[name]]`、`[[name|别名]]`、`[[name#小节]]`、`[[projects/name]]`、
`[[name.md]]`，以及 `[文字](projects/name.md)`、带锚点/尖括号/标题的 Markdown
链接和引用式定义 `[文字]: projects/name.md`。反链只查笔记层（`projects/`、
`rules/`、`profile/` 等），不把 `log/` 的原始观察和 `proposals/` 的历史提案
当作阻止删除的理由。

删除清单追加写入 `~/.config/amem/forget.jsonl`，每条包含路径、大小和
sha256；清单在真正 unlink 之前写入并 flush。清单不含文件内容。删除失败
会返回非 0，并追加 `delete-failed` 记录。扫描转录时单个文件读不了（权限、
完全损坏的 zstd、缺 zstd 命令）不会中断整轮，但会提示“扫描失败”并返回
非 0，因为删除计划不完整；DSH 正在写入而留下的尾部半帧不算损坏，会返回
已经读到的行。

## 删除的边界

- 只删 `projects/` 下的项目页；`profile/`、`rules/` 仍走提案。
- 拒绝 HOME、根目录和过浅路径，以及 HOME 下常见的工作区名（`dev`、
  `workspace`、`projects`、`work` 等，大小写不敏感）；`--force` 可覆盖。
  嵌套工作区（如 `~/work/oss`）仍需人工判断，脚本不猜。
- `log/` 不删，它是原始观察层。
- 不做自动推断：目录不存在、很久没更新都不算放弃；只认明确指令。
- 对话层对单条 assistant 文本只保留前 2000 字符；需要逐字全文时看原始
  `.jsonl.zst`，不要先删压缩层。
- 原始归档不在 git 里，删了就没了。需要留证据时先跑 `conversation`，
  或先提炼规则到 `rules/`。
- 删掉的 vault 项目页仍会留在 git 历史里；真删除要另做历史清理。
- 真删除不是瞬时完成：服务器备份、Syncthing 版本、磁盘快照可能还有副本。
- 转录里出现过密钥或密码时，先轮换凭据再删；删除不能替代轮换。
- 目前没有自动清理压缩层的命令：`compact` 只处理明文，DSH 自带压缩和
  已生成的 `.jsonl.zst` 需要手工按保留期删除；自动 prune 在 roadmap 里。

## 为什么这样做

- Mem0 默认只存抽取后的事实，不存逐字转录；Claude Code 本地转录默认
  30 天删除；Anthropic API 默认 30 天保留。原文短期保留是行业常态。
- Cognee 把原始数据和派生记忆的删除分开（`memory_only` / `prune_data`）。
- RSPM（《Let Them Sleep》）把失败聚类成规则后删除原始 episode。
- Compact-Bench 实测机械抽取保留 79.1% 的反复出现细节，LLM 摘要最多
  16%。所以长期层存机械抽取的对话文本，不存模型摘要。

## 体积统计

```bash
python3 -m amem.archive stats --archive ~/transcript-archive
```
