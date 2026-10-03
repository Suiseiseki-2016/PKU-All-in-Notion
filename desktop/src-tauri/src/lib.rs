//! Desktop shell: spawn `pku-sync panel --no-browser`, wait for /healthz,
//! then navigate the system WebView to `/app`.
//!
//! Binary resolution (production first):
//! 1. Tauri `externalBin` sidecar next to this exe when `resources/runtime` exists
//! 2. Bundled relocatable Python under resource dir (`python -m pku_sync`)
//! 3. Dev fallback: UV_TOOL_BIN_DIR → ~/.local/bin → PATH

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter, Manager, RunEvent, Url, WindowEvent};

const PANEL_PORTS: [u16; 3] = [8791, 8792, 8793];
const HEALTHZ_TIMEOUT: Duration = Duration::from_secs(30);
const HEALTHZ_INTERVAL: Duration = Duration::from_millis(250);

struct PanelState {
    child: Mutex<Option<Child>>,
}

enum PanelLaunch {
    /// `pku-sync panel --no-browser` (sidecar or PATH shim).
    Exe { path: PathBuf },
    /// Bundled interpreter: `python -m pku_sync panel --no-browser`.
    Python { path: PathBuf },
}

#[cfg(windows)]
fn windows_user_https_proxy() -> Option<String> {
    use winreg::{enums::HKEY_CURRENT_USER, RegKey};

    let settings = RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        .ok()?;
    let enabled: u32 = settings.get_value("ProxyEnable").ok()?;
    if enabled == 0 {
        return None;
    }
    let raw: String = settings.get_value("ProxyServer").ok()?;
    let server = if raw.contains('=') {
        raw.split(';')
            .filter_map(|part| part.split_once('='))
            .find(|(kind, _)| kind.eq_ignore_ascii_case("https"))
            .or_else(|| {
                raw.split(';')
                    .filter_map(|part| part.split_once('='))
                    .find(|(kind, _)| kind.eq_ignore_ascii_case("http"))
            })
            .map(|(_, value)| value)
    } else {
        Some(raw.as_str())
    }?;
    let server = server.trim();
    if server.is_empty() {
        None
    } else {
        Some(server.to_owned())
    }
}

#[cfg(windows)]
fn proxy_override_bypasses_host(override_list: &str, host: &str) -> bool {
    let host = host.to_ascii_lowercase();
    override_list.split(';').any(|entry| {
        let pattern = entry.trim().to_ascii_lowercase();
        if pattern == "<local>" || pattern.is_empty() {
            return false;
        }
        if let Some(suffix) = pattern.strip_prefix("*.") {
            return host.ends_with(&format!(".{suffix}"));
        }
        pattern == host
    })
}

#[cfg(windows)]
fn windows_identity_proxy_bypassed() -> bool {
    use winreg::{enums::HKEY_CURRENT_USER, RegKey};

    let Ok(settings) = RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
    else {
        return false;
    };
    let override_list: String = settings.get_value("ProxyOverride").unwrap_or_default();
    proxy_override_bypasses_host(&override_list, "iaaa.pku.edu.cn")
}

#[cfg(windows)]
fn windows_user_no_proxy() -> Option<String> {
    use winreg::{enums::HKEY_CURRENT_USER, RegKey};

    let environment = RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey("Environment")
        .ok()?;
    environment.get_value("NO_PROXY").ok()
}

#[cfg(windows)]
fn configure_panel_proxy_environment(
    cmd: &mut Command,
    system_proxy: Option<&str>,
    user_no_proxy: Option<&str>,
    bypass_campus: bool,
) {
    // A long-running desktop process can retain Clash for Windows' old port
    // after Clash Verge changes the user and WinINET proxy to a new port.
    let proxy = system_proxy.and_then(|proxy| {
        let candidate = if proxy.contains("://") {
            proxy.to_owned()
        } else {
            format!("http://{proxy}")
        };
        let url = Url::parse(&candidate).ok()?;
        if matches!(url.scheme(), "http" | "https") && url.host_str().is_some() {
            Some(candidate)
        } else {
            None
        }
    });
    if let Some(proxy) = proxy {
        cmd.env("HTTP_PROXY", &proxy)
            .env("HTTPS_PROXY", &proxy)
            .env_remove("ALL_PROXY");
    } else {
        cmd.env_remove("HTTP_PROXY")
            .env_remove("HTTPS_PROXY")
            .env_remove("ALL_PROXY");
    }

    let mut no_proxy = user_no_proxy.unwrap_or_default().trim().to_owned();
    if bypass_campus {
        for host in ["pku.edu.cn", ".pku.edu.cn"] {
            if !no_proxy.split(',').any(|part| part.trim().eq_ignore_ascii_case(host)) {
                if !no_proxy.is_empty() {
                    no_proxy.push(',');
                }
                no_proxy.push_str(host);
            }
        }
    }
    if !no_proxy.is_empty() {
        cmd.env("NO_PROXY", no_proxy);
    }
}

