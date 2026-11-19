use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, PhysicalPosition, State,
};
use tauri_plugin_opener::OpenerExt;
#[cfg(target_os = "macos")]
use tauri_nspanel::{
    tauri_panel, CollectionBehavior, ManagerExt as PanelManagerExt, PanelLevel, StyleMask,
    WebviewWindowExt as PanelWindowExt,
};

#[cfg(target_os = "macos")]
tauri_panel! {
    panel!(AmemPanel {
        config: {
            can_become_key_window: true,
            can_become_main_window: false,
            is_floating_panel: true
        }
    })
}

const KEYRING_SERVICE: &str = "amem-client";
const KEYRING_USER: &str = "web-token";

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
struct Settings {
    mode: String,
    ssh_host: String,
    ssh_user: String,
    ssh_port: u16,
    ssh_key: String,
    remote_port: u16,
    local_port: u16,
    direct_url: String,
    poll_seconds: u64,
    vault_path: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            mode: "ssh".into(),
            ssh_host: String::new(),
            ssh_user: String::new(),
            ssh_port: 22,
            ssh_key: String::new(),
            remote_port: 8787,
            local_port: 0,
            direct_url: String::new(),
            poll_seconds: 10,
            vault_path: "~/memory-vault".into(),
        }
    }
}

struct Tunnel {
    child: Child,
}

#[derive(Default)]
struct Runtime {
    connected: bool,
    latency_ms: Option<u64>,
    last_check: Option<u64>,
    last_error: Option<String>,
    local_port: Option<u16>,
    fail_count: u32,
    retry_count: u32,
    retry_at: Option<Instant>,
}

struct AppState {
    settings: Mutex<Settings>,
    runtime: Mutex<Runtime>,
    tunnel: Mutex<Option<Tunnel>>,
    config_dir: PathBuf,
}

#[derive(Serialize)]
struct RuntimeView {
    connected: bool,
    latency_ms: Option<u64>,
    last_check: Option<u64>,
    last_error: Option<String>,
    local_port: Option<u16>,
    retrying: bool,
    retry_count: u32,
}

impl AppState {
    fn new(app: &AppHandle) -> Self {
        let config_dir = app
            .path()
            .app_config_dir()
            .unwrap_or_else(|_| PathBuf::from("."));
        let _ = std::fs::create_dir_all(&config_dir);
        let settings = std::fs::read_to_string(config_dir.join("settings.json"))
            .ok()
            .and_then(|text| serde_json::from_str::<Settings>(&text).ok())
            .unwrap_or_default();
        Self {
            settings: Mutex::new(settings),
            runtime: Mutex::new(Runtime::default()),
            tunnel: Mutex::new(None),
            config_dir,
        }
    }

    fn save_settings_file(&self, settings: &Settings) -> Result<(), String> {
        let _ = std::fs::create_dir_all(&self.config_dir);
        let data = serde_json::to_vec_pretty(settings).map_err(|e| e.to_string())?;
        let tmp = self.config_dir.join(".settings.json.tmp");
        std::fs::write(&tmp, data).map_err(|e| format!("写入设置失败: {e}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
        }
        std::fs::rename(&tmp, self.config_dir.join("settings.json"))
            .map_err(|e| format!("保存设置失败: {e}"))
    }

    fn drop_tunnel(&self) {
        if let Some(mut tunnel) = self.tunnel.lock().unwrap().take() {
            let _ = tunnel.child.kill();
            let _ = tunnel.child.wait();
        }
        self.runtime.lock().unwrap().local_port = None;
    }

    fn base_url(&self, settings: &Settings) -> Option<String> {
        if settings.mode == "direct" {
            let url = settings.direct_url.trim().trim_end_matches('/');
            if url.is_empty() {
                None
            } else {
                Some(url.to_string())
            }
        } else {
            self.runtime
                .lock()
                .unwrap()
                .local_port
                .map(|port| format!("http://127.0.0.1:{port}"))
        }
    }

    fn connection(&self) -> Result<(String, String), String> {
        let settings = self.settings.lock().unwrap().clone();
        let base = self
            .base_url(&settings)
            .ok_or_else(|| "连接尚未建立，请先检查设置或重新连接".to_string())?;
        let token = load_token()?.ok_or_else(|| "尚未配置访问令牌".to_string())?;
        Ok((base, token))
    }

    fn schedule_retry(&self, message: &str) {
        let mut runtime = self.runtime.lock().unwrap();
        runtime.last_error = Some(message.to_string());
        runtime.retry_count = (runtime.retry_count + 1).min(5);
        let delay = 2u64.saturating_pow(runtime.retry_count);
        runtime.retry_at = Some(Instant::now() + Duration::from_secs(delay));
        runtime.fail_count = 0;
    }

    fn ensure_tunnel(&self, settings: &Settings) {
        let mut guard = self.tunnel.lock().unwrap();
        if let Some(tunnel) = guard.as_mut() {
            match tunnel.child.try_wait() {
                Ok(None) => return,
                _ => {
                    *guard = None;
                    drop(guard);
                    self.runtime.lock().unwrap().local_port = None;
                    self.schedule_retry("SSH 隧道已退出，稍后重试");
                    return;
                }
            }
        }
        {
            let mut runtime = self.runtime.lock().unwrap();
            if let Some(at) = runtime.retry_at {
                if Instant::now() < at {
                    return;
                }
                runtime.retry_at = None;
            }
        }
        if settings.ssh_host.trim().is_empty() || settings.ssh_user.trim().is_empty() {
            let mut runtime = self.runtime.lock().unwrap();
            runtime.last_error = Some("尚未配置 SSH 主机或用户名".into());
            return;
        }
        if !ssh_available() {
            let mut runtime = self.runtime.lock().unwrap();
            runtime.last_error = Some("未找到 ssh 命令，请先安装 OpenSSH 客户端".into());
            return;
        }
        let local_port = if settings.local_port == 0 {
            free_port().unwrap_or(0)
        } else {
            settings.local_port
        };
        if local_port == 0 {
            let mut runtime = self.runtime.lock().unwrap();
            runtime.last_error = Some("无法分配本地端口".into());
            return;
        }
        let mut command = Command::new("ssh");
        command
            .arg("-N")
            .arg("-L")
            .arg(format!(
                "127.0.0.1:{local_port}:127.0.0.1:{}",
                settings.remote_port
            ))
            .arg("-p")
            .arg(settings.ssh_port.to_string())
            .arg("-o")
            .arg("ExitOnForwardFailure=yes")
            .arg("-o")
            .arg("ServerAliveInterval=30")
            .arg("-o")
            .arg("ServerAliveCountMax=3")
            .arg("-o")
            .arg("BatchMode=yes")
            .arg("-o")
            .arg("StrictHostKeyChecking=accept-new")
            .arg("-o")
            .arg("ConnectTimeout=10");
        if !settings.ssh_key.trim().is_empty() {
            command.arg("-i").arg(expand_home(settings.ssh_key.trim()));
        }
        command.arg(format!("{}@{}", settings.ssh_user.trim(), settings.ssh_host.trim()));
        command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        match command.spawn() {
            Ok(child) => {
                *guard = Some(Tunnel { child });
                drop(guard);
                self.runtime.lock().unwrap().local_port = Some(local_port);
                // 给 ssh 一点时间建立转发，下一轮健康检查更容易一次成功。
                std::thread::sleep(Duration::from_millis(600));
            }
            Err(e) => {
                let mut runtime = self.runtime.lock().unwrap();
                runtime.last_error = Some(format!("无法启动 ssh: {e}"));
            }
        }
    }

    fn tick(&self, app: &AppHandle) {
        let settings = self.settings.lock().unwrap().clone();
        if settings.mode == "ssh" {
            self.ensure_tunnel(&settings);
        } else {
            self.drop_tunnel();
        }
        let waiting = {
            let mut runtime = self.runtime.lock().unwrap();
            match runtime.retry_at {
                Some(at) if Instant::now() < at => true,
                Some(_) => {
                    runtime.retry_at = None;
                    false
                }
                None => false,
            }
        };
        if settings.mode == "ssh" && waiting {
            // 退避等待期间不要反复计失败，否则会把重试时间越推越远。
            let mut runtime = self.runtime.lock().unwrap();
            runtime.connected = false;
            runtime.last_check = Some(now_ms());
            drop(runtime);
            update_tray(app, "amem：等待重连", false);
            let _ = app.emit("runtime-changed", ());
            return;
        }
        let base = self.base_url(&settings);
        let check = match base.as_deref() {
            Some(base) => health_check(base),
            None => Err(if settings.mode == "ssh" {
                "隧道尚未建立".to_string()
            } else {
                "尚未配置服务器地址".to_string()
            }),
        };
        let mut reconnect = false;
        {
            let mut runtime = self.runtime.lock().unwrap();
            match check {
                Ok(latency) => {
                    runtime.connected = true;
                    runtime.latency_ms = Some(latency);
                    runtime.last_error = None;
                    runtime.fail_count = 0;
                    runtime.retry_count = 0;
                    runtime.retry_at = None;
                }
                Err(error) => {
                    runtime.connected = false;
                    runtime.last_error = Some(error);
                    runtime.fail_count += 1;
                    if settings.mode == "ssh" && runtime.fail_count >= 3 {
                        runtime.retry_count = (runtime.retry_count + 1).min(5);
                        let delay = 2u64.saturating_pow(runtime.retry_count);
                        runtime.retry_at = Some(Instant::now() + Duration::from_secs(delay));
                        runtime.fail_count = 0;
                        reconnect = true;
                    }
                }
            }
            runtime.last_check = Some(now_ms());
        }
        if reconnect {
            self.drop_tunnel();
        }
        let (connected, latency) = {
            let runtime = self.runtime.lock().unwrap();
            (runtime.connected, runtime.latency_ms)
        };
        let text = if connected {
            format!(
                "amem：已连接{}",
                latency.map(|ms| format!(" · {ms}ms")).unwrap_or_default()
            )
        } else {
            "amem：未连接".to_string()
        };
        update_tray(app, &text, connected);
        let _ = app.emit("runtime-changed", ());
    }

    fn reconnect(&self, app: &AppHandle) {
        self.drop_tunnel();
        {
            let mut runtime = self.runtime.lock().unwrap();
            runtime.retry_at = None;
            runtime.retry_count = 0;
            runtime.fail_count = 0;
            runtime.last_error = None;
        }
        let app_handle = app.clone();
        std::thread::spawn(move || {
            let state = app_handle.state::<AppState>();
            state.tick(&app_handle);
        });
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn expand_home_in(home: &Path, path: &str) -> PathBuf {
    if path == "~" {
        return home.to_path_buf();
    }
    for prefix in ["~/", "~\\"] {
        if let Some(rest) = path.strip_prefix(prefix) {
            return home.join(rest);
        }
    }
    PathBuf::from(path)
}

fn expand_home(path: &str) -> PathBuf {
    match home_dir() {
        Ok(home) => expand_home_in(&home, path),
        Err(_) => PathBuf::from(path),
    }
}

fn ssh_available() -> bool {
    Command::new("ssh")
        .arg("-V")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok()
}

fn free_port() -> std::io::Result<u16> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    Ok(listener.local_addr()?.port())
}

fn health_check(base: &str) -> Result<u64, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| format!("创建 HTTP 客户端失败: {e}"))?;
    let started = Instant::now();
    let response = client
        .get(format!("{base}/healthz"))
        .send()
        .map_err(|e| format!("无法连接服务器: {e}"))?;
    if !response.status().is_success() {
        return Err(format!("服务器返回 HTTP {}", response.status()));
    }
    Ok(started.elapsed().as_millis() as u64)
}

