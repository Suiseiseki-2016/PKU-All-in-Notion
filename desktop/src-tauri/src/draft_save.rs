//! Native Save As for the exact reviewed local draft. No arbitrary URLs or paths
//! arrive from JavaScript; the destination comes from the Windows file dialog.
use std::io::{Read, Write};
use std::path::Path;
use std::time::Duration;

use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager, WebviewWindow};
use tauri_plugin_dialog::DialogExt;

use crate::{healthz_matches, PanelState};

const MAX_DRAFT_BYTES: u64 = 41 * 1024 * 1024;

#[derive(Serialize)]
pub struct SavedDraft {
    state: &'static str,
    filename: Option<String>,
    sha256: Option<String>,
}

fn valid_hex(value: &str, length: usize) -> bool {
    value.len() == length && value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn source_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128
        && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

fn reviewed_name(value: &str) -> bool {
    !value.is_empty() && value.len() <= 255 && value != "." && value != ".."
        && !value.chars().any(|ch| ch.is_control() || "<>:\"/\\|?*".contains(ch))
        && !value.ends_with(['.', ' '])
}

fn check_url(origin: &str, draft: &str, digest: &str, course: &str, content: &str) -> Result<String, String> {
    if !valid_hex(draft, 32) || !valid_hex(digest, 64) || !source_id(course) || !source_id(content) {
        return Err("草稿身份未通过核验，请重新准备并预览。".into());
    }
    Ok(format!("{origin}/api/campus/submission-drafts/{draft}/check?sha256={digest}&course_id={course}&content_id={content}"))
}

fn current_endpoint(app: &AppHandle, window: &WebviewWindow) -> Result<(String, String), String> {
    let state = app.state::<PanelState>();
    let mut child = state.child.lock().map_err(|_| "本机服务暂时不可用。")?;
    if !child.as_mut().is_some_and(|child| matches!(child.try_wait(), Ok(None))) {
        return Err("本机服务已停止，草稿没有保存，请重新打开应用。".into());
    }
    let endpoint = state.endpoint.lock().map_err(|_| "本机服务身份无法核验。")?
        .clone().ok_or("本机服务尚未就绪。")?;
    let url = window.url().map_err(|_| "无法核验当前窗口。")?;
    if window.label() != "main" || url.as_str() != format!("{}/app", endpoint.0) {
        return Err("请从课程作业预览页面保存这份草稿。".into());
    }
    Ok(endpoint)
}

fn verify_endpoint(endpoint: &(String, String)) -> Result<(), String> {
    let response = ureq::get(&format!("{}/healthz", endpoint.0)).timeout(Duration::from_secs(5))
        .call()
        .map_err(|_| "本机服务连接中断，未保存草稿。")?;
    let body = response.into_string().map_err(|_| "本机服务身份无法读取，未保存草稿。")?;
    if !healthz_matches(&body, &endpoint.1) {
        return Err("本机服务身份已变化，未保存草稿。".into());
    }
    Ok(())
}

fn verified_preflight(url: &str, draft: &str, digest: &str) -> Result<(), String> {
    let response = ureq::get(url).timeout(Duration::from_secs(20)).call()
        .map_err(|_| "草稿预览已经变化或暂时不可核验，请重新准备。")?;
    let body = response.into_string().map_err(|_| "草稿核验响应不可读取，未保存文件。")?;
    let value: serde_json::Value = serde_json::from_str(&body)
        .map_err(|_| "草稿核验响应无效，未保存文件。")?;
    if value.get("valid").and_then(|v| v.as_bool()) != Some(true)
        || value.get("draft_id").and_then(|v| v.as_str()) != Some(draft)
        || value.get("sha256").and_then(|v| v.as_str()) != Some(digest) {
        return Err("草稿核验与当前预览不一致，未保存文件。".into());
    }
    Ok(())
}

fn persist_verified(path: &Path, payload: &[u8], digest: &str) -> Result<(), String> {
    if payload.len() as u64 > MAX_DRAFT_BYTES || format!("{:x}", Sha256::digest(payload)) != digest {
        return Err("下载内容与已核验预览不同，原文件保留，请重新准备。".into());
    }
    let parent = path.parent().ok_or("保存位置无效，未保存文件。")?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)
        .map_err(|_| "无法写入所选位置，请检查磁盘空间和目录权限后重试。")?;
    temporary.write_all(payload).and_then(|_| temporary.as_file().sync_all())
        .map_err(|_| "文件未能完整写入，原文件保留，请检查磁盘空间后重试。")?;
    temporary.persist(path).map_err(|_| "无法完成保存，原文件保留，请检查文件权限后重试。")?;
    Ok(())
}

