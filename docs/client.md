# 桌面客户端 amem

amem 是 macOS / Windows 上的瘦客户端：看连接、同步和整理状态，逐条处理提案，需要详情时一键打开网页。它不是把网页包进一个壳，而是直接调用服务器的 JSON API，网页仍然是详情界面。

## 为什么单独做客户端

浏览器里的 Web 只能证明“服务器还活着”，不能回答更基本的问题：本机和服务器之间的连接是否通、Syncthing 有没有把各设备连上、dreaming 昨晚跑没跑。客户端把这些控制面信息放在一个常驻窗口和托盘里，并保留提案的批准/驳回入口。

客户端不内嵌网页，原因有三点：

- 服务器 Web 默认只监听回环地址，需要 SSH 隧道；客户端的职责就是维护这条隧道。
- 网页适合读长文和看 diff，客户端适合扫一眼状态，两者分工比重复实现一套富文本渲染更清楚。
- 不内嵌远程页面就不需要在客户端里维护第二套登录、cookie 和 CSP 策略。

## 形态

```text
┌──────────── 客户端（Tauri 2）────────────┐
│ Rust 后端                                │
│  ├─ SSH 隧道 supervisor（ssh -N -L）      │
│  ├─ 健康检查 / 自动重连（指数退避）       │
│  ├─ JSON API 客户端（Bearer token）       │
│  ├─ 设置持久化（config/settings.json）    │
│  ├─ Agent 配置读写（固定路径、原子替换）  │
│  └─ 令牌存系统钥匙串                      │
│ 前端（WebView：状态 / 提案 / Agent / 设置）│
│  └─ 一键打开系统浏览器                     │
└───────────────┬──────────────────────────┘
                │ 127.0.0.1:<本地端口> 或直连 URL
        ┌───────┴────────┐
        │ SSH 隧道        │
        └───────┬────────┘
                │
        服务器 127.0.0.1:8787
        GET /healthz
        GET /api/status
        GET /api/proposals[/<name>]
        POST /api/proposals/<name>/decision
```

客户端主窗口有四页：

| 页面 | 内容 |
|---|---|
| 状态 | 连接状态、延迟、本地端口、最近检查；vault 笔记数、冲突副本、校验结果、最近提交；Syncthing 设备在线列表与最后活跃时间；归档汇总；最近整理、报告数、待处理提案 |
| 提案 | 提案文件列表；进入后逐条显示说明、目标文件、变更后的正文、补丁，以及「批准并写入 / 驳回」按钮 |
| Agent | 本机 vault 路径；检测已安装的 Claude Code、Codex、DeepSeek Harness、Gemini、Pi，用开关接入或移除；Cursor 显示手动配置提示 |
| 设置 | 服务器（`user@主机` 或 `http(s)://地址`，自动判定隧道/直连）、访问令牌；高级里放 SSH 端口、服务器 Web 端口、本地端口、私钥路径；状态刷新间隔 |

Agent 页只改各工具的全局指令文件：开启时写入带 `<!-- amem:begin -->` / `<!-- amem:end -->` 标记的协议引用，关闭时只移除 amem 生成的块或旧格式引用。Claude Code 还会同时维护 `cleanupPeriodDays` 和 `permissions.additionalDirectories`；开启时移除其他 amem vault 的旧权限目录，关闭接入时只移除当前 vault 目录，不撤销 `cleanupPeriodDays`（原值见首次修改前的 `settings.json.bak.amem`）。只有包含 `AGENT_PROTOCOL.md` 和 `MEMORY.md` 的目录才会被视为有效 vault；修改 vault 路径后，已接入的 agent 会显示为未接入，重新打开开关即可把引用改到新路径。路径含空格时会提示，部分 agent 可能无法解析引用。改动在对应 agent 重启后生效。

关闭主窗口不退出程序，收进菜单栏。左键点菜单栏图标弹出面板（状态 / 提案两页，底部可打开主窗口或退出），右键仍是原生菜单（显示主窗口、打开网页、重新连接、退出）。面板贴在图标下方、水平居中；点面板外或切到别的应用时自动收起。设置不放进面板：连接方式、端口和令牌只在主窗口里配置。

