mod desktop_updates;
mod agent_window;
mod xiaomi_remote;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};
use std::time::{Duration, Instant};
use sysinfo::{Pid, ProcessesToUpdate, System};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, State,
};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tokio::sync::Mutex as AsyncMutex;
use zeroize::Zeroize;

const CREDENTIAL_DIRECTORY: &str = "private-credentials";
const MAIN_WINDOW_LABEL: &str = "main";
const TRAY_ICON_ID: &str = "ai-trader-tray";
const TRAY_SHOW_ID: &str = "tray-show";
const TRAY_QUIT_ID: &str = "tray-quit";
const DESKTOP_CACHE_RESET_INIT_SCRIPT: &str = r#"
if (window.location.hostname === "tauri.localhost") {
  const workerApi = window.navigator.serviceWorker;
  if (workerApi && typeof workerApi.register === "function") {
    try {
      Object.defineProperty(workerApi, "register", {
        configurable: true,
        value: () => Promise.reject(new Error("desktop_service_worker_disabled"))
      });
    } catch (_) {}
  }

  const marker = "ai-trader-desktop-cache-reset-v1";
  if (window.sessionStorage.getItem(marker) !== "done") {
    window.sessionStorage.setItem(marker, "running");
    const controlled = Boolean(workerApi && workerApi.controller);
    const unregister = workerApi && typeof workerApi.getRegistrations === "function"
      ? workerApi.getRegistrations()
          .then((registrations) => Promise.allSettled(registrations.map((item) => item.unregister())))
          .catch(() => [])
      : Promise.resolve([]);
    const clearCaches = window.caches && typeof window.caches.keys === "function"
      ? window.caches.keys()
          .then((names) => Promise.allSettled(names.map((name) => window.caches.delete(name))))
          .catch(() => [])
      : Promise.resolve([]);

    Promise.allSettled([unregister, clearCaches]).then(() => {
      window.sessionStorage.setItem(marker, "done");
      if (controlled) window.location.reload();
    });
  }
}
"#;

/// WKWebView refuses `getUserMedia` from `tauri://localhost` ("The operation is insecure."): the
/// custom scheme is not a secure context and the media-capture preference demands one. wry already
/// grants the capture permission prompt; these two WebKit preferences (private, set through KVC the
/// way wry sets `allowsInlineMediaPlayback`) let the real-time voice call open the microphone.
/// Windows (WebView2, `http://tauri.localhost` is trustworthy) needs nothing.
#[cfg(target_os = "macos")]
fn allow_microphone_capture(app: AppHandle) -> impl FnOnce(tauri::webview::PlatformWebview) {
    move |webview| {
        use objc2_foundation::{NSNumber, NSObjectNSKeyValueCoding, NSString};
        use objc2_web_kit::WKWebView;
        // Each preference is set on its own so one unknown key (a WebKit that renamed it) cannot hide
        // the others; the outcome and the values read back go to the desktop log for support.
        let mut keys: Vec<(&str, bool)> = vec![
            ("mediaCaptureRequiresSecureConnection", false),
            ("mediaDevicesEnabled", true),
        ];
        // Test builds on a machine without a microphone: WebKit's mock capture devices let the call
        // path past getUserMedia so the socket and the Runtime side can be exercised.
        if std::env::var("AI_TRADER_DESKTOP_MOCK_CAPTURE").as_deref() == Ok("1") { keys.push(("mockCaptureDevicesEnabled", true)); }
        let mut report = Vec::new();
        for (key, value) in keys {
            let outcome = objc2::exception::catch(std::panic::AssertUnwindSafe(|| unsafe {
                let view = &*(webview.inner() as *const WKWebView);
                let preferences = view.configuration().preferences();
                preferences.setValue_forKey(Some(&NSNumber::numberWithBool(value)), &NSString::from_str(key));
                preferences.valueForKey(&NSString::from_str(key)).map(|read| format!("{read:?}"))
            }));
            report.push(match outcome {
                Ok(read) => format!("{key}={}", read.unwrap_or_else(|| "nil".into())),
                Err(error) => format!("{key}=exception({})", error.map(|e| format!("{e:?}")).unwrap_or_default()),
            });
        }
        desktop_log(&app, &format!("webkit preferences: {}", report.join(" ")));
    }
}

