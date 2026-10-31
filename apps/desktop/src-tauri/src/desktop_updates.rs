//! Verified, resumable downloads shared by full and differential updates.
use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::future::join_all;
use minisign_verify::{PublicKey, Signature};
use reqwest::{Client, Url};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{fs, io::Write, path::Path, sync::atomic::{AtomicBool, Ordering}, time::{Duration, Instant}};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};

pub const SOURCES: [&str; 2] = [
    "https://ai-trader-updates-hz.oss-cn-hangzhou.aliyuncs.com",
    "https://qilinshuzhi.com/ai-trader-updates",
];
const MAX_ARTIFACT: u64 = 300 * 1024 * 1024;

fn client_builder() -> reqwest::ClientBuilder {
    // Same crypto provider as Tauri's updater, also when resuming without a fresh check.
    let _ = rustls::crypto::ring::default_provider().install_default();
    Client::builder().connect_timeout(Duration::from_secs(6))
        .read_timeout(Duration::from_secs(6)).redirect(reqwest::redirect::Policy::none())
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Progress {
    pub stage: &'static str,
    pub downloaded_bytes: u64,
    pub total_bytes: Option<u64>,
    pub percent: Option<u8>,
    pub bytes_per_second: Option<u64>,
    pub remaining_seconds: Option<u64>,
    pub source: Option<&'static str>,
    pub incremental: bool,
}

pub fn emit(app: &AppHandle, progress: Progress) {
    let _ = app.emit("desktop-update-progress", progress);
}

pub async fn check(app: &AppHandle) -> Result<Option<Update>, String> {
    if let Ok(root) = app.path().app_cache_dir() { import_installer_seed(&root.join("updates")); }
    app.updater_builder()
        .endpoints(SOURCES.iter().map(|base| format!("{base}/latest.json").parse().unwrap()).collect())
        .map_err(|_| "desktop_update_configuration_invalid")?
        .timeout(Duration::from_secs(8)).build()
        .map_err(|_| "desktop_update_configuration_invalid")?
        .check().await.map_err(|_| "desktop_update_check_failed".into())
}

pub fn digest(bytes: &[u8]) -> String { format!("{:x}", Sha256::digest(bytes)) }

fn import_installer_seed(cache: &Path) {
    let seed = cache.join("installer-seed.bin");
    if !fs::metadata(&seed).is_ok_and(|m| m.len() <= MAX_ARTIFACT) { return; }
    if let Ok(bytes) = fs::read(&seed) {
        let target = cache.join(format!("{}.bin", digest(&bytes)));
        if target.exists() { let _ = fs::remove_file(&seed); }
        else { let _ = fs::rename(&seed, &target); }
        // This seed is only usable when a later signed manifest names its hash.
        retain_cache(cache, &target);
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ReleasePayload {
    schema_version: u8,
    version: String,
    sha256: String,
    size: u64,
    patches: Vec<Patch>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Patch {
    base_sha256: String,
    base_version: String,
    base_release: String,
    sha256: String,
    size: u64,
    file: String,
}

fn release_payload(update: &Update) -> Result<Option<ReleasePayload>, String> {
    let platform = if cfg!(target_os = "macos") { "darwin-aarch64" } else { "windows-x86_64" };
    let Some(envelope) = update.raw_json["platforms"][platform].get("integrity") else { return Ok(None); };
    let encoded = envelope["payload"].as_str().ok_or("desktop_update_signature_invalid")?;
    if encoded.len() > 32768 { return Err("desktop_update_signature_invalid".into()); }
    let bytes = STANDARD.decode(encoded).map_err(|_| "desktop_update_signature_invalid")?;
    verify(&bytes, envelope["signature"].as_str().ok_or("desktop_update_signature_invalid")?)?;
    let payload: ReleasePayload = serde_json::from_slice(&bytes).map_err(|_| "desktop_update_signature_invalid")?;
    if payload.schema_version != 1 || payload.version != update.version || payload.size > MAX_ARTIFACT
        || payload.size == 0 || !is_digest(&payload.sha256) || payload.patches.len() > 2 {
        return Err("desktop_update_signature_invalid".into());
    }
    for patch in &payload.patches {
        if !is_digest(&patch.base_sha256) || !is_digest(&patch.sha256) || patch.size > MAX_ARTIFACT
            || patch.size == 0 || patch.file != format!("{}.bsdiff", patch.base_sha256)
            || patch.base_version.len() > 40 || patch.base_release.len() > 80 {
            return Err("desktop_update_signature_invalid".into());
        }
    }
    Ok(Some(payload))
}

fn is_digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

fn apply_patch(base: &[u8], patch: &[u8], payload: &ReleasePayload) -> Result<Vec<u8>, String> {
    let patcher = qbsdiff::Bspatch::new(patch).map_err(|_| "desktop_update_patch_invalid")?;
    if patcher.hint_target_size() != payload.size { return Err("desktop_update_patch_invalid".into()); }
    // Fixed output storage also bounds malformed patch expansion.
    let mut target = vec![0; payload.size as usize];
    let mut writer = std::io::Cursor::new(target.as_mut_slice());
    patcher.apply(base, &mut writer).map_err(|_| "desktop_update_patch_invalid")?;
    if writer.position() != payload.size || digest(&target) != payload.sha256 {
        return Err("desktop_update_patch_invalid".into());
    }
    Ok(target)
}

fn retain_cache(cache: &Path, current: &Path) {
    let Ok(entries) = fs::read_dir(cache) else { return; };
    let mut complete: Vec<_> = entries.flatten().filter(|e| {
        e.path().extension().is_some_and(|v| v == "bin") && e.path() != current
    }).collect();
    complete.sort_by_key(|e| std::cmp::Reverse(e.metadata().and_then(|m| m.modified()).ok()));
    for entry in complete.into_iter().skip(1) { let _ = fs::remove_file(entry.path()); }
    if let Ok(entries) = fs::read_dir(cache) {
        for entry in entries.flatten() {
            if entry.path().extension().is_some_and(|v| v == "part") && entry.metadata().ok()
                .and_then(|m| m.modified().ok()).and_then(|t| t.elapsed().ok())
                .is_some_and(|age| age > Duration::from_secs(7 * 86400)) {
                let _ = fs::remove_file(entry.path());
            }
        }
    }
}

pub fn verify(bytes: &[u8], signature: &str) -> Result<(), String> {
    let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
    let decode = |value: &str| STANDARD.decode(value).ok().and_then(|v| String::from_utf8(v).ok())
        .ok_or("desktop_update_signature_invalid");
    let key = decode(config["plugins"]["updater"]["pubkey"].as_str().unwrap())?;
    let signature = decode(signature)?;
    let key = PublicKey::decode(&key).map_err(|_| "desktop_update_signature_invalid")?;
    let signature = Signature::decode(&signature).map_err(|_| "desktop_update_signature_invalid")?;
    key.verify(bytes, &signature, true).map_err(|_| "desktop_update_signature_invalid".into())
}

fn sources(url: &Url) -> Result<Vec<Url>, String> {
    let suffix = SOURCES.iter().find_map(|base| url.as_str().strip_prefix(&format!("{base}/releases/")))
        .ok_or("desktop_update_source_invalid")?;
    if suffix.contains("..") || url.query().is_some() || url.fragment().is_some() {
        return Err("desktop_update_source_invalid".into());
    }
    SOURCES.iter().map(|base| Url::parse(&format!("{base}/releases/{suffix}"))
        .map_err(|_| "desktop_update_source_invalid".into())).collect()
}

fn cancelled(cancel: &AtomicBool) -> Result<(), String> {
    if cancel.load(Ordering::SeqCst) { Err("desktop_update_cancelled".into()) } else { Ok(()) }
}

fn source_name(url: &Url) -> &'static str {
    match url.host_str() {
        Some("ai-trader-updates-hz.oss-cn-hangzhou.aliyuncs.com") => "OSS",
        _ => "官网",
    }
}

async fn ranked_sources(client: &Client, urls: Vec<Url>) -> Vec<Url> {
    let mut ranked = join_all(urls.into_iter().map(|url| async move {
        let started = Instant::now();
        let speed = async {
            let mut response = client.get(url.clone()).header("Range", "bytes=0-131071")
                .timeout(Duration::from_secs(8)).send().await.ok()?;
            if response.status().as_u16() != 206 { return None; }
            let mut received = 0;
            while let Some(chunk) = response.chunk().await.ok()? {
                received += chunk.len();
                if received >= 131072 { break; }
            }
            Some(received as f64 / started.elapsed().as_secs_f64().max(0.001))
        }.await.unwrap_or(0.0);
        (url, speed)
    })).await;
    ranked.sort_by(|a, b| b.1.total_cmp(&a.1));
    ranked.into_iter().map(|(url, _)| url).collect()
}

/// A partial file is never installed. Every completed artifact is signature-checked by the caller.
pub async fn transfer(
    url: &Url, partial: &Path, cancel: &AtomicBool,
    mut progress: impl FnMut(u64, u64, u64, &'static str),
) -> Result<Vec<u8>, String> {
    let client = client_builder().build().map_err(|_| "desktop_update_download_failed")?;
    cancelled(cancel)?;
    let urls = ranked_sources(&client, sources(url)?).await;
    transfer_from_sources(&client, &urls, partial, cancel, &mut progress).await
}

async fn transfer_from_sources(
    client: &Client, urls: &[Url], partial: &Path, cancel: &AtomicBool,
    progress: &mut impl FnMut(u64, u64, u64, &'static str),
) -> Result<Vec<u8>, String> {
    let attempts = urls.len() * 2;
    for (attempt_number, url) in urls.iter().cycle().take(attempts).enumerate() {
        cancelled(cancel)?;
        let offset = fs::metadata(partial).map(|m| m.len()).unwrap_or(0);
        if offset > MAX_ARTIFACT { let _ = fs::remove_file(partial); continue; }
        let attempt = async {
            let mut response = client.get(url.clone()).header("Range", format!("bytes={offset}-"))
                .send().await.map_err(|_| ())?;
            let status = response.status().as_u16();
            if status == 416 { let _ = fs::remove_file(partial); return Err(()); }
            let (start, total) = if status == 206 {
                let range = response.headers().get("content-range").and_then(|v| v.to_str().ok()).ok_or(())?;
                let (span, size) = range.strip_prefix("bytes ").and_then(|v| v.split_once('/')).ok_or(())?;
                let (first, last) = span.split_once('-').ok_or(())?;
                let first: u64 = first.parse().map_err(|_| ())?;
                let last: u64 = last.parse().map_err(|_| ())?;
                let total: u64 = size.parse().map_err(|_| ())?;
                if first != offset || last < first || last >= total { return Err(()); }
                (offset, total)
            } else if status == 200 {
                (0, response.content_length().ok_or(())?)
            } else { return Err(()); };
            if total == 0 || total > MAX_ARTIFACT { return Err(()); }
            let mut file = fs::OpenOptions::new().create(true).write(true)
                .append(start > 0).truncate(start == 0).open(partial).map_err(|_| ())?;
            let began = Instant::now();
            let mut received = start;
            let mut last_event = Instant::now() - Duration::from_secs(1);
            while let Some(chunk) = response.chunk().await.map_err(|_| ())? {
                if cancel.load(Ordering::SeqCst) { return Err(()); }
                received += chunk.len() as u64;
                if received > total { return Err(()); }
                file.write_all(&chunk).map_err(|_| ())?;
                let speed = ((received - start) as f64 / began.elapsed().as_secs_f64().max(0.01)) as u64;
                if last_event.elapsed() >= Duration::from_millis(250) || received == total {
                    progress(received, total, speed, source_name(url));
                    last_event = Instant::now();
                }
                // Try another mirror when the connection is effectively stalled, but keep
                // slow-yet-working sources alive: a 1 Mbps mirror still finishes, and the
                // final source must always be allowed to complete instead of timing out.
                if attempt_number + 1 < attempts && began.elapsed() > Duration::from_secs(12)
                    && speed < 64 * 1024 { return Err(()); }
            }
            if received != total { return Err(()); }
            file.sync_all().map_err(|_| ())?;
            fs::read(partial).map_err(|_| ())
        }.await;
        if let Ok(bytes) = attempt { cancelled(cancel)?; return Ok(bytes); }
    }
    cancelled(cancel)?;
    Err("desktop_update_download_failed".into())
}

pub async fn download(app: &AppHandle, update: &Update, cache: &Path, cancel: &AtomicBool) -> Result<Vec<u8>, String> {
    fs::create_dir_all(cache).map_err(|_| "desktop_update_cache_unavailable")?;
    let payload = release_payload(update)?;
    let key = payload.as_ref().map(|p| p.sha256.clone()).unwrap_or_else(|| digest(update.signature.as_bytes()));
    let complete = cache.join(format!("{key}.bin"));
    if let Ok(bytes) = fs::read(&complete) {
        if verify(&bytes, &update.signature).is_ok()
            && payload.as_ref().is_none_or(|p| bytes.len() as u64 == p.size && digest(&bytes) == p.sha256) {
            return Ok(bytes);
        }
    }
    if let Some(payload) = &payload {
        for patch in &payload.patches {
            cancelled(cancel)?;
            if patch.size >= payload.size * 9 / 10 { continue; }
            let Ok(base) = fs::read(cache.join(format!("{}.bin", patch.base_sha256))) else { continue; };
            if digest(&base) != patch.base_sha256 { continue; }
            let patch_url = update.download_url.join(&patch.file).map_err(|_| "desktop_update_source_invalid")?;
            let partial = cache.join(format!("{}.part", patch.sha256));
            let result = transfer(&patch_url, &partial, cancel, |received, total, speed, source| {
                emit(app, Progress { stage: "downloading", downloaded_bytes: received, total_bytes: Some(total),
                    percent: super::update_download_percent(received, Some(total)),
                    bytes_per_second: Some(speed), remaining_seconds: (speed > 0).then(|| (total - received) / speed.max(1)),
                    source: Some(source), incremental: true });
            }).await;
            cancelled(cancel)?;
            if let Ok(bytes) = result {
                if bytes.len() as u64 == patch.size && digest(&bytes) == patch.sha256 {
                    if let Ok(target) = apply_patch(&base, &bytes, payload) {
                        verify(&target, &update.signature)?;
                        let temporary = cache.join(format!("{key}.verified"));
                        fs::write(&temporary, &target).map_err(|_| "desktop_update_cache_unavailable")?;
                        let _ = fs::remove_file(&complete);
                        fs::rename(&temporary, &complete).map_err(|_| "desktop_update_cache_unavailable")?;
                        let _ = fs::remove_file(&partial);
                        retain_cache(cache, &complete);
                        return Ok(target);
                    }
                }
            }
            // Failed or corrupt patches never replace an installed application.
            let _ = fs::remove_file(&partial);
        }
    }
    let partial = cache.join(format!("{key}.part"));
    let bytes = transfer(&update.download_url, &partial, cancel, |received, total, speed, source| {
        emit(app, Progress { stage: "downloading", downloaded_bytes: received, total_bytes: Some(total),
            percent: super::update_download_percent(received, Some(total)),
            bytes_per_second: Some(speed), remaining_seconds: (speed > 0).then(|| (total - received) / speed.max(1)),
            source: Some(source), incremental: false });
    }).await?;
    if let Err(error) = verify(&bytes, &update.signature) {
        let _ = fs::remove_file(&partial);
        return Err(error);
    }
    if payload.as_ref().is_some_and(|p| bytes.len() as u64 != p.size || digest(&bytes) != p.sha256) {
        let _ = fs::remove_file(&partial);
        return Err("desktop_update_signature_invalid".into());
    }
    // Cache by content hash, including old manifests, so future patches can find the base.
    let complete = cache.join(format!("{}.bin", digest(&bytes)));
    let _ = fs::remove_file(&complete);
    fs::rename(&partial, &complete).map_err(|_| "desktop_update_cache_unavailable")?;
    retain_cache(cache, &complete);
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::{Cursor, Read}, net::TcpListener};

    fn payload(target: &[u8]) -> ReleasePayload {
        ReleasePayload { schema_version: 1, version: "1.4.0".into(), sha256: digest(target),
            size: target.len() as u64, patches: vec![] }
    }

    #[test]
    fn patch_restores_exact_artifact_and_rejects_wrong_base_or_output_size() {
        let base = b"unchanged SDK contents, old application";
        let target = b"unchanged SDK contents, new application with fix";
        let mut patch = Vec::new();
        qbsdiff::Bsdiff::new(base, target).compare(Cursor::new(&mut patch)).unwrap();
        assert_eq!(apply_patch(base, &patch, &payload(target)).unwrap(), target);
        assert!(apply_patch(b"corrupt base", &patch, &payload(target)).is_err());
        let mut descriptor = payload(target);
        descriptor.size += 1;
        assert!(apply_patch(base, &patch, &descriptor).is_err());
        assert!(apply_patch(base, b"broken patch", &payload(target)).is_err());
    }

    #[test]
    fn untrusted_package_and_unapproved_hosts_cannot_be_installed() {
        assert!(verify(b"untrusted executable", "invalid signature").is_err());
        assert!(sources(&Url::parse("https://evil.example/ai-trader-updates/releases/a/b").unwrap()).is_err());
        assert!(sources(&Url::parse("http://qilinshuzhi.com/ai-trader-updates/releases/a/b").unwrap()).is_err());
        assert_eq!(sources(&Url::parse(&format!("{}/releases/1.4.0-a/file", SOURCES[0])).unwrap()).unwrap().len(), SOURCES.len());
    }

    #[test]
    fn installer_seed_is_content_addressed_without_trusting_it_as_an_update() {
        let directory = std::env::temp_dir().join(format!("ai-trader-seed-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("installer-seed.bin"), b"previous installer").unwrap();
        import_installer_seed(&directory);
        assert!(!directory.join("installer-seed.bin").exists());
        assert_eq!(fs::read(directory.join(format!("{}.bin", digest(b"previous installer")))).unwrap(), b"previous installer");
        assert!(verify(b"previous installer", "").is_err());
        fs::remove_dir_all(directory).unwrap();
    }

    fn response_server(status: &str, headers: &str, body: &[u8], expected_range: &'static str) -> (Url, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = Url::parse(&format!("http://{}/artifact", listener.local_addr().unwrap())).unwrap();
        let response = format!("HTTP/1.1 {status}\r\nConnection: close\r\n{headers}\r\n").into_bytes();
        let body = body.to_vec();
        let handle = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut request = vec![0; 4096];
            let size = socket.read(&mut request).unwrap();
            assert!(String::from_utf8_lossy(&request[..size]).to_lowercase().contains(expected_range));
            socket.write_all(&response).unwrap();
            socket.write_all(&body).unwrap();
        });
        (url, handle)
    }

    #[test]
    fn download_resumes_partial_file_from_another_source() {
        let directory = std::env::temp_dir().join(format!("ai-trader-resume-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("artifact.part");
        fs::write(&path, b"first").unwrap();
        let (failed, one) = response_server("503 Unavailable", "Content-Length: 0\r\n", b"", "range: bytes=5-");
        let (working, two) = response_server("206 Partial Content", "Content-Length: 6\r\nContent-Range: bytes 5-10/11\r\n", b"second", "range: bytes=5-");
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let client = client_builder().no_proxy().build().unwrap();
        let bytes = runtime.block_on(transfer_from_sources(&client, &[failed, working], &path, &AtomicBool::new(false), &mut |_, _, _, _| {})).unwrap();
        assert_eq!(bytes, b"firstsecond");
        one.join().unwrap(); two.join().unwrap();
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn ignored_range_restarts_cleanly_and_cancel_keeps_partial() {
        let directory = std::env::temp_dir().join(format!("ai-trader-range-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("artifact.part");
        fs::write(&path, b"old partial").unwrap();
        let (url, server) = response_server("200 OK", "Content-Length: 3\r\n", b"new", "range: bytes=11-");
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let client = client_builder().no_proxy().build().unwrap();
        let bytes = runtime.block_on(transfer_from_sources(&client, &[url], &path, &AtomicBool::new(false), &mut |_, _, _, _| {})).unwrap();
        assert_eq!(bytes, b"new");
        server.join().unwrap();
        assert_eq!(runtime.block_on(transfer_from_sources(&client, &[], &path, &AtomicBool::new(true), &mut |_, _, _, _| {})).unwrap_err(), "desktop_update_cancelled");
        assert_eq!(fs::read(&path).unwrap(), b"new");
        fs::remove_dir_all(directory).unwrap();
    }
}
