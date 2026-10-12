fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["save_reviewed_draft"]),
        ),
    )
    .expect("failed to build the application and its explicit command permissions")
}
