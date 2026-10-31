//! Native WebView capture. No desktop/screen capture and no input automation.
use base64::Engine;
use serde_json::{json, Value};
use tauri::WebviewWindow;

type CaptureSender = tokio::sync::oneshot::Sender<Result<Vec<u8>, String>>;

#[tauri::command]
pub async fn capture_agent_window(window: WebviewWindow) -> Result<Value, String> {
    let (sender, receiver) = tokio::sync::oneshot::channel();
    window.with_webview(move |webview| capture(webview, sender))
        .map_err(|_| "客户端截图暂不可用".to_string())?;
    let bytes = tokio::time::timeout(std::time::Duration::from_secs(4), receiver).await
        .map_err(|_| "截图等待超时".to_string())?
        .map_err(|_| "截图未完成".to_string())??;
    if bytes.len() > 2_000_000 { return Err("截图过大，请缩小窗口后重试".to_string()); }
    Ok(json!({"mimeType":"image/png", "data":base64::engine::general_purpose::STANDARD.encode(bytes)}))
}

#[cfg(target_os = "macos")]
fn capture(webview: tauri::webview::PlatformWebview, sender: CaptureSender) {
    use objc2::{AnyThread, MainThreadMarker};
    use objc2_app_kit::{NSBitmapImageFileType, NSBitmapImageRep, NSImage};
    use objc2_foundation::{NSDictionary, NSError, NSNumber};
    use objc2_web_kit::{WKSnapshotConfiguration, WKWebView};
    let sender = std::cell::RefCell::new(Some(sender));
    let callback = block2::RcBlock::new(move |image: *mut NSImage, error: *mut NSError| {
        let result = (|| {
            if !error.is_null() || image.is_null() { return Err("客户端截图失败".to_string()); }
            // WebKit invokes this block on the main thread and retains image
            // for the callback. Copy encoded bytes before returning.
            let image = unsafe { &*image };
            let tiff = image.TIFFRepresentation().ok_or("截图图像不可用")?;
            let bitmap = NSBitmapImageRep::initWithData(NSBitmapImageRep::alloc(), &tiff).ok_or("截图转换失败")?;
            let png = unsafe { bitmap.representationUsingType_properties(NSBitmapImageFileType::PNG, &NSDictionary::new()) }
                .ok_or("截图编码失败")?;
            Ok(png.to_vec())
        })();
        if let Some(sender) = sender.borrow_mut().take() { let _ = sender.send(result); }
    });
    // with_webview runs on the UI thread, using this exact window's WKWebView.
    let marker = MainThreadMarker::new().expect("WebView callback must run on the main thread");
    unsafe {
        let configuration = WKSnapshotConfiguration::new(marker);
        configuration.setSnapshotWidth(Some(&NSNumber::new_u32(1440)));
        configuration.setAfterScreenUpdates(true);
        let view = &*(webview.inner() as *const WKWebView);
        view.takeSnapshotWithConfiguration_completionHandler(Some(&configuration), &callback);
    }
}

#[cfg(target_os = "windows")]
fn capture(webview: tauri::webview::PlatformWebview, sender: CaptureSender) {
    use std::sync::{Arc, Mutex};
    use webview2_com::{CapturePreviewCompletedHandler, Microsoft::Web::WebView2::Win32::COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG};
    use windows::Win32::{System::Com::{STATFLAG_NONAME, STATSTG, STREAM_SEEK_SET}, UI::Shell::SHCreateMemStream};
    let sender = Arc::new(Mutex::new(Some(sender)));
    let callback_sender = sender.clone();
    let result = (|| unsafe {
        let stream = SHCreateMemStream(None).ok_or("截图缓冲区不可用")?;
        let target = stream.clone();
        let handler = CapturePreviewCompletedHandler::create(Box::new(move |status| {
            let result = (|| {
                status.map_err(|_| "客户端截图失败")?;
                let mut stat = STATSTG::default();
                target.Stat(&mut stat, STATFLAG_NONAME).map_err(|_| "截图长度不可用")?;
                if stat.cbSize > 2_000_000 { return Err("截图过大".to_string()); }
                target.Seek(0, STREAM_SEEK_SET, None).map_err(|_| "截图读取失败")?;
                let mut bytes = vec![0; stat.cbSize as usize];
                let mut read = 0;
                target.Read(bytes.as_mut_ptr().cast(), bytes.len() as u32, Some(&mut read)).ok().map_err(|_| "截图读取失败")?;
                if read as usize != bytes.len() { return Err("截图数据不完整".to_string()); }
                Ok(bytes)
            })();
            if let Some(sender) = callback_sender.lock().unwrap().take() { let _ = sender.send(result); }
            Ok(())
        }));
        webview.controller().CoreWebView2().map_err(|_| "窗口不可用")?
            .CapturePreview(COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG, &stream, &handler).map_err(|_| "截图启动失败")?;
        Ok::<(), String>(())
    })();
    if let Err(error) = result {
        if let Some(sender) = sender.lock().unwrap().take() { let _ = sender.send(Err(error)); }
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn capture(_webview: tauri::webview::PlatformWebview, sender: CaptureSender) {
    let _ = sender.send(Err("当前平台不支持原生截图".to_string()));
}