macOS 上面板用 `NSPanel`（`tauri-nspanel`）实现，带非激活样式：点开面板不会把 amem 变成前台应用，键盘焦点和「点击外部收起」由面板自己处理。位置按点击坐标所在显示器的工作区计算，多显示器和 Retina 下都使用物理像素匹配。Windows 上仍是无边框置顶窗口，出现在任务栏上方。

菜单栏图标使用单色模板，跟随浅色/深色菜单栏自动反色；未连接时图标旁显示 `!`。如果菜单栏图标太多，系统会把 amem 收进「»」隐藏区；按住 ⌘ 拖动图标可以调整顺序，把它留在可见区域。

## 连接方式

设置页只有一个「服务器」输入框：`user@主机` 按 SSH 隧道处理，`http://` 或 `https://` 开头按直连处理；两种方式特有的端口和私钥收在「高级」里。

### SSH 隧道（默认）

客户端启动 `ssh -N -L 127.0.0.1:<本地端口>:127.0.0.1:<服务器端口>`，然后用本地端口访问 Web。要求：

- 本机有 OpenSSH 客户端（Windows 10/11 可在“可选功能”里安装）；
- 到服务器的免密登录已配置好（密钥或 agent）；
- 端口转发通过 `ExitOnForwardFailure=yes` 启动，失败会立即退出而不是假装连上。

本地端口填 `0` 时自动选空闲端口。客户端会给 `ssh` 加 `BatchMode=yes`、`ServerAliveInterval=30`，避免卡在密码提示。首次连接使用 `StrictHostKeyChecking=accept-new`：未知主机自动接受，已知主机密钥变化仍然拒绝。

健康检查连续失败 3 次后，客户端杀掉隧道并按 2、4、8、16、32 秒退避重连。退避期间不会继续累计失败，避免重试时间被越推越远。

### 直连地址

填入 `http://` 或 `https://` 开头的基础地址，例如 Tailscale 暴露的地址。适合已经用反向代理和令牌保护、不需要 SSH 的场景。直连模式不启动隧道。

## 令牌

令牌就是服务器的 `AMEM_WEB_TOKEN`。客户端通过系统钥匙串读写：

- macOS：登录钥匙串，服务名 `amem-client`，账户 `web-token`；
- Windows：Windows Credential Manager，同样使用这两个标识。

令牌不写入 `settings.json`，也不出现在日志里。清除令牌后状态和提案接口会返回 401，直到重新输入。

设置文件按平台放在 Tauri 的应用配置目录：

| 平台 | 路径 |
|---|---|
| macOS | `~/Library/Application Support/cool.cnb.amem.client/settings.json` |
| Windows | `%APPDATA%\cool.cnb.amem.client\settings.json` |

文件权限在 macOS/Linux 上设为 `600`，内容只有连接参数、刷新间隔和本机 vault 路径。

## 本地开发

依赖：Node 22+、Rust stable、Tauri 2 的系统依赖（macOS 需要 Xcode Command Line Tools；Windows 需要 WebView2 与 MSVC Build Tools）。

```bash
cd client
npm install
npm run tauri dev
```

本地联调如果不想反复写入系统钥匙串，可以用调试构建的环境变量注入令牌（release 构建不会读取）：

```bash
AMEM_CLIENT_TOKEN=... npm run tauri dev
```

只构建前端：

```bash
npm run build        # tsc + vite build
```

构建本机安装包：

```bash
npm run tauri build
```

产物在 `client/src-tauri/target/release/bundle/`。只想要可执行文件、不打包：

```bash
npm run tauri build -- --no-bundle
```

## 构建与发布

### GitHub Actions（默认）

`.github/workflows/desktop.yml` 在 GitHub 托管的 runner 上构建两个平台：

| 任务 | 平台 | 产物 |
|---|---|---|
| macOS universal | `macos-15` | 同时包含 arm64 与 x86_64 的 `.dmg` |
| Windows x64 | `windows-2022` | NSIS 安装程序 `.exe` |

发版：改 `client/src-tauri/tauri.conf.json` 的版本号，提交后打标签并推送：