/// The window buttons sit inside the web header (titleBarStyle Overlay): 15pt in from the left
/// edge and centred on the header's 44pt height (`--fdw-header` at the standard density, see
/// WorkspaceHeader.css, which starts the brand after them). tauri.conf.json `trafficLightPosition`
/// moved neither x nor y on macOS 26 (tested 2026-10-08), so the buttons are placed here at
/// startup and again after the window events on which AppKit lays its title bar out anew.
#[cfg(target_os = "macos")]
fn place_traffic_lights(window: &tauri::Window) {
    use objc2_app_kit::{NSWindow, NSWindowButton};
    use objc2_foundation::NSPoint;
    const LEFT: f64 = 15.0;
    const HEADER_HEIGHT: f64 = 44.0;
    // Full screen moves the title bar into its own reveal-on-hover window; leave that to AppKit
    // (leaving full screen resizes the window, which places the buttons again).
    if window.is_fullscreen().unwrap_or(false) { return; }
    let Ok(pointer) = window.ns_window() else { return };
    let ns_window = unsafe { &*(pointer as *const NSWindow) };
    let buttons: Vec<_> = [NSWindowButton::CloseButton, NSWindowButton::MiniaturizeButton, NSWindowButton::ZoomButton]
        .into_iter()
        .filter_map(|kind| ns_window.standardWindowButton(kind))
        .collect();
    let [close, miniaturize, ..] = buttons.as_slice() else { return };
    // The button's parent is the title bar view, whose parent is the title bar container.
    let Some(title_bar) = (unsafe { close.superview() }) else { return };
    let Some(container) = (unsafe { title_bar.superview() }) else { return };
    let spacing = miniaturize.frame().origin.x - close.frame().origin.x;
    // The title bar becomes the header's height so the buttons can sit at its middle.
    let mut frame = container.frame();
    frame.size.height = HEADER_HEIGHT;
    frame.origin.y = ns_window.frame().size.height - HEADER_HEIGHT;
    container.setFrame(frame);
    let mut bar = title_bar.frame();
    bar.origin.y = 0.0;
    bar.size.height = HEADER_HEIGHT;
    title_bar.setFrame(bar);
    for (index, button) in buttons.iter().enumerate() {
        let size = button.frame().size;
        button.setFrameOrigin(NSPoint::new(LEFT + index as f64 * spacing, ((HEADER_HEIGHT - size.height) / 2.0).round()));
    }
}

/// Uncaught frontend errors reach the desktop log (stderr and `<app log dir>/frontend.log`) so a
/// blank window can be diagnosed from the machine that shows it: no devtools, no GUI needed.
const DESKTOP_FRONTEND_ERROR_INIT_SCRIPT: &str = r#"
(() => {
  const report = (kind, message, detail) => {
    try {
      const internals = window.__TAURI_INTERNALS__;
      if (!internals || typeof internals.invoke !== "function") return;
      internals.invoke("desktop_frontend_error", { error: { kind, message: String(message).slice(0, 4000), detail: String(detail || "").slice(0, 4000) } }).catch(() => {});
    } catch (_) {}
  };
  window.addEventListener("error", (event) => report("error", event.message || event.error, (event.error && event.error.stack) || `${event.filename || ""}:${event.lineno || 0}`));
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    report("unhandledrejection", reason && reason.message ? reason.message : reason, reason && reason.stack);
  });
})();
"#;

#[derive(Deserialize)]
struct FrontendError { kind: String, message: String, detail: String }

#[tauri::command]
fn desktop_frontend_error(app: AppHandle, error: FrontendError) {
    desktop_log(&app, &format!("frontend {} {} {}", error.kind, error.message.replace('\n', " "), error.detail.replace('\n', " | ")));
}

fn chrono_like_timestamp() -> String {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    format!("{now}")
}

#[derive(Default)]
struct DesktopProcesses {
    runtime: Option<CommandChild>,
    sidecar: Option<CommandChild>,
}

#[derive(Default)]
struct DesktopInstallGate(AsyncMutex<()>);

#[derive(Default)]
struct DesktopUpdateState {
    installing: AtomicBool,
    cancelled: AtomicBool,
    /// Set by the frontend once React has rendered; the startup watchdog reads it.
    frontend_ready: AtomicBool,
}

/// A window that never reports ready (the 1.5.3 blank page: the bundle embedded a frontend built
/// for another base path, so no script ran and the frontend-driven updater never checked) would
/// otherwise strand the user. Without the ready signal the desktop checks for an update itself
/// and installs a newer version, because a blank window has no button to press.
const FRONTEND_READY_GRACE: Duration = Duration::from_secs(45);

#[derive(Default)]
struct DesktopLifecycleState {
    quitting: AtomicBool,
}

fn stop_desktop_processes(processes: &mut DesktopProcesses) {
    if let Some(runtime) = processes.runtime.take() {
        stop_command_child(runtime);
    }
    if let Some(sidecar) = processes.sidecar.take() {
        stop_command_child(sidecar);
    }
}

/// One process in a snapshot: (pid, parent pid, start time in seconds since the epoch).
type ProcessEntry = (u32, u32, u64);
/// A process identity: Windows reuses PIDs, so a PID alone does not name a process.
type ProcessIdentity = (u32, u64);

fn process_snapshot(system: &System) -> Vec<ProcessEntry> {
    system
        .processes()
        .iter()
        .map(|(pid, process)| {
            (
                pid.as_u32(),
                process.parent().map(Pid::as_u32).unwrap_or_default(),
                process.start_time(),
            )
        })
        .collect()
}

