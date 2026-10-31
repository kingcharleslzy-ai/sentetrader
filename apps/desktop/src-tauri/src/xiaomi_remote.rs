//! Mac input adapter only. Authentication, ATVV decoding and PTT stay in the shared client.
use tauri::{Manager, WebviewWindow};

#[cfg(target_os = "macos")]
mod mac {
    use std::{ffi::{c_char, CStr}, sync::OnceLock};
    use tauri::{AppHandle, Emitter};
    static APP: OnceLock<AppHandle> = OnceLock::new();
    extern "C" {
        fn ai_trader_remote_init(callback: extern "C" fn(*const c_char));
        pub fn ai_trader_remote_connect();
        pub fn ai_trader_remote_disconnect();
        pub fn ai_trader_remote_focus(focused: bool);
        pub fn ai_trader_remote_write(bytes: *const u8, count: usize);
    }
    extern "C" fn packet(text: *const c_char) {
        if text.is_null() { return; }
        // Swift owns this NUL-terminated string until the synchronous callback returns.
        if let Ok(value) = serde_json::from_slice::<serde_json::Value>(unsafe { CStr::from_ptr(text) }.to_bytes()) {
            if let Some(app) = APP.get() { let _ = app.emit_to(super::super::MAIN_WINDOW_LABEL, "xiaomi-remote", value); }
        }
    }
    pub fn initialize(app: &AppHandle) { let _ = APP.set(app.clone()); unsafe { ai_trader_remote_init(packet) }; }
}

#[cfg(target_os = "macos")]
pub fn initialize(app: &tauri::AppHandle) { mac::initialize(app); }
#[cfg(target_os = "macos")]
pub fn focus(focused: bool) { unsafe { mac::ai_trader_remote_focus(focused) }; }
#[cfg(target_os = "macos")]
pub fn stop() { unsafe { mac::ai_trader_remote_disconnect() }; }

fn main_window(window: &WebviewWindow) -> Result<(), String> {
    if window.label() != super::MAIN_WINDOW_LABEL { return Err("遥控器只能连接主窗口".into()); }
    Ok(())
}

#[tauri::command]
pub fn xiaomi_remote_connect(window: WebviewWindow) -> Result<(), String> {
    main_window(&window)?;
    #[cfg(target_os = "macos")]
    {
        mac::initialize(window.app_handle());
        focus(window.is_focused().unwrap_or(false));
        unsafe { mac::ai_trader_remote_connect() };
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    Err("此版本原生遥控器仅支持 Mac".into())
}

#[tauri::command]
pub fn xiaomi_remote_disconnect(window: WebviewWindow) -> Result<(), String> {
    main_window(&window)?;
    #[cfg(target_os = "macos")]
    stop();
    Ok(())
}

#[tauri::command]
pub fn xiaomi_remote_write(window: WebviewWindow, bytes: Vec<u8>) -> Result<(), String> {
    main_window(&window)?;
    let permitted = bytes == [10, 1, 0, 0, 3, 3] || bytes == [12, 0] || (bytes.len() == 2 && bytes[0] == 14);
    if !permitted { return Err("遥控器控制指令无效".into()); }
    #[cfg(target_os = "macos")]
    { unsafe { mac::ai_trader_remote_write(bytes.as_ptr(), bytes.len()) }; Ok(()) }
    #[cfg(not(target_os = "macos"))]
    Err("此版本原生遥控器仅支持 Mac".into())
}
