# 只读 Web

服务器上的查看界面：看各设备的同步/活动历史、转录归档状态和记忆文件。**只读**，不提供编辑、接受提案或删除入口。

默认只监听 `127.0.0.1:8787`，通过 SSH 隧道访问；不设计成公网服务。

## 能看什么

| 路径 | 内容 |
|---|---|
| `/` | 总览：笔记数、待处理提案、最近 dreaming、设备和归档汇总、最近提交/报告 |
| `/memory` | `MEMORY.md` 渲染结果，以及 `profile/`、`rules/`、`projects/` 下的笔记列表 |
| `/memory/<路径>` | 单篇笔记；`?raw=1` 查看原文 |
| `/devices` | 每台设备的最近 log、归档文件数/体积/最后归档时间 |
| `/devices/<设备>` | 单台设备的 log 时间线和归档明细 |
| `/history` | vault 的 git 提交列表（读完即走，不写仓库） |
| `/history/<hash>` | 单次提交的 diff |
| `/reports` | dreaming 报告列表 |
| `/reports/<文件名>` | 报告正文；`?raw=1` 查看原文 |
| `/proposals` | 整理提案列表与待处理数量 |
| `/proposals/<文件名>` | 提案正文；`?raw=1` 查看原文 |
| `/healthz` | 健康检查，返回 `ok`，不需要令牌 |

“同步历史”在 Web 里是**设备活动历史**：vault 里按设备分开的 `log/YYYY-MM.<device>.md`，加上归档目录的新旧程度。它不读取 Syncthing 的传输记录（那需要 Syncthing REST API），也不展示转录内容。

## 本机试用

```bash
cd ~/agent-memory-sync
scripts/web.sh --vault ~/memory-vault --archive ~/transcript-archive
# 打开 http://127.0.0.1:8787/
```

`--reports` 默认是 vault 同级的 `dream-reports/`。按 `Ctrl-C` 停止。

## 配置

| 参数 | 环境变量 | 默认值 |
|---|---|---|
| `--vault` | `AMEM_VAULT` | `~/memory-vault` |
| `--archive` | `AMEM_ARCHIVE` | `~/transcript-archive` |
| `--reports` | `AMEM_REPORTS` | `<vault 的父目录>/dream-reports` |
| `--host` | `AMEM_WEB_HOST` | `127.0.0.1` |
| `--port` | `AMEM_WEB_PORT` | `8787` |
| `--token` | `AMEM_WEB_TOKEN` | 无 |

监听非回环地址时必须设置令牌，否则 `amem.web` 拒绝启动。

## 服务器部署（systemd 用户单元）

在服务器上、用拥有 vault 的普通用户执行：

```bash
mkdir -p ~/.config/systemd/user ~/.config/amem
cp ~/agent-memory-sync/scripts/systemd/amem-web.service.template \
   ~/.config/systemd/user/amem-web.service

# 生成令牌；web.env 只放敏感配置，权限 600
printf 'AMEM_WEB_TOKEN=%s\n' "$(openssl rand -hex 24)" > ~/.config/amem/web.env
chmod 600 ~/.config/amem/web.env

systemctl --user daemon-reload
systemctl --user enable --now amem-web
loginctl enable-linger "$USER"   # 未登录时也保持运行
```

单元文件假定仓库在 `~/agent-memory-sync`、vault 在 `~/memory-vault`。放在别处就改 `WorkingDirectory`、`ExecStart` 和 `AMEM_VAULT`/`AMEM_ARCHIVE`/`AMEM_REPORTS`；`AMEM_REPORTS` 要与 dreaming 的报告目录一致（默认是 vault 同级的 `dream-reports/`）。

单元里设置了 `ProtectHome=read-only` 和 `ProtectSystem=strict`，从 OS 层限制服务只能读、不能写 vault。

查看状态和日志：

```bash
systemctl --user status amem-web
journalctl --user -u amem-web -f
```

