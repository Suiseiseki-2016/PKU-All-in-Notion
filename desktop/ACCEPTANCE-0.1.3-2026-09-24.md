# Windows 按需录像整理验收 · v0.1.3 · 2026-09-24

- 构建机：Windows 11 Pro x64，build 26200。
- 构建：`uv build --wheel --offline`；`cd desktop && npm run build`。NSIS 压缩完成后，使用本机 Tauri updater 密钥对现有安装包签名（`tauri signer sign --app-version 0.1.3`）。
- 安装包：`desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.3_x64-setup.exe`；分发副本：`dist/PKU-All-in-Notion-Windows-0.1.3.exe`；SHA256 `5e78203f5b39b60e396aae6593fae124c56f3e0433114c3aa19891fae52ea5b4`。同目录有 `.sig`。
- 安装：NSIS `/S` 升级至 `%LOCALAPPDATA%\Programs\PKU All in Notion`；用户 `%USERPROFILE%\PKU-All-in-Notion\.env` 保留。静默安装，因此未观察 SmartScreen。
- 窗口：未打包树和已安装目录的冒烟测试均通过。出现 Tauri 桌面窗口与 `/app`；后台 sidecar/Python 没有可见终端；第二次启动只聚焦原窗口；关窗后进程树退出、8791 端口释放；用户目录有 `panel.log`。
- 按需流程：真实教学网只读同步取得 12 门课程、28 条录像索引；真实 Notion 只读目录有 10 门课程、21 个可选讲次。页面只在用户选择录像和目标讲次并确认后处理，不会批量转写。云端 `notes` 实际调用成功，返回课堂笔记，消耗 0.001 AI 点。Notion 写入与重试去重由本地模拟测试覆盖；未替用户选择真实讲次，也未向真实 Notion 发布测试笔记。
- 发布：安装包与 SHA256 已在 server-a/b/c 三节点核对；`https://pku.aeoluswu.info/download/windows` 返回新版安装包（Range 请求 206），旧版自动更新请求返回 v0.1.3，新版请求返回 204。
- 测试：按需流程、转写、平台登录和 Notion 相关定向测试 83 项通过；Windows 全量回归（含 Edge 浏览器测试）946 项通过。