fn managed_process_tree_ids(processes: &DesktopProcesses) -> Vec<ProcessIdentity> {
    let roots = [
        processes.runtime.as_ref().map(CommandChild::pid),
        processes.sidecar.as_ref().map(CommandChild::pid),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>();
    let snapshot = process_snapshot(&System::new_all());
    let mut tracked = Vec::new();
    for root in roots {
        if let Some(&(_, _, started)) = snapshot.iter().find(|entry| entry.0 == root) {
            tracked.push((root, started));
        }
        tracked.extend(descendant_processes(root, &snapshot));
    }
    tracked.sort_unstable();
    tracked.dedup();
    tracked
}

/// The process that holds `pid` now, if it is still the one identified by `started`.
fn same_process(system: &mut System, (pid, started): ProcessIdentity) -> Option<&sysinfo::Process> {
    let pid = Pid::from_u32(pid);
    system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
    system
        .process(pid)
        .filter(|process| process.start_time() == started)
}

fn wait_for_process_exit(processes: &[ProcessIdentity], timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        let mut system = System::new();
        if processes
            .iter()
            .all(|&identity| same_process(&mut system, identity).is_none())
        {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Processes in `root_pid`'s tree with their start times. Windows keeps a stale parent PID
/// after the parent exits and may hand that PID to a new process, so a child that started
/// before its recorded parent belongs to an earlier holder of the PID and is not ours.
fn descendant_processes(root_pid: u32, snapshot: &[ProcessEntry]) -> Vec<ProcessIdentity> {
    let Some(&(_, _, root_started)) = snapshot.iter().find(|entry| entry.0 == root_pid) else {
        return Vec::new();
    };
    let mut descendants: Vec<ProcessIdentity> = Vec::new();
    let mut parents = vec![(root_pid, root_started)];
    while let Some((parent, parent_started)) = parents.pop() {
        for &(pid, ppid, started) in snapshot {
            if ppid == parent
                && pid != root_pid
                && started >= parent_started
                && !descendants.iter().any(|&(known, _)| known == pid)
            {
                descendants.push((pid, started));
                parents.push((pid, started));
            }
        }
    }
    descendants
}

fn stop_process_descendants(root_pid: u32) {
    let descendants = descendant_processes(root_pid, &process_snapshot(&System::new_all()));
    let mut system = System::new();
    for &identity in descendants.iter().rev() {
        // Re-check identity right before the kill: the PID may have been reused since the snapshot.
        if let Some(process) = same_process(&mut system, identity) {
            process.kill();
        }
    }
}

fn stop_command_child(child: CommandChild) {
    // PyInstaller one-file binaries launch a second worker process. Killing
    // only the outer Tauri CommandChild leaves that worker orphaned.
    stop_process_descendants(child.pid());
    let _ = child.kill();
}

fn stop_managed_desktop_processes(app: &AppHandle) {
    let state = app.state::<Mutex<DesktopProcesses>>();
    if let Ok(mut processes) = state.lock() {
        stop_desktop_processes(&mut processes);
    };
}

fn restore_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn request_desktop_quit(app: &AppHandle) {
    app.state::<DesktopLifecycleState>()
        .quitting
        .store(true, Ordering::SeqCst);
    stop_managed_desktop_processes(app);
    app.exit(0);
}

fn should_hide_on_close(quitting: bool) -> bool {
    !quitting
}

fn setup_desktop_tray(app: &tauri::App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, TRAY_SHOW_ID, "显示 Sente", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, TRAY_QUIT_ID, "退出 Sente", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    let mut tray = TrayIconBuilder::with_id(TRAY_ICON_ID)
        .menu(&menu)
        .tooltip("Sente")
        .show_menu_on_left_click(false)
        // The bundled small icon is the coloured Sente mark (stone + blue dot). Treating it
        // as a macOS template would collapse the opaque artwork into a solid square.
        .icon_as_template(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            TRAY_SHOW_ID => restore_main_window(app),
            TRAY_QUIT_ID => request_desktop_quit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            let restore = matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                } | TrayIconEvent::DoubleClick {
                    button: MouseButton::Left,
                    ..
                }
            );
            if restore {
                restore_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray.build(app)?;
    Ok(())
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopUpdateCheck {
    current_version: String,
    available: bool,
    version: Option<String>,
    notes: Option<String>,
    pub_date: Option<String>,
}

fn credential_root(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|error| error.to_string())?
        .join(CREDENTIAL_DIRECTORY))
}

fn ensure_private_directory(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|error| error.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn write_private_file(path: &Path, bytes: &[u8], create_new: bool) -> Result<(), String> {
    let mut options = OpenOptions::new();
    options
        .write(true)
        .truncate(!create_new)
        .create(!create_new)
        .create_new(create_new);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(|error| error.to_string())?;
    file.write_all(bytes).map_err(|error| error.to_string())?;
    file.sync_all().map_err(|error| error.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn agent_connection_info(app: AppHandle) -> Result<serde_json::Value, String> {
    let root = credential_root(&app)?;
    ensure_private_directory(&root)?;
    let path = root.join("agent-device.json");
    if !path.exists() {
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).map_err(|_| "无法创建设备标识".to_string())?;
        let identifier: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let bytes = serde_json::to_vec(&json!({"deviceId":identifier})).map_err(|_| "设备标识无效".to_string())?;
        // Another window may win creation; it will be read below.
        if let Err(error) = write_private_file(&path, &bytes, true) { if !path.exists() { return Err(error); } }
    }
    let value: serde_json::Value = serde_json::from_slice(&fs::read(path).map_err(|_| "设备标识不可用")?)
        .map_err(|_| "设备标识无效")?;
    let device = value["deviceId"].as_str().filter(|id| id.len() == 32 && id.chars().all(|c| c.is_ascii_hexdigit()))
        .ok_or("设备标识无效")?;
    let executable = std::env::current_exe().map_err(|_| "安装路径不可用")?
        .with_file_name(if cfg!(windows) {"ai-trader-runtime.exe"} else {"ai-trader-runtime"});
    Ok(json!({"deviceId":device, "platform":std::env::consts::OS, "version":app.package_info().version.to_string(),
        "nativeCapture":cfg!(any(target_os="macos", target_os="windows")),
        "mcp":{"command":executable, "args":["mcp"]}}))
}

const OWNER_RUNTIME_URL: &str = "https://ai-runtime.qilinshuzhi.com:8443";
const CLOUD_SESSION_FILE: &str = "cloud-session.v1.json";

fn validate_cloud_connection(url: &str, token: &str) -> Result<(), String> {
    if url != OWNER_RUNTIME_URL || !token.starts_with("v1.") || token.split('.').count() != 3
        || token.len() > 4096 || !token.is_ascii() || token.chars().any(char::is_whitespace) {
        return Err("cloud_session_invalid".to_string());
    }
    Ok(())
}

#[tauri::command]
fn save_cloud_connection(app: AppHandle, url: String, mut token: String) -> Result<(), String> {
    let result = (|| {
        validate_cloud_connection(&url, &token)?;
        let root = credential_root(&app)?;
        ensure_private_directory(&root)?;
        let mut bytes = serde_json::to_vec(&serde_json::json!({"url": url, "token": token, "authMode": "owner-password"}))
            .map_err(|_| "cloud_session_invalid".to_string())?;
        let result = write_private_file(&root.join(CLOUD_SESSION_FILE), &bytes, false)
            .and_then(|_| write_private_file(&root.join("agent-cli.json"), &bytes, false));
        bytes.zeroize();
        result
    })();
    token.zeroize();
    result
}

#[tauri::command]
fn load_cloud_connection(app: AppHandle) -> Result<Option<serde_json::Value>, String> {
    let root = credential_root(&app)?;
    let mut bytes = match fs::read(root.join(CLOUD_SESSION_FILE)) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("cloud_session_unavailable".to_string()),
    };
    let parsed: Result<serde_json::Value, _> = serde_json::from_slice(&bytes);
    bytes.zeroize();
    let value = parsed.map_err(|_| "cloud_session_invalid".to_string())?;
    validate_cloud_connection(value["url"].as_str().unwrap_or(""), value["token"].as_str().unwrap_or(""))?;
    // Selecting the cloud account also selects the authenticated MCP target.
    let mut serialized = serde_json::to_vec(&value).map_err(|_| "cloud_session_invalid".to_string())?;
    let result = write_private_file(&root.join("agent-cli.json"), &serialized, false);
    serialized.zeroize();
    result?;
    Ok(Some(value))
}

#[tauri::command]
fn clear_cloud_connection(app: AppHandle) -> Result<(), String> {
    let root = credential_root(&app)?;
    for name in [CLOUD_SESSION_FILE, "agent-cli.json"] {
        let path = root.join(name);
        if name == "agent-cli.json" {
            let is_cloud = fs::read(&path).ok().and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
                .is_some_and(|v| v["authMode"] == "owner-password");
            if !is_cloud { continue; }
        }
        match fs::remove_file(path) {
            Ok(()) => (),
            Err(error) if error.kind() == ErrorKind::NotFound => (),
            Err(_) => return Err("cloud_session_clear_failed".to_string()),
        }
    }
    Ok(())
}

/// Open web pages or a feedback draft to the fixed support address. Mail may
/// contain only a subject and body; it cannot add recipients or other headers.
fn external_url_allowed(raw: &str) -> bool {
    match tauri::Url::parse(raw) {
        Ok(url) if url.scheme() == "mailto" => {
            if raw.chars().any(char::is_control)
                || url.host_str().is_some()
                || url.path() != "lzy619202200@163.com"
                || url.fragment().is_some()
            {
                return false;
            }
            let (mut subject_seen, mut body_seen) = (false, false);
            url.query_pairs().all(|(key, value)| match key.as_ref() {
                "subject" if !subject_seen && !value.chars().any(char::is_control) => {
                    subject_seen = true;
                    true
                }
                "body" if !body_seen => {
                    body_seen = true;
                    true
                }
                _ => false,
            })
        }
        Ok(url) => (url.scheme() == "https" || url.scheme() == "http")
            && url.username().is_empty()
            && url.password().is_none(),
        Err(_) => false,
    }
}

#[tauri::command]
fn open_external_url(app: AppHandle, url: String) -> Result<(), String> {
    if !external_url_allowed(&url) {
        return Err("external_url_forbidden".to_string());
    }
    #[allow(deprecated)]
    app.shell().open(url, None).map_err(|_| "external_url_open_failed".to_string())
}

#[tauri::command]
async fn check_desktop_update(app: AppHandle) -> Result<DesktopUpdateCheck, String> {
    let current_version = app.package_info().version.to_string();
    let update = desktop_updates::check(&app).await?;
    Ok(match update {
        Some(update) => DesktopUpdateCheck {
            current_version,
            available: true,
            version: Some(update.version),
            notes: update.body,
            pub_date: update.date.map(|date| date.to_string()),
        },
        None => DesktopUpdateCheck {
            current_version,
            available: false,
            version: None,
            notes: None,
            pub_date: None,
        },
    })
}

#[tauri::command]
fn cancel_desktop_update(state: State<'_, DesktopUpdateState>) {
    state.cancelled.store(true, Ordering::SeqCst);
}

#[tauri::command]
async fn install_desktop_update(
    app: AppHandle,
    expected_version: String,
    state: State<'_, DesktopUpdateState>,
    processes: State<'_, Mutex<DesktopProcesses>>,
    install_gate: State<'_, DesktopInstallGate>,
) -> Result<(), String> {
    if state.installing.swap(true, Ordering::SeqCst) {
        return Err("desktop_update_already_running".to_string());
    }

    state.cancelled.store(false, Ordering::SeqCst);
    let result = async {
        let update = desktop_updates::check(&app).await?
            .ok_or_else(|| "desktop_update_not_available".to_string())?;
        ensure_expected_update_version(&expected_version, &update.version)?;
        download_and_install_update(&app, update, &state, &processes, &install_gate).await
    }
    .await;

    if let Err(error) = result {
        state.installing.store(false, Ordering::SeqCst);
        return Err(error);
    }
    app.restart();
}

/// Download, stop the managed processes, install; the caller restarts on success.
async fn download_and_install_update(
    app: &AppHandle,
    update: tauri_plugin_updater::Update,
    state: &DesktopUpdateState,
    processes: &Mutex<DesktopProcesses>,
    install_gate: &DesktopInstallGate,
) -> Result<(), String> {
    {
        let app = app.clone();
        let cache = app.path().app_cache_dir().map_err(|_| "desktop_update_cache_unavailable")?.join("updates");
        let bytes = desktop_updates::download(&app, &update, &cache, &state.cancelled).await?;
        if state.cancelled.load(Ordering::SeqCst) { return Err("desktop_update_cancelled".to_string()); }
        // Serialize process collection, shutdown, and installation.
        let _guard = install_gate.0.lock().await;
        if state.cancelled.load(Ordering::SeqCst) { return Err("desktop_update_cancelled".to_string()); }
        desktop_updates::emit(&app, desktop_updates::Progress {
            stage: "installing", downloaded_bytes: bytes.len() as u64,
            total_bytes: Some(bytes.len() as u64), percent: Some(100),
            bytes_per_second: None, remaining_seconds: None, source: None, incremental: false,
        });
        let process_ids = {
            let mut processes = processes
                .lock()
                .map_err(|_| "desktop_state_poisoned".to_string())?;
            let process_ids = managed_process_tree_ids(&processes);
            stop_desktop_processes(&mut processes);
            process_ids
        };
        if !wait_for_process_exit(&process_ids, Duration::from_secs(10)) {
            return Err("desktop_update_processes_still_running".to_string());
        }
        update
            .install(bytes)
            .map_err(|_| "desktop_update_install_failed".to_string())
    }
}

/// 设置 → 数据与隐私 → 导出诊断日志: the tail of this machine's frontend.log (the desktop shell's own
/// lines plus uncaught frontend errors) for the diagnostics file. Never the credential store.
#[derive(Serialize)]
struct FrontendLogTail { path: String, text: String, truncated: bool }

#[tauri::command]
fn desktop_frontend_log(app: AppHandle, max_bytes: Option<usize>) -> Result<FrontendLogTail, String> {
    let path = app.path().app_log_dir().map_err(|e| e.to_string())?.join("frontend.log");
    let bytes = fs::read(&path).map_err(|e| format!("frontend_log_unreadable: {e}"))?;
    let limit = max_bytes.unwrap_or(200 * 1024).max(1024);
    let truncated = bytes.len() > limit;
    let tail = if truncated { &bytes[bytes.len() - limit..] } else { &bytes[..] };
    let mut text = String::from_utf8_lossy(tail).into_owned();
    if truncated { if let Some(cut) = text.find('\n') { text = text[cut + 1..].to_string(); } }
    Ok(FrontendLogTail { path: path.to_string_lossy().into_owned(), text, truncated })
}

/// Save-as dialog for an export (diagnostics, conversation, review); returns the chosen path, or
/// None when the user cancelled. The frontend passes the contents; nothing else is written.
#[tauri::command]
async fn desktop_save_text_file(app: AppHandle, default_name: String, contents: String) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let chosen = tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().set_file_name(&default_name).blocking_save_file()
    }).await.map_err(|e| e.to_string())?;
    let Some(path) = chosen else { return Ok(None) };
    let path = path.into_path().map_err(|e| e.to_string())?;
    fs::write(&path, contents.as_bytes()).map_err(|e| format!("save_failed: {e}"))?;
    Ok(Some(path.to_string_lossy().into_owned()))
}

