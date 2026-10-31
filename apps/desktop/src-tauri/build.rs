fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        use std::{path::PathBuf, process::Command};
        let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
        let arch = if std::env::var("CARGO_CFG_TARGET_ARCH").as_deref() == Ok("aarch64") { "arm64" } else { "x86_64" };
        let deployment = std::env::var("MACOSX_DEPLOYMENT_TARGET").unwrap_or_else(|_| "10.15".into());
        let target = format!("{arch}-apple-macosx{deployment}");
        let status = Command::new("xcrun").args(["swiftc", "-swift-version", "5", "-parse-as-library", "-emit-library", "-static", "-target", &target,
            "src/xiaomi_remote.swift", "-o"]).arg(out.join("libxiaomi_remote.a")).status().expect("Swift compiler is required for the Mac input adapter");
        assert!(status.success(), "Mac remote input adapter failed to compile");
        println!("cargo:rerun-if-changed=src/xiaomi_remote.swift");
        println!("cargo:rustc-link-search=native={}", out.display());
        println!("cargo:rustc-link-search=native=/usr/lib/swift");
        let swift = Command::new("xcrun").args(["--find", "swiftc"]).output().expect("locate Swift toolchain");
        assert!(swift.status.success(), "Swift toolchain is unavailable");
        let swift_path = std::fs::canonicalize(String::from_utf8(swift.stdout).unwrap().trim()).unwrap();
        let compatibility = swift_path.parent().unwrap().parent().unwrap().join("lib/swift/macosx");
        println!("cargo:rustc-link-search=native={}", compatibility.display());
        println!("cargo:rustc-link-lib=static=xiaomi_remote");
        for framework in ["Foundation", "CoreBluetooth", "IOKit"] { println!("cargo:rustc-link-lib=framework={framework}"); }
    }
    // Windows shows the exe's version-info name (Task Manager, Startup apps, "Open with").
    // tauri-build derives ProductName/FileDescription from productName, which must stay
    // "AI Trader" (install dir, uninstall key, updater, artifacts). Patch only this build
    // script's view of the config so the exe resources say "Sente"; the app's own
    // package_info().name is generated from the real config and is unchanged.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut config = std::env::var("TAURI_CONFIG")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .map(|value| serde_json::from_str(&value).expect("TAURI_CONFIG is valid JSON"))
            .unwrap_or_else(|| serde_json::json!({}));
        config["productName"] = serde_json::json!("Sente");
        std::env::set_var("TAURI_CONFIG", config.to_string());
    }
    tauri_build::build()
}