#[tauri::command]
pub async fn save_reviewed_draft(app: AppHandle, window: WebviewWindow, draft_id: String,
    sha256: String, course_id: String, content_id: String, archive_name: String) -> Result<SavedDraft, String> {
    if !reviewed_name(&archive_name) {
        return Err("草稿文件名无效，请重新准备。".into());
    }
    let endpoint = current_endpoint(&app, &window)?;
    let preflight = check_url(&endpoint.0, &draft_id, &sha256, &course_id, &content_id)?;
    tauri::async_runtime::spawn_blocking(move || {
        verify_endpoint(&endpoint)?;
        verified_preflight(&preflight, &draft_id, &sha256)?;
        let selected = app.dialog().file().set_parent(&window).set_file_name(&archive_name).blocking_save_file();
        let Some(selected) = selected else {
            return Ok(SavedDraft { state: "cancelled", filename: None, sha256: None });
        };
        let path = selected.into_path().map_err(|_| "请选择本机文件保存位置。")?;
        if current_endpoint(&app, &window)? != endpoint {
            return Err("保存期间本机服务已变化，未保存文件，请重新打开预览。".into());
        }
        // The user may leave Save As open while a draft/source changes. Check
        // again, then read bounded immutable bytes and verify their full hash.
        verify_endpoint(&endpoint)?;
        verified_preflight(&preflight, &draft_id, &sha256)?;
        let response = ureq::get(&format!("{}/api/campus/submission-drafts/{draft_id}/file?sha256={sha256}", endpoint.0))
            .timeout(Duration::from_secs(20)).call().map_err(|_| "草稿内容暂时无法下载，请重新核验预览。")?;
        let mut payload = Vec::new();
        response.into_reader().take(MAX_DRAFT_BYTES + 1).read_to_end(&mut payload)
            .map_err(|_| "下载中断，原文件保留，请重试。")?;
        persist_verified(&path, &payload, &sha256)?;
        Ok(SavedDraft { state: "saved", filename: path.file_name().map(|name| name.to_string_lossy().into()), sha256: Some(sha256) })
    }).await.map_err(|_| "保存任务中断，请重新打开预览。".to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_ipc_origin_is_allowed_without_granting_foreign_hosts_or_ports() {
        // Windows' custom-protocol IPC parses the HTTP Origin header, which
        // has no /app path. The command separately verifies window.url()
        // against the owned panel's exact /app URL before opening a dialog.
        let capability: serde_json::Value = serde_json::from_str(
            include_str!("../capabilities/default.json")
        ).unwrap();
        let patterns: Vec<tauri::utils::acl::RemoteUrlPattern> = capability["remote"]["urls"]
            .as_array().unwrap().iter().map(|value| value.as_str().unwrap().parse().unwrap()).collect();
        for port in [8791, 8792, 8793] {
            let origin = format!("http://127.0.0.1:{port}/").parse().unwrap();
            assert!(patterns.iter().any(|pattern| pattern.test(&origin)), "Windows IPC Origin must reach the reviewed save command");
        }
        for url in ["http://127.0.0.1:8794/", "http://localhost:8791/", "https://127.0.0.1:8791/", "http://example.org:8791/"] {
            assert!(!patterns.iter().any(|pattern| pattern.test(&url.parse().unwrap())));
        }
    }
    #[test]
    fn foreign_urls_and_encoded_paths_cannot_enter_a_local_draft_request() {
        for value in ["../file", "_1_1/../../healthz", "https://example.org", "_1_1&sha256=bad", ""] {
            assert!(check_url("http://127.0.0.1:8791", &"a".repeat(32), &"b".repeat(64), value, "_2_1").is_err());
        }
        assert!(check_url("http://127.0.0.1:8791", &"a".repeat(32), &"b".repeat(64), "_1_1", "_2_1").is_ok());
    }
    #[test]
    fn corrupt_download_cannot_overwrite_an_existing_student_file() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("answer.zip");
        std::fs::write(&path, b"student original").unwrap();
        assert!(persist_verified(&path, b"changed bytes", &"a".repeat(64)).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"student original");
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 1);
    }
    #[test]
    fn exact_content_is_saved_atomically() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("answer.zip");
        let bytes = b"reviewed exact bytes";
        persist_verified(&path, bytes, &format!("{:x}", Sha256::digest(bytes))).unwrap();
        assert_eq!(std::fs::read(path).unwrap(), bytes);
    }
    #[test]
    fn filenames_cannot_supply_paths_or_windows_streams() {
        for name in ["../a.zip", "a.zip:stream", "C:\\a.zip", "a/answer.zip", "a.zip.", ""] { assert!(!reviewed_name(name)); }
        assert!(reviewed_name("作业答案.zip"));
    }
}
