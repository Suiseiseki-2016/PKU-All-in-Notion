# Windows desktop acceptance — 0.1.23

Date: 2026-10-04 (Asia/Singapore)

## Outcome

- Before the final campus-independent publish adjustment, the Windows release host completed
  the Python regression suite with 1,642 passed and 2 expected failures. The final focused
  recovery suite then passed 11/11. Its broad rerun completed 1,631 passed and 2 expected
  failures; two unrelated OAuth listener tests could not acquire port 8765 from the current
  Windows networking state. Those listener tests had passed in the earlier full run.
  `tests/test_panel_ports.py` remained excluded because the user's live panel stayed on port
  8791 throughout validation; the release did not interrupt that session.
- The Tauri shell completed its four Windows Rust tests. The bundled release tree passed
  `verify-windows-release.ps1 -ExpectedVersion 0.1.23`: desktop executable, sidecar, portable
  Python runtime, package metadata, and NSIS installer all report 0.1.23.
- An isolated fake-panel smoke on port 8792 loaded `/app`, verified the bundled
  `reuse_existing` and `failure_id` UI paths, showed no helper console windows, released its
  port on exit, and preserved the original port-8791 process.
- A live, idempotent acceptance run against existing user artifacts published the selected
  lecture to Notion with `reuse_existing=true`. No video file existed before or after the run,
  so this path did not redownload video or consume transcription/AI quota.

## Release artifact

- NSIS: `desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.23_x64-setup.exe`
- Size: 97,552,816 bytes
- SHA-256: `1cc7a91e5368647469fa6d7bf13029556d48ffd7e11363f2766b59116b631401`
- Matching Tauri updater signature: generated (452 bytes).

## User-facing recovery changes

- Existing transcript, notes, and keyframes can be published without downloading the lecture
  video again.
- Unexpected failures now identify the failed stage and include a short failure ID while keeping
  exception details out of the student UI.
- Safe Notion reads and campus login recover once over a direct route after an inherited proxy
  exhausts its transport retries. Non-idempotent Notion writes are never replayed this way.