fn api_request(
    base: &str,
    token: &str,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> Result<Value, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| format!("创建 HTTP 客户端失败: {e}"))?;
    let url = format!("{base}{path}");
    let request = match method {
        "GET" => client.get(url),
        "POST" => client.post(url),
        _ => return Err("不支持的请求方法".into()),
    }
    .bearer_auth(token)
    .header("Accept", "application/json");
    let request = match body {
        Some(value) => request.json(&value),
        None => request,
    };
    let response = request.send().map_err(|e| format!("请求失败: {e}"))?;
    let status = response.status();
    let text = response.text().map_err(|e| format!("读取响应失败: {e}"))?;
    let value: Value = serde_json::from_str(&text)
        .map_err(|_| format!("服务器返回了非 JSON 内容（HTTP {status}）"))?;
    if !status.is_success() {
        let message = value
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("请求失败");
        return Err(message.to_string());
    }
    Ok(value)
}

fn load_token() -> Result<Option<String>, String> {
    // 仅开发构建可用环境变量注入令牌，避免本地联调反复操作钥匙串；release 不编译这段。
    #[cfg(debug_assertions)]
    if let Ok(token) = std::env::var("AMEM_CLIENT_TOKEN") {
        if !token.is_empty() {
            return Ok(Some(token));
        }
    }
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER)
        .map_err(|e| format!("访问钥匙串失败: {e}"))?;
    match entry.get_password() {
        Ok(token) => Ok(Some(token)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("读取钥匙串失败: {e}")),
    }
}

fn set_token(token: &str) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER)
        .map_err(|e| format!("访问钥匙串失败: {e}"))?;
    entry
        .set_password(token)
        .map_err(|e| format!("写入钥匙串失败: {e}"))
}

fn delete_token() -> Result<(), String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER)
        .map_err(|e| format!("访问钥匙串失败: {e}"))?;
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(format!("删除钥匙串条目失败: {e}")),
    }
}

fn validate_settings(settings: &Settings) -> Result<(), String> {
    if settings.vault_path.trim().is_empty() {
        return Err("请填写本机 vault 路径".into());
    }
    if !(3..=120).contains(&settings.poll_seconds) {
        return Err("刷新间隔需在 3 到 120 秒之间".into());
    }
    match settings.mode.as_str() {
        "ssh" => {
            if settings.ssh_host.trim().is_empty() || settings.ssh_user.trim().is_empty() {
                return Err("请填写 SSH 主机和用户名".into());
            }
            if settings.ssh_port == 0 || settings.remote_port == 0 {
                return Err("端口不能为 0".into());
            }
        }
        "direct" => {
            let url = settings.direct_url.trim();
            if !(url.starts_with("http://") || url.starts_with("https://")) {
                return Err("直连地址需以 http:// 或 https:// 开头".into());
            }
        }
        _ => return Err("未知连接方式".into()),
    }
    Ok(())
}

// ---------- 本机 agent 接入 ----------

const AGENT_BEGIN: &str = "<!-- amem:begin -->";
const AGENT_END: &str = "<!-- amem:end -->";
const LEGACY_MARK: &str = "<!-- amem: 共享记忆 -->";
const INSTRUCTION_PREFIX: &str = "开始任何任务前，先读取并遵守 ";

#[derive(Clone, Copy, PartialEq, Eq)]
enum AgentStyle {
    AtRef,
    Instruction,
}

struct AgentSpec {
    id: &'static str,
    name: &'static str,
    dir: &'static str,
    file: &'static str,
    style: AgentStyle,
    manual: bool,
}

const AGENT_SPECS: &[AgentSpec] = &[
    AgentSpec {
        id: "claude-code",
        name: "Claude Code",
        dir: ".claude",
        file: ".claude/CLAUDE.md",
        style: AgentStyle::AtRef,
        manual: false,
    },
    AgentSpec {
        id: "codex",
        name: "Codex",
        dir: ".codex",
        file: ".codex/AGENTS.md",
        style: AgentStyle::Instruction,
        manual: false,
    },
    AgentSpec {
        id: "dsh",
        name: "DeepSeek Harness",
        dir: ".dsh",
        file: "AGENTS.md",
        style: AgentStyle::Instruction,
        manual: false,
    },
    AgentSpec {
        id: "gemini",
        name: "Gemini",
        dir: ".gemini",
        file: ".gemini/GEMINI.md",
        style: AgentStyle::AtRef,
        manual: false,
    },
    AgentSpec {
        id: "pi",
        name: "Pi",
        dir: ".pi/agent",
        file: ".pi/agent/AGENTS.md",
        style: AgentStyle::Instruction,
        manual: false,
    },
    AgentSpec {
        id: "cursor",
        name: "Cursor",
        dir: ".cursor",
        file: "",
        style: AgentStyle::Instruction,
        manual: true,
    },
];

