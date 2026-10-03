#![cfg_attr(windows, windows_subsystem = "windows")]
//! Thin launcher shipped as Tauri `externalBin` (`pku-sync`).
//!
//! Production: exec bundled relocatable Python with `-m pku_sync …`.
//! Dev / incomplete bundle: fall back to a PATH / uv-tool `pku-sync` that is
//! not this executable (avoids recursion).
//!
//! Standalone crate (no Tauri deps) so it builds on WSL/Linux without GTK/DBus.

use std::env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn panel_exe_name() -> &'static str {
    if cfg!(windows) {
        "pku-sync.exe"
    } else {
        "pku-sync"
    }
}

fn home_dir() -> Option<PathBuf> {
    env::var_os("USERPROFILE")
        .or_else(|| env::var_os("HOME"))
        .map(PathBuf::from)
}

fn bundled_python(exe_dir: &Path) -> Option<PathBuf> {
    // Windows stores resources beside the executable; macOS puts them below
    // Contents/Resources while the sidecar lives in Contents/MacOS.
    let roots = [
        exe_dir.join("resources").join("runtime"),
        exe_dir.parent().unwrap_or(exe_dir)
            .join("Resources").join("resources").join("runtime"),
    ];
    roots.into_iter().find_map(|runtime| {
        [
            runtime.join("Scripts").join("python.exe"),
            runtime.join("python.exe"),
            runtime.join("bin").join("python3"),
            runtime.join("bin").join("python"),
        ].into_iter().find(|p| p.is_file())
    })
}

fn path_pku_sync_excluding(self_exe: &Path) -> Option<PathBuf> {
    let exe_name = panel_exe_name();

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(dir) = env::var("UV_TOOL_BIN_DIR") {
        candidates.push(PathBuf::from(dir).join(exe_name));
    }
    if let Some(home) = home_dir() {
        candidates.push(home.join(".local").join("bin").join(exe_name));
        candidates.push(home.join(".local").join("bin").join("pku-sync"));
    }
    if let Some(path) = env::var_os("PATH").or_else(|| env::var_os("Path")) {
        for dir in env::split_paths(&path) {
            candidates.push(dir.join(exe_name));
            if cfg!(windows) {
                candidates.push(dir.join("pku-sync.exe"));
            } else {
                candidates.push(dir.join("pku-sync"));
            }
        }
    }

    candidates.into_iter().find(|p| {
        p.is_file() && same_file(p, self_exe).map(|same| !same).unwrap_or(true)
    })
}

fn same_file(a: &Path, b: &Path) -> std::io::Result<bool> {
    Ok(a.canonicalize()? == b.canonicalize()?)
}

fn run() -> ExitCode {
    let self_exe = match env::current_exe() {
        Ok(p) => p,
        Err(err) => {
            eprintln!("pku-sync sidecar: cannot resolve own path: {err}");
            return ExitCode::from(2);
        }
    };
    let Some(exe_dir) = self_exe.parent() else {
        eprintln!("pku-sync sidecar: executable has no parent directory");
        return ExitCode::from(2);
    };

    let forwarded: Vec<OsString> = env::args_os().skip(1).collect();

    if let Some(python) = bundled_python(exe_dir) {
        let mut cmd = Command::new(&python);
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);
        cmd.arg("-m").arg("pku_sync").args(&forwarded);
        cmd.env("PYTHONUTF8", "1");
        let path_key = if cfg!(windows) { "Path" } else { "PATH" };
        let mut prefix = OsString::from(python.parent().unwrap_or(exe_dir).as_os_str());
        if let Some(existing) = env::var_os(path_key) {
            prefix.push(if cfg!(windows) { ";" } else { ":" });
            prefix.push(existing);
        }
        cmd.env(path_key, prefix);

        return match cmd.status() {
            Ok(status) => status
                .code()
                .map(|c| ExitCode::from(c as u8))
                .unwrap_or(ExitCode::FAILURE),
            Err(err) => {
                eprintln!(
                    "pku-sync sidecar: failed to start bundled Python ({}): {err}",
                    python.display()
                );
                ExitCode::from(3)
            }
        };
    }

    let Some(fallback) = path_pku_sync_excluding(&self_exe) else {
        eprintln!(
            "pku-sync sidecar: no bundled runtime near {} and no pku-sync on \
             PATH. Run desktop/scripts/stage-sidecar on the target build host, \
             or install pku-sync for development.",
            exe_dir.display()
        );
        return ExitCode::from(1);
    };

    let mut cmd = Command::new(&fallback);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    match cmd.args(&forwarded).status() {
        Ok(status) => status
            .code()
            .map(|c| ExitCode::from(c as u8))
            .unwrap_or(ExitCode::FAILURE),
        Err(err) => {
            eprintln!(
                "pku-sync sidecar: failed to start fallback ({}): {err}",
                fallback.display()
            );
            ExitCode::from(3)
        }
    }
}

fn main() -> ExitCode {
    run()
}