#[cfg(all(test, windows))]
mod proxy_tests {
    use super::{configure_panel_proxy_environment, proxy_override_bypasses_host};
    use std::process::Command;

    fn command_env(cmd: &Command, key: &str) -> Option<String> {
        cmd.get_envs()
            .find(|(name, _)| name.to_string_lossy().eq_ignore_ascii_case(key))
            .and_then(|(_, value)| value.map(|item| item.to_string_lossy().into_owned()))
    }

    #[test]
    fn identity_host_obeys_windows_proxy_override() {
        assert!(proxy_override_bypasses_host("pku.edu.cn;*.pku.edu.cn", "iaaa.pku.edu.cn"));
        assert!(proxy_override_bypasses_host(" IAAA.PKU.EDU.CN ;<local>", "iaaa.pku.edu.cn"));
        assert!(!proxy_override_bypasses_host("<local>;*.example.com", "iaaa.pku.edu.cn"));
    }

    #[test]
    fn panel_uses_current_proxy_instead_of_inherited_clash_port() {
        let mut cmd = Command::new("cmd");
        cmd.env("HTTP_PROXY", "http://127.0.0.1:7890")
            .env("HTTPS_PROXY", "http://127.0.0.1:7890")
            .env("ALL_PROXY", "http://127.0.0.1:7890");
        configure_panel_proxy_environment(
            &mut cmd,
            Some("127.0.0.1:7897"),
            Some("localhost,127.0.0.1"),
            true,
        );
        assert_eq!(
            command_env(&cmd, "HTTP_PROXY").as_deref(),
            Some("http://127.0.0.1:7897")
        );
        assert_eq!(
            command_env(&cmd, "HTTPS_PROXY").as_deref(),
            Some("http://127.0.0.1:7897")
        );
        assert_eq!(command_env(&cmd, "ALL_PROXY"), None);
        assert_eq!(
            command_env(&cmd, "NO_PROXY").as_deref(),
            Some("localhost,127.0.0.1,pku.edu.cn,.pku.edu.cn")
        );
    }

    #[test]
    fn panel_clears_inherited_proxy_when_system_proxy_is_disabled() {
        let mut cmd = Command::new("cmd");
        cmd.env("HTTPS_PROXY", "http://127.0.0.1:7890");
        configure_panel_proxy_environment(&mut cmd, None, None, false);
        assert_eq!(command_env(&cmd, "HTTPS_PROXY"), None);
    }

    #[test]
    fn panel_clears_inherited_proxy_when_system_value_is_invalid() {
        let mut cmd = Command::new("cmd");
        cmd.env("HTTPS_PROXY", "http://127.0.0.1:7890");
        configure_panel_proxy_environment(&mut cmd, Some("not a proxy"), None, false);
        assert_eq!(command_env(&cmd, "HTTPS_PROXY"), None);
    }
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

fn app_dir() -> Result<PathBuf, String> {
    let home = home_dir().ok_or_else(|| "无法确定用户主目录（HOME / USERPROFILE）。".to_string())?;
    Ok(home.join("PKU-All-in-Notion"))
}

fn panel_exe_name() -> &'static str {
    if cfg!(windows) {
        "pku-sync.exe"
    } else {
        "pku-sync"
    }
}

fn runtime_python_candidates(runtime: &Path) -> [PathBuf; 4] {
    [
        runtime.join("Scripts").join("python.exe"),
        runtime.join("python.exe"),
        runtime.join("bin").join("python3"),
        runtime.join("bin").join("python"),
    ]
}

fn first_existing(paths: impl IntoIterator<Item = PathBuf>) -> Option<PathBuf> {
    paths.into_iter().find(|p| p.is_file())
}

fn bundled_runtime_dir(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let beside = dir.join("resources").join("runtime");
            if first_existing(runtime_python_candidates(&beside)).is_some() {
                return Some(beside);
            }
        }
    }
    if let Ok(resource_root) = app.path().resource_dir() {
        let runtime = resource_root.join("runtime");
        if first_existing(runtime_python_candidates(&runtime)).is_some() {
            return Some(runtime);
        }
        // Map-style resource layout may nest under resources/
        let nested = resource_root.join("resources").join("runtime");
        if first_existing(runtime_python_candidates(&nested)).is_some() {
            return Some(nested);
        }
    }
    None
}