#[derive(Serialize)]
struct AgentView {
    id: String,
    name: String,
    installed: bool,
    integrated: bool,
    manual: bool,
    config_path: String,
}

#[derive(Serialize)]
struct AgentConfigView {
    vault_path: String,
    vault_exists: bool,
    vault_ready: bool,
    agents: Vec<AgentView>,
}

fn home_dir() -> Result<PathBuf, String> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| "找不到用户主目录".to_string())
}

fn vault_dir(settings: &Settings) -> PathBuf {
    expand_home(settings.vault_path.trim())
}

fn vault_is_ready(vault: &Path) -> bool {
    vault.is_dir()
        && vault.join("AGENT_PROTOCOL.md").is_file()
        && vault.join("MEMORY.md").is_file()
}

fn agent_dir(spec: &AgentSpec, home: &Path) -> PathBuf {
    if spec.id == "dsh" {
        if let Some(dir) = std::env::var_os("DSH_HOME") {
            return PathBuf::from(dir);
        }
    }
    home.join(spec.dir)
}

fn agent_file(spec: &AgentSpec, home: &Path) -> PathBuf {
    if spec.id == "dsh" {
        agent_dir(spec, home).join(spec.file)
    } else {
        home.join(spec.file)
    }
}

fn managed_block(style: AgentStyle, vault: &Path) -> String {
    let protocol = vault.join("AGENT_PROTOCOL.md").to_string_lossy().to_string();
    let memory = vault.join("MEMORY.md").to_string_lossy().to_string();
    let body = match style {
        AgentStyle::AtRef => format!("@{protocol}\n@{memory}"),
        AgentStyle::Instruction => {
            format!("{INSTRUCTION_PREFIX}{protocol}，再读 {memory}。")
        }
    };
    format!("{AGENT_BEGIN}\n{body}\n{AGENT_END}")
}

fn is_exact_at_ref(line: &str, name: &str) -> bool {
    let trimmed = line.trim();
    let Some(path) = trimmed.strip_prefix('@') else {
        return false;
    };
    let slash = format!("/{name}");
    let backslash = format!("\\{name}");
    path.len() > name.len() && (path.ends_with(&slash) || path.ends_with(&backslash))
}

fn is_protocol_ref_shape(line: &str) -> bool {
    is_exact_at_ref(line, "AGENT_PROTOCOL.md")
}

fn is_memory_ref_shape(line: &str) -> bool {
    is_exact_at_ref(line, "MEMORY.md")
}

/// 旧版脚本生成的指示句是固定句式；只认这个形状，避免把用户笔记当成受管内容。
fn is_instruction_shape(line: &str) -> bool {
    let trimmed = line.trim();
    trimmed.starts_with(INSTRUCTION_PREFIX)
        && trimmed.ends_with("。")
        && (trimmed.contains("/AGENT_PROTOCOL.md，再读 ")
            || trimmed.contains("\\AGENT_PROTOCOL.md，再读 "))
        && (trimmed.contains("/MEMORY.md") || trimmed.contains("\\MEMORY.md"))
}

/// 找到与 begin 配对的 end；中间出现新的 begin 时视为不配对。
fn block_end(lines: &[&str], start: usize) -> Option<usize> {
    for (offset, line) in lines[start..].iter().enumerate() {
        let trimmed = line.trim();
        if trimmed == AGENT_BEGIN {
            return None;
        }
        if trimmed == AGENT_END {
            return Some(start + offset);
        }
    }
    None
}

fn rest_is_blank(lines: &[&str], start: usize) -> bool {
    lines[start..].iter().all(|line| line.trim().is_empty())
}

/// begin/end 必须严格配对；不配对时拒绝改动，避免把用户内容夹在块里删掉。
fn has_malformed_markers(text: &str) -> bool {
    let mut in_block = false;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed == AGENT_BEGIN {
            if in_block {
                return true;
            }
            in_block = true;
        } else if trimmed == AGENT_END {
            if !in_block {
                return true;
            }
            in_block = false;
        }
    }
    in_block
}

/// 移除 amem 写入的块。新格式的 begin/end 块由标记本身拥有，任何路径都移除；
/// 旧格式只处理可确认由 amem 生成的形状（精确 @ 引用行、固定句式指示句）。
///
/// `all_legacy` 为 true 时移除指向任意 vault 的旧格式（用于换路径/重新接入）；
/// 为 false 时只移除指向当前 vault 的旧格式（用于关闭接入，保留其他 vault 的引用）。
fn strip_managed_blocks_scoped(text: &str, vault: &Path, all_legacy: bool) -> String {
    let protocol = vault.join("AGENT_PROTOCOL.md").to_string_lossy().to_string();
    let memory = vault.join("MEMORY.md").to_string_lossy().to_string();
    let protocol_ref = format!("@{protocol}");
    let memory_ref = format!("@{memory}");
    let instruction = format!("{INSTRUCTION_PREFIX}{protocol}，再读 {memory}。");
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    let mut out: Vec<&str> = Vec::new();
    let mut index = 0;

    while index < lines.len() {
        let line = lines[index];
        let trimmed = line.trim();
        if trimmed == AGENT_BEGIN {
            if let Some(end) = block_end(&lines, index + 1) {
                // 块在文件末尾时，去掉 upsert 加在它前面的一个空行，
                // 但保留用户原有的尾部空行。
                if rest_is_blank(&lines, end + 1)
                    && out.last().is_some_and(|line| line.trim().is_empty())
                {
                    out.pop();
                }
                index = end + 1;
            } else {
                // 孤立的 begin 保留原样，不吞掉后面的用户内容。
                out.push(line);
                index += 1;
            }
            continue;
        }
        if trimmed == LEGACY_MARK {
            if all_legacy {
                index += 1;
                let mut removed_refs = 0;
                while index < lines.len()
                    && removed_refs < 2
                    && (is_protocol_ref_shape(lines[index]) || is_memory_ref_shape(lines[index]))
                {
                    index += 1;
                    removed_refs += 1;
                }
                if removed_refs == 0 && index < lines.len() && is_instruction_shape(lines[index]) {
                    index += 1;
                }
            } else {
                let mut look = index + 1;
                let mut refs = Vec::new();
                while look < lines.len()
                    && refs.len() < 2
                    && (is_protocol_ref_shape(lines[look]) || is_memory_ref_shape(lines[look]))
                {
                    refs.push(lines[look].trim());
                    look += 1;
                }
                let current_pair = refs.len() == 2
                    && refs.iter().any(|value| *value == protocol_ref)
                    && refs.iter().any(|value| *value == memory_ref);
                let current_instruction =
                    look == index + 1 && lines[look.min(lines.len() - 1)].trim() == instruction;
                if current_pair || current_instruction {
                    index = if current_instruction { look + 1 } else { look };
                    continue;
                }
                // 不是当前 vault 的旧块：连同 marker 原样保留。
                out.push(line);
                index += 1;
            }
            continue;
        }
        let managed_instruction = if all_legacy {
            is_instruction_shape(trimmed)
        } else {
            trimmed == instruction
        };
        if managed_instruction {
            index += 1;
            continue;
        }
        let generic_pair = is_protocol_ref_shape(trimmed)
            && index + 1 < lines.len()
            && is_memory_ref_shape(lines[index + 1]);
        let current_pair = trimmed == protocol_ref
            && index + 1 < lines.len()
            && lines[index + 1].trim() == memory_ref;
        if if all_legacy { generic_pair } else { current_pair } {
            index += 2;
            continue;
        }
        out.push(line);
        index += 1;
    }

    if out.is_empty() {
        String::new()
    } else {
        out.concat()
    }
}

fn strip_managed_blocks(text: &str, vault: &Path) -> String {
    strip_managed_blocks_scoped(text, vault, false)
}

fn strip_all_managed_blocks(text: &str, vault: &Path) -> String {
    strip_managed_blocks_scoped(text, vault, true)
}

