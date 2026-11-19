use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    path::PathBuf,
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

fn expand_home(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return PathBuf::from(home).join(rest);
        }
        if let Ok(home) = std::env::var("USERPROFILE") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(path)
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
}