/// Desktop notifications (设置 → 通知): the WebView has no Notification API, so fills, cancels, the
/// emergency stop and briefs go through the OS notification centre. The frontend applies the
/// account's notify preferences before calling; this only delivers.
#[tauri::command]
fn desktop_notify(app: AppHandle, title: String, body: Option<String>) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    let mut builder = app.notification().builder().title(title);
    if let Some(body) = body { builder = builder.body(body); }
    builder.show().map_err(|e| e.to_string())
}

#[tauri::command]
fn desktop_notification_permission(app: AppHandle, request: bool) -> Result<String, String> {
    use tauri_plugin_notification::{NotificationExt, PermissionState};
    let state = if request { app.notification().request_permission() } else { app.notification().permission_state() }
        .map_err(|e| e.to_string())?;
    Ok(match state { PermissionState::Granted => "granted", PermissionState::Denied => "denied", _ => "default" }.to_string())
}

#[tauri::command]
fn desktop_frontend_ready(state: State<'_, DesktopUpdateState>, build: Option<String>) {
    state.frontend_ready.store(true, Ordering::SeqCst);
    // The packaged-bundle smoke launches the app with this set and waits for the file: proof that
    // the embedded frontend actually ran, which is what a wrong-base bundle (1.5.3) never did.
    if let Ok(path) = std::env::var("AI_TRADER_DESKTOP_READY_FILE") {
        let _ = fs::write(path, format!("{}\n", build.as_deref().unwrap_or("ready")));
    }
}