```bash
git tag client-v0.1.0
git push origin client-v0.1.0
```

标签触发构建，两个平台的产物都上传到同名 GitHub Release。也可以在 Actions 页面手动 `Run workflow`，只构建并上传 Artifacts，不创建 Release；PR 只跑前端检查。

公开仓库的标准 runner 免费；私有仓库按 GitHub 的每月包含额度计费（macOS 约 $0.062/分钟，Windows 约 $0.010/分钟）。

### 本机构建

开发时在本机出安装包：

```bash
cd client
npm run tauri build
```

产物在 `client/src-tauri/target/release/bundle/`。本机构建不发布；需要发布时走上面的标签流程。

`ci.yml` 在每次 push / PR 时跑 Python 测试和客户端前端构建检查（`tsc + vite build`）。

## 安装未签名构建

当前产物未做代码签名和公证，属于内部使用：

- **macOS**：第一次打开被 Gatekeeper 拦下时，右键点击应用 →「打开」；如果提示“已损坏”，执行
  `xattr -dr com.apple.quarantine /Applications/amem.app` 再打开。universal 包在 Intel 和 Apple Silicon 上都能运行。
- **Windows**：SmartScreen 提示“Windows 已保护你的电脑”时，点「更多信息」→「仍要运行」。

签名需要 Apple Developer ID 证书、公证凭据和 Windows 代码签名证书。配置好之后在 workflow 里加 secrets，并把 `APPLE_SIGNING_IDENTITY` 等传给 Tauri 即可；这不是当前范围。

未签名意味着无法验证二进制来源，只应从本机构建或自己的 GitHub Release 下载，不要转发给其他人。

## 安全边界

- 客户端不监听网络端口；SSH 隧道只绑定 `127.0.0.1`。
- 令牌存系统钥匙串，不落配置文件；设置文件只含非敏感连接参数。
- JSON API 与网页共用同一令牌；`/api/*` 未认证返回 401。令牌泄露的影响与 Web 相同：能读记忆、能改 `profile/`/`rules/` 提案。
- 客户端只调用固定的 API 路径，没有任意 URL 代理、没有执行远程命令的接口。
- Agent 页只读写固定的 agent 全局指令路径，不执行本机命令；修改前把原文件备份为 `<文件>.bak.amem`，已有备份时不覆盖，保留首次修改前的版本。
- 客户端展示的服务器字符串（设备名、提交标题、提案正文）全部做 HTML 转义。
- 当前 Tauri 配置未启用 CSP（`csp: null`）。前端不加载远程内容，风险较低；后续可收紧到 `default-src 'self'`，并单独放行 Vite 开发服务器的连接。
- 自动更新未实现；升级需要重新下载安装包。

## 排错

| 现象 | 检查 |
|---|---|
| 一直“未连接”，错误是“SSH 隧道已退出” | 先在终端手动执行 `ssh -N -L 8787:127.0.0.1:8787 user@host`，确认免密登录和端口转发可用 |
| “未找到 ssh 命令” | Windows 安装 OpenSSH 客户端；或改用直连 |
| 状态接口 401 | 设置里重新输入 `AMEM_WEB_TOKEN` |
| 有连接但状态页报“服务器返回 HTTP 502” | 服务器上 `systemctl --user status amem-web`，确认 Web 在 8787 监听 |
| Syncthing 显示“未接入” | 服务器 `web.env` 里配置 `AMEM_SYNCTHING_API_KEY` 后重启 `amem-web` |
| 点“批准”提示冲突 | 提案生成后目标文件被改过；刷新页面看最新内容，或等 dreaming 重新生成 |
| macOS 打不开应用 | 右键 → 打开，或去掉 quarantine 属性 |

## 已知缺口

- 未签名、未公证，也没有自动更新。
- 未签名构建重新安装后，macOS 可能因为二进制签名变化再次询问钥匙串访问权限。
- 未做系统通知；提案更新需要用户在客户端里刷新或等轮询。
- 未做开机自启。
- Windows 构建只在 CI 上验证，本机未实机运行。
- 客户端只处理 `profile/`、`rules/` 提案；其它提案仍需看网页或直接编辑文件。