## 访问

SSH 隧道（推荐）：

```bash
ssh -N -L 8787:127.0.0.1:8787 user@server
# 本机浏览器打开 http://127.0.0.1:8787/
```

第一次访问带令牌：

```
http://127.0.0.1:8787/?token=<AMEM_WEB_TOKEN>
```

服务校验后会写入 `HttpOnly; SameSite=Strict` 的 cookie 并重定向到去掉令牌、保留其余查询参数的地址。也可以直接用请求头：

```bash
curl -H "Authorization: Bearer $AMEM_WEB_TOKEN" http://127.0.0.1:8787/
```

令牌建议用 `openssl rand -hex 24` 生成。令牌只能包含 ASCII 可见字符，且不能含空白、引号、逗号、分号或反斜杠（保证能安全写入 cookie）；若含 `+`、`&`、`#` 等字符，放进 URL 前需要 percent-encode，否则改用 `Authorization` 头。

cookie 没有加 `Secure`，因为默认访问地址是 `http://127.0.0.1`；如果通过 HTTPS 反向代理访问，可以在反向代理层补上这个属性。

未设置令牌时，服务只接受 Host 为回环地址（`127.0.0.1`、`localhost`、`::1`）的请求，避免 DNS rebinding 读取 vault；设置令牌后不再限制 Host。

Tailscale 用户可以用 `tailscale serve --bg 8787` 把服务只暴露给 tailnet；这是可选的，必须同时设置令牌。不要把端口直接映射到公网，也不要在前面加无认证的反向代理。

## 安全边界

- 只实现 `GET`/`HEAD`；`POST`/`PUT`/`DELETE`/`PATCH` 返回 405。
- 默认回环监听；非回环地址强制令牌。
- 笔记、报告、提案路径都限制在各自根目录内；拒绝 `..`、绝对路径、符号链接和越界解析。
- 不读取、不展示 `~/.claude/projects`、`~/.codex/sessions` 等原始转录；只统计归档目录的文件数、体积和 mtime。
- 所有页面先做 HTML 转义；Markdown 渲染器不允许原始 HTML，只放行 `http`/`https`/`mailto`/`tel` scheme 和相对链接，拒绝 `javascript:`、`data:`、`file:`、反斜杠和控制字符。
- 响应带 CSP（`default-src 'none'`）、`X-Content-Type-Options: nosniff`、`Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
- 文件读取上限 512 KB；log 只读最近 512 KB，页面会标注截断；提交 diff 上限 256 KB。仪表盘的 vault 校验会跳过超过 512 KB 的文件，归档统计缓存 60 秒，避免每次请求都扫描大目录。
- 令牌是单一共享密钥，没有多用户和逐人审计。令牌泄露就换掉并重启服务。

## 只读的边界

Web 进程本身不做任何写操作，但“只读”是功能层面而不是权限层面的保证：如果不用 systemd 的 `ProtectHome=read-only`，进程仍以你的用户身份运行，拥有该用户对 vault 的写权限。部署时保留单元模板里的保护项。

提案的接受/拒绝不在 Web 里做：回到会话中让 agent 处理，或直接编辑提案文件。

## 排错

| 现象 | 检查 |
|---|---|
| 启动报“vault 不存在” | `--vault`/`AMEM_VAULT` 指向 Syncthing 已同步的目录 |
| 设备归档全为 0 | `AMEM_ARCHIVE` 路径不对，或归档任务还没首次上传 |
| 历史为空 | vault 不是 git 仓库，或还没有提交 |
| 401 | 带 `?token=...` 首次访问，或设置 `Authorization: Bearer` |
| 端口被占用 | `--port` 换一个，或 `lsof -i :8787` |
| 报告页 404 | `AMEM_REPORTS` 指向 dreaming 的 `Config.report_dir`（默认 `<vault 父目录>/dream-reports`） |