fn upsert_managed_block(text: &str, block: &str, vault: &Path) -> String {
    let newline = if text.contains("\r\n") { "\r\n" } else { "\n" };
    let block = block.replace('\n', newline);
    let cleaned = strip_all_managed_blocks(text, vault);
    if cleaned.trim().is_empty() {
        return format!("{block}{newline}");
    }
    let mut out = cleaned;
    if !out.ends_with('\n') {
        out.push_str(newline);
    }
    out.push_str(newline);
    out.push_str(&block);
    out.push_str(newline);
    out
}

fn text_has_managed(text: &str, vault: &Path) -> bool {
    let protocol = vault.join("AGENT_PROTOCOL.md").to_string_lossy().to_string();
    let memory = vault.join("MEMORY.md").to_string_lossy().to_string();
    let protocol_ref = format!("@{protocol}");
    let memory_ref = format!("@{memory}");
    let instruction = format!("{INSTRUCTION_PREFIX}{protocol}，再读 {memory}。");
    let lines: Vec<&str> = text.lines().collect();
    let mut index = 0;

    while index < lines.len() {
        let trimmed = lines[index].trim();
        if trimmed == AGENT_BEGIN {
            if let Some(end) = block_end(&lines, index + 1) {
                let body: Vec<&str> = lines[index + 1..end]
                    .iter()
                    .map(|line| line.trim())
                    .collect();
                let has_refs =
                    body.contains(&protocol_ref.as_str()) && body.contains(&memory_ref.as_str());
                if has_refs || body.contains(&instruction.as_str()) {
                    return true;
                }
                index = end + 1;
                continue;
            }
        }
        if trimmed == instruction {
            return true;
        }
        if trimmed == protocol_ref
            && index + 1 < lines.len()
            && lines[index + 1].trim() == memory_ref
        {
            return true;
        }
        index += 1;
    }
    false
}

fn file_has_managed(path: &Path, vault: &Path) -> bool {
    let Ok(text) = std::fs::read_to_string(path) else {
        return false;
    };
    text_has_managed(&text, vault)
}

fn read_config_text(path: &Path) -> Result<String, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(format!("读取 {} 失败: {error}", path.display())),
    }
}

fn backup_file(path: &Path) -> Result<(), String> {
    if !path.is_file() {
        return Ok(());
    }
    let backup = PathBuf::from(format!("{}.bak.amem", path.display()));
    match std::fs::symlink_metadata(&backup) {
        Ok(meta) if meta.file_type().is_file() => return Ok(()),
        Ok(_) => {
            return Err(format!(
                "备份路径已存在且不是普通文件: {}",
                backup.display()
            ))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(format!("检查备份 {} 失败: {error}", backup.display()))
        }
    }
    std::fs::copy(path, &backup)
        .map(|_| ())
        .map_err(|error| format!("备份 {} 失败: {error}", path.display()))
}

fn write_atomic(path: &Path, content: &str) -> Result<(), String> {
    // 保留符号链接：dotfiles 用户常把 CLAUDE.md/settings.json 链接到版本库，
    // 直接 rename 会把链接替换成普通文件。
    let target = match std::fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_symlink() => std::fs::canonicalize(path)
            .map_err(|error| format!("解析符号链接失败 {}: {error}", path.display()))?,
        _ => path.to_path_buf(),
    };
    let parent = target
        .parent()
        .ok_or_else(|| format!("无效路径: {}", target.display()))?;
    std::fs::create_dir_all(parent).map_err(|e| format!("创建目录失败: {e}"))?;
    let name = target
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("amem");
    let tmp = parent.join(format!(".{name}.amem-tmp"));
    std::fs::write(&tmp, content).map_err(|e| format!("写入临时文件失败: {e}"))?;
    if let Ok(meta) = std::fs::metadata(&target) {
        let _ = std::fs::set_permissions(&tmp, meta.permissions());
    }
    std::fs::rename(&tmp, &target).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("替换文件失败: {e}")
    })
}

fn update_claude_settings(home: &Path, vault: &Path, enabled: bool) -> Result<(), String> {
    let path = home.join(".claude").join("settings.json");
    let before = read_config_text(&path)?;
    let mut data: Value = if before.trim().is_empty() {
        json!({})
    } else {
        serde_json::from_str(&before).map_err(|e| format!("解析 settings.json 失败: {e}"))?
    };
    if !data.is_object() {
        return Err("settings.json 顶层不是对象".into());
    }

    if enabled {
        data["cleanupPeriodDays"] = json!(36500);
    }

    let vault_text = vault.to_string_lossy().to_string();
    if enabled {
        let perms_value = data
            .as_object_mut()
            .unwrap()
            .entry("permissions".to_string())
            .or_insert_with(|| json!({}));
        if perms_value.is_null() {
            *perms_value = json!({});
        }
        let perms = perms_value.as_object_mut().ok_or("permissions 不是对象")?;
        let dirs_value = perms
            .entry("additionalDirectories".to_string())
            .or_insert_with(|| json!([]));
        if dirs_value.is_null() {
            *dirs_value = json!([]);
        }
        let dirs = dirs_value
            .as_array_mut()
            .ok_or("permissions.additionalDirectories 不是数组")?;
        // 换 vault 后清掉其他 amem vault 的旧权限，只保留当前 vault。
        dirs.retain(|value| {
            let Some(text) = value.as_str() else {
                return true;
            };
            expand_home_in(home, text).as_path() == vault
                || !vault_is_ready(&expand_home_in(home, text))
        });
        if !dirs
            .iter()
            .any(|value| {
                value
                    .as_str()
                    .is_some_and(|text| expand_home_in(home, text).as_path() == vault)
            })
        {
            dirs.push(json!(vault_text));
        }
    } else if let Some(perms_value) = data.get_mut("permissions") {
        if !perms_value.is_null() {
            let perms = perms_value.as_object_mut().ok_or("permissions 不是对象")?;
            if let Some(dirs_value) = perms.get_mut("additionalDirectories") {
                if !dirs_value.is_null() {
                    let dirs = dirs_value
                        .as_array_mut()
                        .ok_or("permissions.additionalDirectories 不是数组")?;
                    dirs.retain(|value| {
                        !value
                            .as_str()
                            .is_some_and(|text| expand_home_in(home, text).as_path() == vault)
                    });
                }
            }
        }
    }

    let after = format!(
        "{}\n",
        serde_json::to_string_pretty(&data).map_err(|e| e.to_string())?
    );
    if before != after {
        backup_file(&path)?;
        write_atomic(&path, &after)?;
    }
    Ok(())
}

fn agent_views(settings: &Settings) -> Result<AgentConfigView, String> {
    let home = home_dir()?;
    let vault = vault_dir(settings);
    let agents = AGENT_SPECS
        .iter()
        .map(|spec| {
            let dir = agent_dir(spec, &home);
            let file = agent_file(spec, &home);
            let installed = dir.is_dir();
            let integrated = !spec.manual && file.is_file() && file_has_managed(&file, &vault);
            AgentView {
                id: spec.id.to_string(),
                name: spec.name.to_string(),
                installed,
                integrated,
                manual: spec.manual,
                config_path: if spec.manual {
                    dir.display().to_string()
                } else {
                    file.display().to_string()
                },
            }
        })
        .collect();
    Ok(AgentConfigView {
        vault_path: settings.vault_path.clone(),
        vault_exists: vault.is_dir(),
        vault_ready: vault_is_ready(&vault),
        agents,
    })
}