fn bundled_sidecar_exe() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let sidecar = dir.join(panel_exe_name());
    if !sidecar.is_file() {
        return None;
    }
    // Avoid treating the shell itself as the panel binary.
    if let (Ok(a), Ok(b)) = (sidecar.canonicalize(), exe.canonicalize()) {
        if a == b {
            return None;
        }
    }
    // Only prefer the sidecar when a portable runtime was shipped with it.
    let runtime = dir.join("resources").join("runtime");
    if first_existing(runtime_python_candidates(&runtime)).is_some() {
        return Some(sidecar);
    }
    None
}

fn resolve_path_pku_sync() -> Option<PathBuf> {
    let exe_name = panel_exe_name();

    if let Ok(dir) = std::env::var("UV_TOOL_BIN_DIR") {
        let candidate = PathBuf::from(dir).join(exe_name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }

    if let Some(home) = home_dir() {
        let candidate = home.join(".local").join("bin").join(exe_name);
        if candidate.is_file() {
            return Some(candidate);
        }
        let alt = home.join(".local").join("bin").join("pku-sync");
        if alt.is_file() {
            return Some(alt);
        }
    }

    which::which("pku-sync")
        .or_else(|_| which::which("pku-sync.exe"))
        .ok()
}

fn resolve_panel_launch(app: &AppHandle) -> Option<PanelLaunch> {
    if let Some(path) = bundled_sidecar_exe() {
        return Some(PanelLaunch::Exe { path });
    }
    if let Some(runtime) = bundled_runtime_dir(app) {
        if let Some(path) = first_existing(runtime_python_candidates(&runtime)) {
            return Some(PanelLaunch::Python { path });
        }
    }
    resolve_path_pku_sync().map(|path| PanelLaunch::Exe { path })
}

fn append_log(log_path: &Path, line: &str) {
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(file, "{line}");
    }
}

fn now_stamp() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("unix:{secs}")
}

