# Windows v0.1.4 验收 · 2026-09-24

- 构建机：Windows 11 Pro x64，build 26200。
- 构建：`uv build --wheel --offline`；在配置既有 Tauri 更新签名私钥与口令后，`cd desktop && npm run build` 返回 0，同时生成 NSIS 与 `.sig`。
- 主安装包：`desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.4_x64-setup.exe`。分发副本：`dist/PKU-All-in-Notion-Windows-0.1.4.exe`，118954767 字节，SHA256 `e75e02e3861615f7d5a67b079efbb8a7905d13f43ea4f337652bbafc0954e5f8`。
- 测试：Windows 全量回归 951 项通过，含真实 Edge 浏览器用例；每日默认只同步索引与简报、桌面“每日/自动化”按钮跳过录像的测试通过。
- 安装：NSIS `/S` 成功，路径 `%LOCALAPPDATA%\Programs\PKU All in Notion`；`%USERPROFILE%\PKU-All-in-Notion\.env` 保留。静默安装未观察 SmartScreen。
- 实机窗口：release 树及安装目录均通过桌面冒烟；`/app` 返回 200、无背景终端、单实例、关窗结束 sidecar/Python 并释放 8791，`panel.log` 留在用户目录。
- 按需处理：应用内同步索引后，用户选定一条录像及 Notion 讲次并确认才下载、转写、生成笔记和发布。真实教学网只读同步取得 12 门课程、28 条录像；真实 Notion 只读目录有 10 门课程、21 个可选讲次。云端 `notes` 测试成功，消耗 0.001 AI 点；真实 Notion 写入未在用户未指定目标的情况下执行，写入与去重由测试覆盖。
- 发布：server-a/b/c 安装包 SHA256 一致，清单均为 0.1.4。公网旧版更新请求返回 0.1.4 及签名，新版返回 204；Windows 下载端点支持 Range 206。
- 本机旧定时任务 `pku-course-sync` 仍指向不存在的 `.venv\Scripts\python.exe`，上次运行失败，当前账号修改任务被 Windows 拒绝。它不会执行录像整理，也不会成功执行原有 06:00 同步/简报；恢复调度需有任务修改权限后重设解释器路径。
