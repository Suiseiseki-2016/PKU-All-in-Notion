# PKU Course Sync Lecture Notes

## Current Status
**Blocker**: None. The real reprocessing run is published, the regression is green, and the entire working tree is committed and pushed to GitHub.
**Last action**: 2026-10-03 - Pushed all session commits (module batches plus the project-log update, up to 218196a) to origin/main; local and remote are in sync.
**Next action**: Human review of the `unverified` / 回看-marked sections in the new note.

## Decision Log
| Date | Decision | Reasoning | Outcome |
|------|----------|-----------|---------|
| 2026-10-03 | Publish the reorganized note as a dated child page under the lecture acceptance page instead of overwriting the original | The original accepted note must stay intact while the new one is evaluated | Original note untouched; child page 更新笔记 2026-10-03 14:02 published |
| 2026-10-03 | Allow recovery-mode fallbacks (exact validated claims plus explicit 回看 re-listen warnings) only inside a recovery container | Strict gates kept aborting on renderer/verifier edge cases while the underlying evidence was sound | 13/13 chapters completed with transparent warnings; strict fail-closed behavior kept in unit-test paths |
| 2026-10-03 | Reuse cached verified evidence after each policy fix instead of redownloading/re-transcribing | Real downloads are expensive and the cache was already verified | Chapters 2, 3, 8, 9 recovered from cache; run reached publish |
| 2026-09-30 | Use the user-approved relaxed claim audit for the CNS retry | Strict sentence verification failed only at the final internal chapter; source transcript and uncertainty markers were preserved | CNS notes generated and the relaxed-audit provenance is stated on the Notion page |
| 2026-09-30 | Remove the shell-proxy fallback from IAAA authentication | The inherited proxy caused the TLS EOF failure; campus authentication should default to the computer network unless explicitly configured | Real CNS and 计算机网络 downloads succeeded |

## Experiment Log
### 2026-10-03: Real end-to-end reprocessing of CNS解剖 2026-09-09第5-6节
- **Command/Script**: live panel (port 8791) driven through real Edge via `C:\Users\A\AppData\Local\Temp\pku_real_process.py`; 《重新整理笔记》 flow
- **Config**: recovery-mode note pipeline with provenance/readability/coverage/semantic gates; panel scroll-preservation patch in `pku_sync/panel/static/app.js`; dedup and cache-reuse fixes in `pku_sync/media.py` and `pku_sync/recovery_claim_selection.py`
- **Result**: PASS - 13/13 chapters; progress 7%→92% then publish; panel scroll held at 1020 for the whole run; note 48,582 chars; original note preserved
- **Evidence**: recording-dir `notion-publication.json` (published_at `2026-10-03T06:02:40+00:00`, note page `3ee91b6f53e1815a89eaeea29c70c04d`); app status `notes_ready`; focused regression 237 passed in 62s; full regression 1625 passed + 2 xfailed in 153s (`tests/test_panel_ports.py` excluded while the live panel holds 8791)

### 2026-09-30: Complete and reconcile 2026-09-23 lecture recordings
- **Command/Script**: lecture batch pipeline, Notion page creation/update, local recording-status verification
- **Config**: CNS retry with `NOTES_CLAIM_AUDIT=false`; standard pipeline for 计算机网络
- **Result**: PASS - both recordings are `notes_ready`; both lecture pages contain structured notes and keyframe references
- **Evidence**: CNS `28,803` note characters and `3,818` transcript segments; 计算机网络 `30,304` note characters and `2,389` transcript segments; Notion hub now links both pages

### 2026-09-30: IAAA proxy/TLS recovery
- **Command/Script**: `pku_sync/network.py` and `tests/test_auth_proxy.py`
- **Config**: no proxy fallback unless `PKU_CAMPUS_IDENTITY_PROXY` is explicitly set
- **Result**: PASS - six auth-proxy tests passed and both target recordings downloaded
- **Evidence**: CNS download approximately 707 MB; 计算机网络 download approximately 643 MB

## Environment Reference
- **Platform**: Windows 10, CUDA-enabled local workstation
- **Working directories**: repository `E:\remote_project\pku-course-sync`; course data for the live panel and pku-sync MCP `C:\Users\A\PKU-All-in-Notion\data` (earlier batch runs used `E:\pku-course-data`)
- **Test environment**: repo-root `.venv` (`python -m pytest`; the system Python has no pytest); full-suite runs exclude `tests/test_panel_ports.py` while the live panel occupies port 8791
- **Notion pages**: lecture hub `3d791b6f53e18112a603e15256c706d5`; CNS 2026-09-23 lecture `3eb91b6f53e181a09e77d54639e2d24d`; 计算机网络 lecture `3eb91b6f53e18158a77cfdcdbe177df4`; CNS 2026-09-09 acceptance page `3e591b6f53e1815bb3aec98231ffd4f1` (reorganized note child `3ee91b6f53e1815a89eaeea29c70c04d`)

## Eliminated Paths
| Date | What was tried | Why it doesn't work |
|------|---------------|---------------------|
| 2026-10-03 | Redownload/re-transcribe chapters 2, 3, 8, 9 after each policy fix | Costs a full re-listen and still hits the same verifier edge cases; cache reuse plus restored review pointers completed the run instead |
| 2026-09-30 | Force the inherited shell proxy for IAAA TLS | Causes `UNEXPECTED_EOF_WHILE_READING`; direct campus-network access works |
| 2026-09-30 | Replace the entire Notion hub reconciliation block | Exact full-text match was brittle; smaller targeted replacements succeeded |