fn kill_panel(child: &mut Child) {
    let pid = child.id();
    #[cfg(windows)]
    {
        let mut terminate = Command::new("taskkill");
        terminate.creation_flags(CREATE_NO_WINDOW);
        let _ = terminate.args(["/F", "/T", "/PID", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    #[cfg(not(windows))]
    {
        // The sidecar waits for bundled Python, which owns the local server.
        // Terminate the process group to avoid leaving Python behind.
        let group = format!("-{pid}");
        let terminated = Command::new("/bin/kill")
            .args(["-TERM", "--", &group])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
        if !terminated {
            let _ = child.kill();
        }
    }
    let _ = child.wait();
}

fn stop_panel(app: &AppHandle) {
    if let Some(state) = app.try_state::<PanelState>() {
        if let Ok(mut guard) = state.child.lock() {
            if let Some(mut child) = guard.take() {
                kill_panel(&mut child);
            }
        }
    }
}

fn poll_healthz() -> Result<u16, String> {
    let deadline = Instant::now() + HEALTHZ_TIMEOUT;
    while Instant::now() < deadline {
        for port in PANEL_PORTS {
            let url = format!("http://127.0.0.1:{port}/healthz");
            match ureq::get(&url)
                .timeout(Duration::from_secs(1))
                .call()
            {
                Ok(resp) if (200..300).contains(&resp.status()) => return Ok(port),
                _ => {}
            }
        }
        std::thread::sleep(HEALTHZ_INTERVAL);
    }
    Err(
        "本机服务启动超时（约 30 秒）。请查看应用目录中的 panel.log，确认 pku-sync 是否已正确安装。"
            .to_string(),
    )
}

fn emit_status(app: &AppHandle, message: &str) {
    let _ = app.emit("panel-status", message);
}

fn emit_error(app: &AppHandle, message: &str) {
    let _ = app.emit("panel-error", message);
}

fn start_panel(app: AppHandle) {
    let app_dir = match app_dir() {
        Ok(dir) => dir,
        Err(err) => {
            emit_error(&app, &err);
            return;
        }
    };

    if let Err(err) = fs::create_dir_all(&app_dir) {
        emit_error(
            &app,
            &format!("无法创建应用目录 {}：{err}", app_dir.display()),
        );
        return;
    }

    let log_path = app_dir.join("panel.log");
    let Some(launch) = resolve_panel_launch(&app) else {
        let msg = "没有找到 pku-sync：应用尚未安装完成。请重新运行安装程序，或在本机开发环境安装 pku-sync 后再试。";
        append_log(
            &log_path,
            &format!("[launcher] {} {msg}", now_stamp()),
        );
        emit_error(&app, msg);
        return;
    };

    let (prog, extra_args): (PathBuf, Vec<&str>) = match &launch {
        PanelLaunch::Exe { path } => (path.clone(), vec![]),
        PanelLaunch::Python { path } => (path.clone(), vec!["-m", "pku_sync"]),
    };

    append_log(
        &log_path,
        &format!(
            "[launcher] {} 启动面板，工作目录 {}，可执行文件 {}",
            now_stamp(),
            app_dir.display(),
            prog.display()
        ),
    );
    emit_status(&app, "正在启动本机服务…");

    let log_file = match OpenOptions::new().create(true).append(true).open(&log_path) {
        Ok(file) => file,
        Err(err) => {
            emit_error(&app, &format!("无法写入 panel.log：{err}"));
            return;
        }
    };
    let log_err = match log_file.try_clone() {
        Ok(file) => file,
        Err(err) => {
            emit_error(&app, &format!("无法写入 panel.log：{err}"));
            return;
        }
    };

    let mut cmd = Command::new(&prog);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    #[cfg(unix)]
    cmd.process_group(0);
    cmd.args(&extra_args)
        .args(["panel", "--no-browser"])
        .current_dir(&app_dir)
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(log_err))
        .env("PYTHONUTF8", "1");
    // The desktop shell owns installation and signed updates. The bundled
    // panel must not offer the separate `uv tool upgrade` path.
    cmd.env("PKU_DESKTOP_APP", "1");

    // Use the current Windows proxy settings for the entire panel, including
    // Notion and cloud services, even if this process inherited a stale port.
    #[cfg(windows)]
    configure_panel_proxy_environment(
        &mut cmd,
        windows_user_https_proxy().as_deref(),
        windows_user_no_proxy().as_deref(),
        windows_identity_proxy_bypassed(),
    );

    // The retrying Python transport needs the active Windows user proxy for
    // IAAA; it still downloads large campus videos through a direct route.
    #[cfg(windows)]
    if std::env::var_os("PKU_CAMPUS_IDENTITY_PROXY").is_none() {
        if windows_identity_proxy_bypassed() {
            cmd.env("PKU_CAMPUS_IDENTITY_PROXY", "direct");
        } else if let Some(proxy) = windows_user_https_proxy() {
            cmd.env("PKU_CAMPUS_IDENTITY_PROXY", proxy);
        }
    }

    // Keep uv resolvable for in-app upgrade paths when UV_TOOL_BIN_DIR is set.
    if let Ok(bin_dir) = std::env::var("UV_TOOL_BIN_DIR") {
        let path_key = if cfg!(windows) { "Path" } else { "PATH" };
        let mut path_val = std::env::var_os(path_key).unwrap_or_default();
        let mut prefix = std::ffi::OsString::from(&bin_dir);
        prefix.push(if cfg!(windows) { ";" } else { ":" });
        prefix.push(&path_val);
        path_val = prefix;
        cmd.env(path_key, path_val);
    }

    let child = match cmd.spawn() {
        Ok(child) => child,
        Err(err) => {
            let msg = format!("无法启动本机服务（{}）：{err}", prog.display());
            append_log(&log_path, &format!("[launcher] {} {msg}", now_stamp()));
            emit_error(&app, &msg);
            return;
        }
    };

    if let Some(state) = app.try_state::<PanelState>() {
        if let Ok(mut guard) = state.child.lock() {
            *guard = Some(child);
        }
    }

    emit_status(&app, "正在等待本机服务就绪…");
    let port = match poll_healthz() {
        Ok(port) => port,
        Err(err) => {
            append_log(&log_path, &format!("[launcher] {} {err}", now_stamp()));
            stop_panel(&app);
            emit_error(&app, &err);
            return;
        }
    };

    let target = format!("http://127.0.0.1:{port}/app");
    append_log(
        &log_path,
        &format!("[launcher] {} 健康检查通过，打开 {target}", now_stamp()),
    );

    let Ok(url) = Url::parse(&target) else {
        emit_error(&app, &format!("内部错误：无法解析地址 {target}"));
        return;
    };

    match app.get_webview_window("main") {
        Some(window) => {
            if let Err(err) = window.navigate(url) {
                emit_error(&app, &format!("打开学生界面失败：{err}"));
            }
        }
        None => emit_error(&app, "内部错误：找不到主窗口。"),
    }
}

#[cfg(not(debug_assertions))]
fn desktop_updater(app: &AppHandle, direct: bool) -> tauri_plugin_updater::Result<tauri_plugin_updater::Updater> {
    use tauri_plugin_updater::UpdaterExt;

    let mut builder = app.updater_builder();
    // On Windows the updater calls process::exit after launching NSIS,
    // so RunEvent::Exit cannot be relied on to stop the Python service.
    // It must release bundled runtime files before NSIS replaces them.
    let cleanup_app = app.clone();
    builder = builder.on_before_exit(move || {
        stop_panel(&cleanup_app);
        cleanup_app.cleanup_before_exit();
    });
    if direct {
        builder = builder.no_proxy();
    } else {
        #[cfg(windows)]
        if let Some(proxy) = windows_user_https_proxy() {
            let normalized = if proxy.contains("://") { proxy } else { format!("http://{proxy}") };
            if let Ok(url) = Url::parse(&normalized) {
                builder = builder.proxy(url);
            }
        }
    }
    builder.build()
}

#[cfg(not(debug_assertions))]
fn check_for_updates(app: AppHandle) {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

    std::thread::spawn(move || {
        // Let the local panel appear before making the update request or showing a prompt.
        std::thread::sleep(Duration::from_secs(8));
        let update = match tauri::async_runtime::block_on(async {
            match desktop_updater(&app, false)?.check().await {
                Ok(update) => Ok(update),
                Err(first_error) => {
                    // A stale system proxy must not hide an otherwise reachable
                    // signed update endpoint. Retry over a direct TLS route.
                    if let Ok(dir) = app_dir() {
                        append_log(&dir.join("panel.log"), &format!("[updater] proxied check failed; trying direct: {first_error}"));
                    }
                    desktop_updater(&app, true)?.check().await
                }
            }
        }) {
            Ok(Some(update)) => update,
            Ok(None) => return,
            Err(err) => {
                if let Ok(dir) = app_dir() {
                    append_log(&dir.join("panel.log"), &format!("[updater] check failed: {err}"));
                }
                return;
            }
        };
        let accepted = app.dialog()
            .message(format!("发现新版本 {}。现在下载并安装吗？安装会中断正在进行的转写或同步，关闭窗口后自动重新打开；请先保存正在编辑的内容。已保存的用户数据会保留。", update.version))
            .title("PKU All in Notion 更新")
            .buttons(MessageDialogButtons::YesNo)
            .blocking_show();
        if !accepted {
            return;
        }
        let progress_window = app.get_webview_window("main");
        if let Some(window) = &progress_window {
            let _ = window.set_title("PKU All in Notion · 正在下载更新…");
        }
        let mut downloaded = 0_u64;
        let mut last_percent = None;
        let outcome = tauri::async_runtime::block_on(update.download_and_install(
            |chunk, total| {
                downloaded = downloaded.saturating_add(chunk as u64);
                if let (Some(total), Some(window)) = (total.filter(|n| *n > 0), &progress_window) {
                    let percent = (downloaded.saturating_mul(100) / total).min(100);
                    if last_percent != Some(percent) {
                        let _ = window.set_title(&format!("PKU All in Notion · 下载更新 {percent}%"));
                        last_percent = Some(percent);
                    }
                }
            },
            || {
                if let Some(window) = &progress_window {
                    let _ = window.set_title("PKU All in Notion · 正在验证并安装更新…");
                }
            },
        ));
        if let Err(err) = outcome {
            if let Some(window) = &progress_window {
                let _ = window.set_title("PKU All in Notion");
            }
            // The before-exit hook has already stopped the local panel if
            // Windows failed to launch NSIS. Restore it so this still-running
            // window does not remain disconnected after the error dialog.
            let panel_stopped = app.try_state::<PanelState>()
                .and_then(|state| state.child.lock().ok().map(|guard| guard.is_none()))
                .unwrap_or(false);
            if panel_stopped {
                let restart_app = app.clone();
                std::thread::spawn(move || start_panel(restart_app));
            }
            if let Ok(dir) = app_dir() {
                append_log(&dir.join("panel.log"), &format!("[updater] install failed: {err}"));
            }
            let _ = app.dialog()
                .message("更新没有完成。请稍后重试，或从官网下载安装包。")
                .title("PKU All in Notion 更新")
                .kind(MessageDialogKind::Error)
                .blocking_show();
            return;
        }
        #[cfg(not(windows))]
        app.restart();
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(PanelState {
            child: Mutex::new(None),
        })
        .setup(|app| {
            let handle = app.handle().clone();
            std::thread::spawn(move || start_panel(handle));
            #[cfg(not(debug_assertions))]
            check_for_updates(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(
                event,
                WindowEvent::CloseRequested { .. } | WindowEvent::Destroyed
            ) {
                stop_panel(window.app_handle());
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
                stop_panel(app_handle);
            }
        });
}