/// Startup watchdog: with no ready signal after the grace period, check for an update on the
/// Rust side and install a newer version unattended; otherwise only leave a log line.
fn spawn_frontend_ready_watchdog(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(FRONTEND_READY_GRACE).await;
        let state = app.state::<DesktopUpdateState>();
        if state.frontend_ready.load(Ordering::SeqCst) { return; }
        desktop_log(&app, "frontend never reported ready; checking for an update from the desktop side");
        let update = match desktop_updates::check(&app).await {
            Ok(Some(update)) => update,
            Ok(None) => { desktop_log(&app, "no newer version available; the window stays as it is"); return; }
            Err(error) => { desktop_log(&app, &format!("update check failed: {error}")); return; }
        };
        if state.installing.swap(true, Ordering::SeqCst) { return; }
        state.cancelled.store(false, Ordering::SeqCst);
        desktop_log(&app, &format!("installing {} unattended", update.version));
        let processes = app.state::<Mutex<DesktopProcesses>>();
        let install_gate = app.state::<DesktopInstallGate>();
        match download_and_install_update(&app, update, &state, &processes, &install_gate).await {
            Ok(()) => app.restart(),
            Err(error) => { state.installing.store(false, Ordering::SeqCst); desktop_log(&app, &format!("unattended update failed: {error}")); }
        }
    });
}