fn set_agent_enabled(settings: &Settings, id: &str, enabled: bool) -> Result<(), String> {
    let spec = AGENT_SPECS
        .iter()
        .find(|spec| spec.id == id)
        .ok_or_else(|| "未知 agent".to_string())?;
    if spec.manual {
        return Err(format!("{} 没有全局指令文件，需要手动配置", spec.name));
    }
    let home = home_dir()?;
    let vault = vault_dir(settings);
    let dir = agent_dir(spec, &home);

    if enabled {
        if !vault_is_ready(&vault) {
            return Err(format!(
                "vault 不完整（缺少 AGENT_PROTOCOL.md 或 MEMORY.md）: {}",
                vault.display()
            ));
        }
        if !dir.is_dir() {
            return Err(format!("未检测到 {}", spec.name));
        }
    }

    let file = agent_file(spec, &home);
    let before = read_config_text(&file)?;
    if has_malformed_markers(&before) {
        return Err(format!(
            "{} 含不完整的 amem 标记（begin/end 不配对），请先手工修复",
            file.display()
        ));
    }
    let after = if enabled {
        upsert_managed_block(&before, &managed_block(spec.style, &vault), &vault)
    } else {
        strip_managed_blocks(&before, &vault)
    };
    let changed = before != after;
    if changed {
        backup_file(&file)?;
    }
    // 开启时先更新 Claude 设置，解析失败就不写 Agent 文件。
    if enabled && spec.id == "claude-code" {
        update_claude_settings(&home, &vault, true)?;
    }
    if changed {
        write_atomic(&file, &after)?;
    }
    // 关闭时先移除引用，再清理 settings.json 里的权限。
    if !enabled && spec.id == "claude-code" {
        update_claude_settings(&home, &vault, false)?;
    }
    Ok(())
}

#[tauri::command]
fn get_agents(state: State<'_, AppState>) -> Result<AgentConfigView, String> {
    let settings = state.settings.lock().unwrap().clone();
    agent_views(&settings)
}

#[tauri::command]
fn set_agent(
    state: State<'_, AppState>,
    id: String,
    enabled: bool,
) -> Result<AgentConfigView, String> {
    let settings = state.settings.lock().unwrap().clone();
    set_agent_enabled(&settings, &id, enabled)?;
    agent_views(&settings)
}

#[tauri::command]
fn save_vault_path(
    state: State<'_, AppState>,
    path: String,
) -> Result<AgentConfigView, String> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return Err("vault 路径不能为空".into());
    }
    if trimmed.contains('\n') || trimmed.contains('\r') {
        return Err("vault 路径不能包含换行".into());
    }
    if !expand_home(trimmed).is_absolute() {
        return Err("vault 路径需要是绝对路径，或以 ~/ 开头".into());
    }
    let mut settings = state.settings.lock().unwrap().clone();
    settings.vault_path = trimmed.to_string();
    state.save_settings_file(&settings)?;
    *state.settings.lock().unwrap() = settings.clone();
    agent_views(&settings)
}

fn show_main(app: &AppHandle) {
    if let Some(panel) = app.get_webview_window("panel") {
        hide_panel_window(app, &panel);
    }
    #[cfg(target_os = "macos")]
    let _ = app.show();
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn monitor_at(panel: &tauri::WebviewWindow, point: PhysicalPosition<f64>) -> Option<tauri::Monitor> {
    // 不用 monitor_from_point：macOS 上它按逻辑点匹配，其他平台按物理像素，语义不一致。
    panel.available_monitors().ok()?.into_iter().find(|monitor| {
        let origin = monitor.position();
        let size = monitor.size();
        point.x >= origin.x as f64
            && point.x < (origin.x as f64 + size.width as f64)
            && point.y >= origin.y as f64
            && point.y < (origin.y as f64 + size.height as f64)
    })
}

#[cfg(target_os = "macos")]
fn move_panel_immediately(app: &AppHandle, x: f64, y: f64, scale: f64) -> bool {
    let Ok(ns_panel) = app.get_webview_panel("panel") else {
        return false;
    };
    let Some(primary) = app
        .get_webview_window("panel")
        .and_then(|panel| panel.primary_monitor().ok().flatten())
    else {
        return false;
    };
    let primary_height = primary.size().height as f64 / primary.scale_factor();
    let point = tauri_nspanel::NSPoint::new(x / scale, primary_height - y / scale);
    let _: () = unsafe {
        tauri_nspanel::objc2::msg_send![ns_panel.as_panel(), setFrameTopLeftPoint: point]
    };
    true
}

fn position_panel(app: &AppHandle, panel: &tauri::WebviewWindow, anchor: Option<PhysicalPosition<f64>>) {
    let cursor = anchor
        .or_else(|| panel.cursor_position().ok())
        .unwrap_or(PhysicalPosition::new(0.0, 0.0));
    let Ok(size) = panel.outer_size() else {
        return;
    };
    let Some(monitor) = monitor_at(panel, cursor) else {
        return;
    };
    let scale = monitor.scale_factor();
    let work = monitor.work_area();
    let margin = 8.0 * scale;
    let left = work.position.x as f64 + margin;
    let right = work.position.x as f64 + work.size.width as f64 - size.width as f64 - margin;
    let x = (cursor.x - size.width as f64 / 2.0).clamp(left.min(right), right.max(left));
    // macOS 菜单栏在顶部：面板顶边贴住工作区上沿；Windows 任务栏通常在底部：面板底边贴住工作区下沿。
    #[cfg(target_os = "windows")]
    let y = work.position.y as f64 + work.size.height as f64 - size.height as f64 - margin;
    #[cfg(not(target_os = "windows"))]
    let y = work.position.y as f64;
    #[cfg(target_os = "macos")]
    if move_panel_immediately(app, x, y, scale) {
        return;
    }
    let _ = panel.set_position(PhysicalPosition::new(x.round() as i32, y.round() as i32));
    let _ = app;
}

/// 面板隐藏期间 WebKit 可能回收内容进程。显示后先探测页面是否还能响应，没响应就重载，
/// 避免出现「窗口已显示但整页全透明」。
static PANEL_PROBE_OK: AtomicBool = AtomicBool::new(false);

#[tauri::command]
fn panel_alive() {
    PANEL_PROBE_OK.store(true, Ordering::Relaxed);
}

fn ensure_panel_rendered(app: &AppHandle, panel: &tauri::WebviewWindow) {
    PANEL_PROBE_OK.store(false, Ordering::Relaxed);
    let _ = app.emit_to("panel", "panel-probe", ());
    let panel = panel.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(400));
        if !PANEL_PROBE_OK.load(Ordering::Relaxed) {
            let _ = panel.reload();
        }
    });
}

fn show_panel_window(app: &AppHandle, panel: &tauri::WebviewWindow) {
    #[cfg(target_os = "macos")]
    if let Ok(ns_panel) = app.get_webview_panel("panel") {
        ns_panel.show_and_make_key();
        ensure_panel_rendered(app, panel);
        return;
    }
    let _ = panel.show();
    let _ = panel.set_focus();
    ensure_panel_rendered(app, panel);
}

fn hide_panel_window(app: &AppHandle, panel: &tauri::WebviewWindow) {
    #[cfg(target_os = "macos")]
    if let Ok(ns_panel) = app.get_webview_panel("panel") {
        ns_panel.hide();
        return;
    }
    let _ = app;
    let _ = panel.hide();
}

/// 记录因失焦自动收起的时间。点图标关闭面板时可能先失焦隐藏、再收到点击事件，
/// 这个时间戳让随后的 toggle 不再把面板重新打开。
static LAST_AUTO_HIDE: Mutex<Option<Instant>> = Mutex::new(None);

fn toggle_panel(app: &AppHandle, anchor: Option<PhysicalPosition<f64>>) {
    // 从菜单栏溢出区点击时，系统可能补发一次 click；短时间内只认第一次，避免刚打开就被收起。
    static LAST_TOGGLE: Mutex<Option<Instant>> = Mutex::new(None);
    {
        let mut last = LAST_TOGGLE.lock().unwrap();
        if last.is_some_and(|at| at.elapsed() < Duration::from_millis(300)) {
            return;
        }
        *last = Some(Instant::now());
    }
    let Some(panel) = app.get_webview_window("panel") else {
        return;
    };
    if panel.is_visible().unwrap_or(false) {
        hide_panel_window(app, &panel);
        return;
    }
    if LAST_AUTO_HIDE
        .lock()
        .unwrap()
        .is_some_and(|at| at.elapsed() < Duration::from_millis(250))
    {
        return;
    }
    position_panel(app, &panel, anchor);
    show_panel_window(app, &panel);
    let _ = app.emit_to("panel", "panel-shown", ());
}

fn update_tray(app: &AppHandle, text: &str, connected: bool) {
    if let Some(tray) = app.tray_by_id("main") {
        let _ = tray.set_tooltip(Some(text));
        #[cfg(target_os = "macos")]
        {
            let _ = tray.set_title(Some(if connected { "" } else { "!" }));
        }
    }
}

fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "打开网页", true, None::<&str>)?;
    let reconnect = MenuItem::with_id(app, "reconnect", "重新连接", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &open, &reconnect, &quit])?;
    #[cfg(target_os = "macos")]
    let icon = tauri::include_image!("icons/tray-template.png");
    #[cfg(not(target_os = "macos"))]
    let icon = tauri::include_image!("icons/Square44x44Logo.png");
    let builder = TrayIconBuilder::with_id("main")
        .icon(icon)
        .tooltip("amem：正在启动")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                position,
                ..
            } = event
            {
                toggle_panel(tray.app_handle(), Some(position));
            }
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main(app),
            "open" => {
                let state = app.state::<AppState>();
                if let Err(error) = open_web_url(app, &state, None) {
                    let _ = app.emit("client-error", error);
                    show_main(app);
                }
            }
            "reconnect" => {
                app.state::<AppState>().reconnect(app);
            }
            "quit" => app.exit(0),
            _ => {}
        });
    // macOS 菜单栏图标用模板渲染，跟随浅色/深色菜单栏自动反色。
    #[cfg(target_os = "macos")]
    let builder = builder.icon_as_template(true);
    builder.build(app)?;
    Ok(())
}

fn open_web_url(app: &AppHandle, state: &AppState, path: Option<&str>) -> Result<(), String> {
    let (base, token) = state.connection()?;
    let mut url = format!("{base}/");
    if let Some(path) = path {
        let path = path.trim_start_matches('/');
        if !path.is_empty() {
            url = format!("{base}/{path}");
        }
    }
    let separator = if url.contains('?') { '&' } else { '?' };
    let url = format!("{url}{separator}token={}", urlencoding::encode(&token));
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| format!("打开浏览器失败: {e}"))
}

#[tauri::command]
fn get_settings(app: AppHandle, state: State<'_, AppState>) -> Value {
    let settings = state.settings.lock().unwrap().clone();
    json!({
        "settings": settings,
        "token_configured": load_token().ok().flatten().is_some(),
        "ssh_available": ssh_available(),
        "platform": std::env::consts::OS,
        "version": app.package_info().version.to_string(),
    })
}

#[tauri::command]
fn save_settings(
    app: AppHandle,
    state: State<'_, AppState>,
    settings: Settings,
    token: Option<String>,
) -> Result<(), String> {
    validate_settings(&settings)?;
    state.save_settings_file(&settings)?;
    *state.settings.lock().unwrap() = settings;
    if let Some(token) = token {
        if token.is_empty() {
            delete_token()?;
        } else {
            set_token(&token)?;
        }
    }
    state.reconnect(&app);
    Ok(())
}

#[tauri::command]
fn clear_token() -> Result<(), String> {
    delete_token()
}

#[tauri::command]
fn get_runtime(state: State<'_, AppState>) -> RuntimeView {
    let runtime = state.runtime.lock().unwrap();
    RuntimeView {
        connected: runtime.connected,
        latency_ms: runtime.latency_ms,
        last_check: runtime.last_check,
        last_error: runtime.last_error.clone(),
        local_port: runtime.local_port,
        retrying: runtime.retry_at.is_some(),
        retry_count: runtime.retry_count,
    }
}

#[tauri::command]
async fn fetch_status(state: State<'_, AppState>) -> Result<Value, String> {
    let (base, token) = state.connection()?;
    tauri::async_runtime::spawn_blocking(move || {
        api_request(&base, &token, "GET", "/api/status", None)
    })
    .await
    .map_err(|e| format!("请求线程失败: {e}"))?
}

#[tauri::command]
async fn fetch_proposals(state: State<'_, AppState>) -> Result<Value, String> {
    let (base, token) = state.connection()?;
    tauri::async_runtime::spawn_blocking(move || {
        api_request(&base, &token, "GET", "/api/proposals", None)
    })
    .await
    .map_err(|e| format!("请求线程失败: {e}"))?
}

#[tauri::command]
async fn fetch_proposal(state: State<'_, AppState>, name: String) -> Result<Value, String> {
    let (base, token) = state.connection()?;
    tauri::async_runtime::spawn_blocking(move || {
        api_request(
            &base,
            &token,
            "GET",
            &format!("/api/proposals/{}", urlencoding::encode(&name)),
            None,
        )
    })
    .await
    .map_err(|e| format!("请求线程失败: {e}"))?
}

#[tauri::command]
async fn decide_proposal(
    state: State<'_, AppState>,
    name: String,
    index: i64,
    action: String,
) -> Result<Value, String> {
    if action != "accept" && action != "reject" {
        return Err("未知操作".into());
    }
    let (base, token) = state.connection()?;
    tauri::async_runtime::spawn_blocking(move || {
        api_request(
            &base,
            &token,
            "POST",
            &format!("/api/proposals/{}/decision", urlencoding::encode(&name)),
            Some(json!({"index": index, "action": action})),
        )
    })
    .await
    .map_err(|e| format!("请求线程失败: {e}"))?
}

#[tauri::command]
fn open_web(app: AppHandle, state: State<'_, AppState>, path: Option<String>) -> Result<(), String> {
    open_web_url(&app, &state, path.as_deref())
}

#[tauri::command]
fn reconnect(app: AppHandle, state: State<'_, AppState>) {
    state.reconnect(&app);
}

#[tauri::command]
fn show_main_window(app: AppHandle) {
    show_main(&app);
}

#[tauri::command]
fn hide_panel(app: AppHandle) {
    if let Some(panel) = app.get_webview_window("panel") {
        hide_panel_window(&app, &panel);
    }
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    app.exit(0);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default().plugin(tauri_plugin_opener::init());
    #[cfg(target_os = "macos")]
    let builder = builder.plugin(tauri_nspanel::init());
    let app = builder
        .setup(|app| {
            let state = AppState::new(app.handle());
            app.manage(state);
            #[cfg(target_os = "macos")]
            if let Some(panel) = app.get_webview_window("panel") {
                match panel.to_panel::<AmemPanel>() {
                    Ok(ns_panel) => {
                        let _ = ns_panel.add_style_mask(
                            StyleMask::empty().nonactivating_panel().value(),
                        );
                        ns_panel.set_level(PanelLevel::Floating.value());
                        ns_panel.set_collection_behavior(
                            CollectionBehavior::new()
                                .can_join_all_spaces()
                                .full_screen_auxiliary()
                                .value(),
                        );
                    }
                    Err(error) => eprintln!("菜单栏面板初始化失败: {error}"),
                }
            }
            if let Err(error) = setup_tray(app.handle()) {
                eprintln!("托盘图标初始化失败: {error}");
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(300));
                loop {
                    let state = handle.state::<AppState>();
                    state.tick(&handle);
                    let seconds = state.settings.lock().unwrap().poll_seconds.clamp(3, 120);
                    drop(state);
                    std::thread::sleep(Duration::from_secs(seconds));
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "panel" {
                if let tauri::WindowEvent::Focused(false) = event {
                    *LAST_AUTO_HIDE.lock().unwrap() = Some(Instant::now());
                    if let Some(panel) = window.app_handle().get_webview_window("panel") {
                        hide_panel_window(window.app_handle(), &panel);
                    }
                }
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_settings,
            save_settings,
            clear_token,
            get_runtime,
            get_agents,
            set_agent,
            save_vault_path,
            fetch_status,
            fetch_proposals,
            fetch_proposal,
            decide_proposal,
            open_web,
            reconnect,
            show_main_window,
            hide_panel,
            panel_alive,
            quit_app
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");
    app.run(|app_handle, event| {
        match event {
            tauri::RunEvent::Exit => {
                app_handle.state::<AppState>().drop_tunnel();
            }
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Reopen { .. } => show_main(app_handle),
            _ => {}
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ssh_settings() -> Settings {
        Settings {
            ssh_host: "example.com".into(),
            ssh_user: "ubuntu".into(),
            ..Settings::default()
        }
    }

    #[test]
    fn accepts_valid_ssh_settings() {
        assert!(validate_settings(&ssh_settings()).is_ok());
    }

    #[test]
    fn rejects_missing_ssh_fields() {
        let mut settings = ssh_settings();
        settings.ssh_host.clear();
        assert!(validate_settings(&settings).is_err());
        let mut settings = ssh_settings();
        settings.ssh_user.clear();
        assert!(validate_settings(&settings).is_err());
    }

    #[test]
    fn rejects_zero_ports() {
        let mut settings = ssh_settings();
        settings.ssh_port = 0;
        assert!(validate_settings(&settings).is_err());
        let mut settings = ssh_settings();
        settings.remote_port = 0;
        assert!(validate_settings(&settings).is_err());
    }

    #[test]
    fn direct_mode_requires_http_scheme() {
        let mut settings = Settings {
            mode: "direct".into(),
            direct_url: "http://127.0.0.1:8787".into(),
            ..Settings::default()
        };
        assert!(validate_settings(&settings).is_ok());
        settings.direct_url = "127.0.0.1:8787".into();
        assert!(validate_settings(&settings).is_err());
        settings.direct_url = "file:///etc/passwd".into();
        assert!(validate_settings(&settings).is_err());
    }

    #[test]
    fn poll_interval_is_bounded() {
        let mut settings = ssh_settings();
        settings.poll_seconds = 2;
        assert!(validate_settings(&settings).is_err());
        settings.poll_seconds = 121;
        assert!(validate_settings(&settings).is_err());
        settings.poll_seconds = 10;
        assert!(validate_settings(&settings).is_ok());
    }

    #[test]
    fn expands_tilde_against_home() {
        let home = std::env::var("HOME").expect("HOME is set in tests");
        assert_eq!(
            expand_home("~/.ssh/id_ed25519"),
            PathBuf::from(home).join(".ssh/id_ed25519")
        );
        assert_eq!(expand_home("/tmp/key"), PathBuf::from("/tmp/key"));
    }

    #[test]
    fn managed_block_is_idempotent_and_removable() {
        let vault = PathBuf::from("/tmp/vault");
        let block = managed_block(AgentStyle::AtRef, &vault);
        let original = "# 用户内容\n";
        let with_block = upsert_managed_block(original, &block, &vault);
        assert!(with_block.contains(AGENT_BEGIN));
        assert!(with_block.contains(AGENT_END));
        assert_eq!(upsert_managed_block(&with_block, &block, &vault), with_block);
        assert_eq!(strip_managed_blocks(&with_block, &vault), original);
    }

    #[test]
    fn strips_legacy_blocks() {
        let vault = PathBuf::from("/tmp/vault");
        let legacy = "before\n<!-- amem: 共享记忆 -->\n\
@/tmp/vault/AGENT_PROTOCOL.md\n@/tmp/vault/MEMORY.md\nafter\n";
        assert_eq!(strip_managed_blocks(legacy, &vault), "before\nafter\n");

        let instruction = "开始任何任务前，先读取并遵守 /tmp/vault/AGENT_PROTOCOL.md，\
再读 /tmp/vault/MEMORY.md。\nkeep\n";
        assert_eq!(strip_managed_blocks(instruction, &vault), "keep\n");

        let gemini_legacy = "@/tmp/vault/AGENT_PROTOCOL.md\n@/tmp/vault/MEMORY.md\nkeep\n";
        assert_eq!(strip_managed_blocks(gemini_legacy, &vault), "keep\n");
    }

    #[test]
    fn strip_preserves_other_vault_references() {
        let vault = PathBuf::from("/tmp/vault");
        let pair = "@/work/vault/AGENT_PROTOCOL.md\n@/work/vault/MEMORY.md\n";
        assert_eq!(strip_managed_blocks(pair, &vault), pair);
        let single = "@/work/vault/AGENT_PROTOCOL.md\nkeep\n";
        assert_eq!(strip_managed_blocks(single, &vault), single);
        let instruction = "开始任何任务前，先读取并遵守 /work/vault/AGENT_PROTOCOL.md，\
再读 /work/vault/MEMORY.md。\n";
        assert_eq!(strip_managed_blocks(instruction, &vault), instruction);
    }

    #[test]
    fn legacy_strip_keeps_user_notes_after_marker() {
        let vault = PathBuf::from("/tmp/vault");
        let text = "<!-- amem: 共享记忆 -->\n@/tmp/vault/AGENT_PROTOCOL.md\n\
@/tmp/vault/MEMORY.md\n用户自己的笔记：参见 /work/vault/MEMORY.md，很重要\n";
        assert_eq!(
            strip_managed_blocks(text, &vault),
            "用户自己的笔记：参见 /work/vault/MEMORY.md，很重要\n"
        );
    }

    #[test]
    fn orphan_begin_does_not_swallow_user_content() {
        let vault = PathBuf::from("/tmp/vault");
        let text = "<!-- amem:begin -->\n用户内容\n<!-- amem:begin -->\n\
@/tmp/vault/AGENT_PROTOCOL.md\n@/tmp/vault/MEMORY.md\n<!-- amem:end -->\n";
        let stripped = strip_managed_blocks(text, &vault);
        assert!(stripped.contains("用户内容"));
        assert_eq!(stripped.matches(AGENT_BEGIN).count(), 1);
        assert!(!stripped.contains(AGENT_END));
    }

    #[test]
    fn malformed_markers_are_detected() {
        assert!(has_malformed_markers("<!-- amem:begin -->\nkeep\n"));
        assert!(has_malformed_markers("<!-- amem:end -->\n"));
        assert!(has_malformed_markers(
            "<!-- amem:begin -->\n<!-- amem:begin -->\n<!-- amem:end -->\n"
        ));
        assert!(!has_malformed_markers(
            "<!-- amem:begin -->\nx\n<!-- amem:end -->\n"
        ));
    }

    #[test]
    fn text_has_managed_requires_managed_shape() {
        let vault = PathBuf::from("/tmp/vault");
        assert!(!text_has_managed(
            "用户笔记：参见 /tmp/vault/AGENT_PROTOCOL.md\n",
            &vault
        ));
        assert!(text_has_managed(
            "开始任何任务前，先读取并遵守 /tmp/vault/AGENT_PROTOCOL.md，\
再读 /tmp/vault/MEMORY.md。\n",
            &vault
        ));
        assert!(text_has_managed(
            "@/tmp/vault/AGENT_PROTOCOL.md\n@/tmp/vault/MEMORY.md\n",
            &vault
        ));
        let block = managed_block(AgentStyle::AtRef, &vault);
        assert!(text_has_managed(&upsert_managed_block("", &block, &vault), &vault));
    }

    #[test]
    fn upsert_replaces_legacy_from_other_vault() {
        let new = PathBuf::from("/tmp/new-vault");
        let legacy = "<!-- amem: 共享记忆 -->\n@/tmp/old-vault/AGENT_PROTOCOL.md\n\
@/tmp/old-vault/MEMORY.md\n用户内容\n";
        let updated = upsert_managed_block(legacy, &managed_block(AgentStyle::AtRef, &new), &new);
        assert!(updated.contains("用户内容"));
        assert!(!updated.contains("/tmp/old-vault/AGENT_PROTOCOL.md"));
        assert!(updated.contains("/tmp/new-vault/AGENT_PROTOCOL.md"));
        assert_eq!(updated.matches(AGENT_BEGIN).count(), 1);

        let instruction = "开始任何任务前，先读取并遵守 /tmp/old-vault/AGENT_PROTOCOL.md，\
再读 /tmp/old-vault/MEMORY.md。\n";
        let updated =
            upsert_managed_block(instruction, &managed_block(AgentStyle::Instruction, &new), &new);
        assert!(!updated.contains("/tmp/old-vault/AGENT_PROTOCOL.md"));
        assert!(updated.contains("/tmp/new-vault/MEMORY.md"));
    }

    #[test]
    fn recognizes_windows_style_legacy_refs() {
        assert!(is_protocol_ref_shape("@C:\\vault\\AGENT_PROTOCOL.md"));
        assert!(is_memory_ref_shape("@C:\\vault\\MEMORY.md"));
        assert!(is_instruction_shape(
            "开始任何任务前，先读取并遵守 C:\\vault\\AGENT_PROTOCOL.md，\
再读 C:\\vault\\MEMORY.md。"
        ));
        assert!(!is_protocol_ref_shape("@C:\\vault\\notes.md"));
    }

    #[test]
    fn upsert_preserves_line_endings_and_trailing_blanks() {
        let vault = PathBuf::from("/tmp/vault");
        let block = managed_block(AgentStyle::AtRef, &vault);
        for original in ["用户内容\r\n\r\n", "用户内容\n\n"] {
            let with_block = upsert_managed_block(original, &block, &vault);
            assert_eq!(strip_managed_blocks(&with_block, &vault), original);
        }
        let crlf = upsert_managed_block("用户内容\r\n", &block, &vault);
        assert!(crlf.contains("<!-- amem:begin -->\r\n"));
        assert!(!crlf.contains("<!-- amem:begin -->\n"));
    }

    #[test]
    fn integration_state_follows_vault_path() {
        let old = PathBuf::from("/tmp/old-vault");
        let new = PathBuf::from("/tmp/new-vault");
        let block = managed_block(AgentStyle::AtRef, &old);
        let text = upsert_managed_block("", &block, &old);
        assert!(text_has_managed(&text, &old));
        assert!(!text_has_managed(&text, &new));
    }

    #[test]
    fn vault_requires_protocol_and_memory() {
        let base = std::env::temp_dir().join(format!("amem-vault-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        assert!(!vault_is_ready(&base));
        std::fs::write(base.join("AGENT_PROTOCOL.md"), "p").unwrap();
        assert!(!vault_is_ready(&base));
        std::fs::write(base.join("MEMORY.md"), "m").unwrap();
        assert!(vault_is_ready(&base));
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(unix)]
    #[test]
    fn write_atomic_preserves_symlinks() {
        use std::os::unix::fs::symlink;
        let base = std::env::temp_dir().join(format!("amem-link-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let target = base.join("target.md");
        let link = base.join("link.md");
        std::fs::write(&target, "old\n").unwrap();
        symlink(&target, &link).unwrap();
        write_atomic(&link, "new\n").unwrap();
        assert!(std::fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "new\n");
        assert_eq!(std::fs::read_to_string(&link).unwrap(), "new\n");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn read_errors_are_not_treated_as_empty() {
        let base = std::env::temp_dir().join(format!("amem-read-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let dir = base.join("not-a-file");
        std::fs::create_dir_all(&dir).unwrap();
        assert!(read_config_text(&dir).is_err());
        assert_eq!(read_config_text(&base.join("missing")).unwrap(), "");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn backup_failure_is_reported() {
        let base = std::env::temp_dir().join(format!("amem-backup-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let file = base.join("AGENTS.md");
        std::fs::write(&file, "x\n").unwrap();
        std::fs::create_dir_all(format!("{}.bak.amem", file.display())).unwrap();
        assert!(backup_file(&file).is_err());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn claude_settings_adds_and_removes_vault() {
        let base =
            std::env::temp_dir().join(format!("amem-agent-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join(".claude")).unwrap();
        let settings = base.join(".claude/settings.json");
        std::fs::write(&settings, "{\"theme\":\"dark\"}\n").unwrap();
        let vault = base.join("memory-vault");
        std::fs::create_dir_all(&vault).unwrap();
        let vault_text = vault.to_string_lossy().to_string();

        update_claude_settings(&base, &vault, true).unwrap();
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&settings).unwrap()).unwrap();
        assert_eq!(data["theme"], "dark");
        assert_eq!(data["cleanupPeriodDays"], 36500);
        let dirs = data["permissions"]["additionalDirectories"]
            .as_array()
            .unwrap();
        assert!(dirs.iter().any(|value| value.as_str() == Some(&vault_text)));

        update_claude_settings(&base, &vault, false).unwrap();
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&settings).unwrap()).unwrap();
        assert_eq!(data["cleanupPeriodDays"], 36500);
        let dirs = data["permissions"]["additionalDirectories"]
            .as_array()
            .unwrap();
        assert!(!dirs.iter().any(|value| value.as_str() == Some(&vault_text)));
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn claude_settings_handles_null_fields() {
        let base =
            std::env::temp_dir().join(format!("amem-null-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join(".claude")).unwrap();
        let settings = base.join(".claude/settings.json");
        let vault = base.join("memory-vault");
        std::fs::create_dir_all(&vault).unwrap();
        let vault_text = vault.to_string_lossy().to_string();

        std::fs::write(&settings, "{\"permissions\":null}\n").unwrap();
        update_claude_settings(&base, &vault, true).unwrap();
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&settings).unwrap()).unwrap();
        assert!(data["permissions"]["additionalDirectories"]
            .as_array()
            .unwrap()
            .iter()
            .any(|value| value.as_str() == Some(&vault_text)));

        std::fs::write(
            &settings,
            "{\"permissions\":{\"additionalDirectories\":null}}\n",
        )
        .unwrap();
        update_claude_settings(&base, &vault, true).unwrap();
        update_claude_settings(&base, &vault, false).unwrap();
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&settings).unwrap()).unwrap();
        assert!(data["permissions"]["additionalDirectories"]
            .as_array()
            .unwrap()
            .is_empty());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn claude_settings_matches_tilde_entries() {
        let base =
            std::env::temp_dir().join(format!("amem-tilde-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join(".claude")).unwrap();
        let vault = base.join("memory-vault");
        std::fs::create_dir_all(&vault).unwrap();
        let settings = base.join(".claude/settings.json");
        std::fs::write(
            &settings,
            "{\"permissions\":{\"additionalDirectories\":[\"~/memory-vault\"]}}\n",
        )
        .unwrap();

        update_claude_settings(&base, &vault, true).unwrap();
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&settings).unwrap()).unwrap();
        let dirs = data["permissions"]["additionalDirectories"]
            .as_array()
            .unwrap();
        assert_eq!(dirs.len(), 1);
        assert_eq!(dirs[0], "~/memory-vault");

        update_claude_settings(&base, &vault, false).unwrap();
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&settings).unwrap()).unwrap();
        assert!(data["permissions"]["additionalDirectories"]
            .as_array()
            .unwrap()
            .is_empty());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn backup_keeps_first_version() {
        let base =
            std::env::temp_dir().join(format!("amem-backup-keep-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let file = base.join("CLAUDE.md");
        std::fs::write(&file, "first\n").unwrap();
        backup_file(&file).unwrap();
        std::fs::write(&file, "second\n").unwrap();
        backup_file(&file).unwrap();
        assert_eq!(
            std::fs::read_to_string(format!("{}.bak.amem", file.display())).unwrap(),
            "first\n"
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn claude_settings_prunes_other_amem_vaults() {
        let base =
            std::env::temp_dir().join(format!("amem-prune-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join(".claude")).unwrap();
        let old_vault = base.join("old-vault");
        let new_vault = base.join("new-vault");
        for vault in [&old_vault, &new_vault] {
            std::fs::create_dir_all(vault).unwrap();
            std::fs::write(vault.join("AGENT_PROTOCOL.md"), "p").unwrap();
            std::fs::write(vault.join("MEMORY.md"), "m").unwrap();
        }
        let keep = base.join("notes");
        std::fs::create_dir_all(&keep).unwrap();
        let settings = base.join(".claude/settings.json");
        std::fs::write(
            &settings,
            serde_json::to_string(&json!({
                "permissions": {
                    "additionalDirectories": [
                        old_vault.to_string_lossy(),
                        keep.to_string_lossy()
                    ]
                }
            }))
            .unwrap(),
        )
        .unwrap();

        update_claude_settings(&base, &new_vault, true).unwrap();
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&settings).unwrap()).unwrap();
        let dirs = data["permissions"]["additionalDirectories"]
            .as_array()
            .unwrap();
        let paths: Vec<&str> = dirs.iter().filter_map(|value| value.as_str()).collect();
        assert!(!paths.contains(&old_vault.to_string_lossy().as_ref()));
        assert!(paths.contains(&new_vault.to_string_lossy().as_ref()));
        assert!(paths.contains(&keep.to_string_lossy().as_ref()));
        let _ = std::fs::remove_dir_all(&base);
    }
}