fn desktop_log(app: &AppHandle, message: &str) {
    let line = format!("{} desktop {}\n", chrono_like_timestamp(), message);
    eprint!("{line}");
    if let Ok(dir) = app.path().app_log_dir() {
        let _ = fs::create_dir_all(&dir);
        if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(dir.join("frontend.log")) {
            let _ = file.write_all(line.as_bytes());
        }
    }
}

fn ensure_expected_update_version(expected: &str, actual: &str) -> Result<(), String> {
    if expected.trim().is_empty() || expected != actual {
        return Err("desktop_update_version_changed".to_string());
    }
    Ok(())
}

fn update_download_percent(downloaded_bytes: u64, total_bytes: Option<u64>) -> Option<u8> {
    total_bytes
        .filter(|total| *total > 0)
        .map(|total| ((u128::from(downloaded_bytes) * 100 / u128::from(total)).min(100)) as u8)
}

pub fn run() {
    let startup_script = format!("{}\n{}\nwindow.__AI_TRADER_LOCAL_FIXTURE__ = {};", DESKTOP_FRONTEND_ERROR_INIT_SCRIPT, DESKTOP_CACHE_RESET_INIT_SCRIPT,
        std::env::var("AI_TRADER_DESKTOP_SIDECAR_FIXTURE").as_deref() == Ok("1"));
    let app = tauri::Builder::default()
        .plugin(
            tauri::plugin::Builder::<tauri::Wry, ()>::new("desktop-cache-reset")
                .js_init_script(startup_script)
                .build(),
        )
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            restore_main_window(app);
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(Mutex::new(DesktopProcesses::default()))
        .manage(DesktopInstallGate::default())
        .manage(DesktopUpdateState::default())
        .manage(DesktopLifecycleState::default())
        .on_page_load(|window, payload| {
            #[cfg(target_os = "macos")]
            if window.label() == MAIN_WINDOW_LABEL && matches!(payload.event(), tauri::webview::PageLoadEvent::Started) {
                xiaomi_remote::stop();
            }
            #[cfg(not(target_os = "macos"))]
            let _ = (window, payload);
        })
        .setup(|app| {
            #[cfg(target_os = "macos")]
            xiaomi_remote::initialize(app.handle());
            #[cfg(target_os = "windows")]
            app.remove_menu()?;
            setup_desktop_tray(app)?;
            #[cfg(target_os = "macos")]
            if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
                let _ = window.with_webview(allow_microphone_capture(app.handle().clone()));
                place_traffic_lights(&window.as_ref().window());
            }
            spawn_frontend_ready_watchdog(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            xiaomi_remote::xiaomi_remote_connect,
            xiaomi_remote::xiaomi_remote_disconnect,
            xiaomi_remote::xiaomi_remote_write,
            agent_connection_info,
            agent_window::capture_agent_window,
            save_cloud_connection,
            load_cloud_connection,
            clear_cloud_connection,
            open_external_url,
            desktop_frontend_error,
            desktop_frontend_log,
            desktop_save_text_file,
            desktop_notify,
            desktop_notification_permission,
            desktop_frontend_ready,
            check_desktop_update,
            install_desktop_update,
            cancel_desktop_update,
        ])
        .on_window_event(|window, event| {
            #[cfg(target_os = "macos")]
            if window.label() == MAIN_WINDOW_LABEL {
                if let tauri::WindowEvent::Focused(focused) = event { xiaomi_remote::focus(*focused); }
            }
            #[cfg(target_os = "macos")]
            if matches!(event, tauri::WindowEvent::Resized(_) | tauri::WindowEvent::Focused(_) | tauri::WindowEvent::ThemeChanged(_) | tauri::WindowEvent::ScaleFactorChanged { .. }) {
                place_traffic_lights(window);
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let quitting = window
                    .state::<DesktopLifecycleState>()
                    .quitting
                    .load(Ordering::SeqCst);
                if should_hide_on_close(quitting) {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running AI Trader desktop");

    app.run(|app, event| match event {
        tauri::RunEvent::ExitRequested { .. } => {
            #[cfg(target_os = "macos")]
            xiaomi_remote::stop();
            app.state::<DesktopLifecycleState>()
                .quitting
                .store(true, Ordering::SeqCst);
            stop_managed_desktop_processes(app);
            // Also cover descendants outside the managed process state.
            stop_process_descendants(std::process::id());
        }
        // Keep the final event idempotent as a fallback for platform-specific
        // exits that do not expose a prior ExitRequested callback.
        tauri::RunEvent::Exit => stop_managed_desktop_processes(app),
        #[cfg(target_os = "macos")]
        tauri::RunEvent::Reopen { .. } => restore_main_window(app),
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn close_hides_window_until_an_explicit_quit() {
        assert!(should_hide_on_close(false));
        assert!(!should_hide_on_close(true));
    }

    #[test]
    fn desktop_startup_removes_legacy_pwa_state_before_client_scripts() {
        assert!(DESKTOP_CACHE_RESET_INIT_SCRIPT.contains("workerApi.getRegistrations()"));
        assert!(DESKTOP_CACHE_RESET_INIT_SCRIPT.contains("window.caches.keys()"));
        assert!(DESKTOP_CACHE_RESET_INIT_SCRIPT.contains("desktop_service_worker_disabled"));
        assert!(DESKTOP_CACHE_RESET_INIT_SCRIPT.contains("window.location.reload()"));
    }

    #[test]
    fn process_tree_cleanup_includes_pyinstaller_workers_and_nested_children() {
        let snapshot = vec![(101, 1, 10), (102, 101, 11), (103, 102, 12), (201, 1, 5)];
        let descendants = descendant_processes(101, &snapshot);
        assert_eq!(descendants, vec![(102, 11), (103, 12)]);
        assert!(!descendants.iter().any(|&(pid, _)| pid == 201));
    }

    #[test]
    fn process_tree_cleanup_skips_older_holders_of_a_reused_parent_pid() {
        // Same counterexample as scripts/smoke_desktop_lifecycle.py (#361/#525): WPS 8001 still
        // records ppid 6748 from an earlier process, then 6748 was reused by our root.
        let snapshot = vec![
            (6748, 1, 200),
            (8001, 6748, 100),
            (8002, 8001, 120),
            (9001, 6748, 210),
            (9002, 9001, 210),
        ];
        assert_eq!(descendant_processes(6748, &snapshot), vec![(9001, 210), (9002, 210)]);
        assert!(descendant_processes(4242, &snapshot).is_empty());
    }

    #[test]
    fn external_url_allowed_accepts_plain_http_and_https() {
        assert!(external_url_allowed("https://account.shinnytech.com/"));
        assert!(external_url_allowed("http://example.com/news?id=1#top"));
        assert!(external_url_allowed("https://www.ndrc.gov.cn/x"));
    }

    #[test]
    fn external_url_allowed_accepts_only_the_support_mail_draft() {
        assert!(external_url_allowed("mailto:lzy619202200@163.com"));
        assert!(external_url_allowed("mailto:lzy619202200@163.com?subject=Sente%20%E9%97%AE%E9%A2%98&body=%E6%8F%8F%E8%BF%B0%0D%0A%26%23%3F"));
        for url in [
            "mailto:other@example.com",
            "mailto:lzy619202200@163.com,other@example.com",
            "mailto:lzy619202200@163.com;other@example.com",
            "mailto:lzy619202200@163.com%2Cother@example.com",
            "mailto://lzy619202200@163.com",
            "mailto:lzy619202200@163.com#fragment",
            "mailto:lzy619202200@163.com?cc=other@example.com",
            "mailto:lzy619202200@163.com?bcc=other@example.com",
            "mailto:lzy619202200@163.com?to=other@example.com",
            "mailto:lzy619202200@163.com?%62cc=other@example.com",
            "mailto:lzy619202200@163.com?attach=/private/file",
            "mailto:lzy619202200@163.com?subject=one&subject=two",
            "mailto:lzy619202200@163.com?body=one&body=two",
            "mailto:lzy619202200@163.com?subject=hello%0D%0ABcc%3Aother@example.com",
            "mailto:lzy619202200@163.com?body=hello\nBcc:other@example.com",
        ] {
            assert!(!external_url_allowed(url), "accepted {url}");
        }
    }

    #[test]
    fn external_url_allowed_rejects_other_schemes_and_embedded_credentials() {
        assert!(!external_url_allowed("file:///etc/passwd"));
        assert!(!external_url_allowed("javascript:alert(1)"));
        assert!(!external_url_allowed("tauri://x"));
        assert!(!external_url_allowed("https://user:pass@example.com/"));
        assert!(!external_url_allowed("https://user@example.com/"));
    }

    #[test]
    fn external_url_allowed_rejects_invalid_urls() {
        assert!(!external_url_allowed("not a url"));
        assert!(!external_url_allowed(""));
        assert!(!external_url_allowed("://missing-scheme"));
    }

    #[test]
    fn cloud_session_cannot_redirect_mcp_or_accept_a_model_key() {
        assert!(validate_cloud_connection(OWNER_RUNTIME_URL, "v1.fixture.signature").is_ok());
        for url in ["http://ai-runtime.qilinshuzhi.com:8443", "https://evil.example", "https://ai-runtime.qilinshuzhi.com:8443@evil.example"] {
            assert!(validate_cloud_connection(url, "v1.fixture.signature").is_err());
        }
        for token in ["", "sk-model-key", "v1.bad token.signature", "v1.bad"] {
            assert!(validate_cloud_connection(OWNER_RUNTIME_URL, token).is_err());
        }
    }

    #[test]
    fn update_installation_is_bound_to_the_confirmed_version() {
        assert!(ensure_expected_update_version("0.8.6", "0.8.6").is_ok());
        assert_eq!(
            ensure_expected_update_version("0.8.6", "0.8.7").unwrap_err(),
            "desktop_update_version_changed"
        );
        assert_eq!(
            ensure_expected_update_version("", "0.8.6").unwrap_err(),
            "desktop_update_version_changed"
        );
    }

    #[test]
    fn updater_download_percentage_is_bounded_and_handles_unknown_totals() {
        assert_eq!(update_download_percent(0, Some(200)), Some(0));
        assert_eq!(update_download_percent(100, Some(200)), Some(50));
        assert_eq!(update_download_percent(200, Some(200)), Some(100));
        assert_eq!(update_download_percent(250, Some(200)), Some(100));
        assert_eq!(update_download_percent(10, Some(0)), None);
        assert_eq!(update_download_percent(10, None), None);
    }

    #[cfg(unix)]
    #[test]
    fn cloud_session_files_are_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let suffix = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "ai-trader-cloud-session-mode-test-{}-{suffix}",
            std::process::id()
        ));
        ensure_private_directory(&root).unwrap();
        let bytes = serde_json::to_vec(&json!({
            "url": OWNER_RUNTIME_URL,
            "token": "v1.fixture.signature",
            "authMode": "owner-password",
        })).unwrap();
        assert_eq!(
            fs::metadata(&root).unwrap().permissions().mode() & 0o777,
            0o700
        );
        for name in [CLOUD_SESSION_FILE, "agent-cli.json"] {
            write_private_file(&root.join(name), &bytes, false).unwrap();
            assert_eq!(
                fs::metadata(root.join(name)).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        fs::remove_dir_all(root).unwrap();
    }
}
