# Windows 桌面版 0.1.8 验收记录

- 构建机：Windows 11 专业版，x64，10.0.26200。
- 构建：`uv build --wheel --offline --no-build-isolation`；`cd desktop && npm run build`。NSIS 安装包及 updater 签名均成功生成。
- 产物：`desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.8_x64-setup.exe`（119,104,059 字节）及同名 `.sig`。
- 包内检查：`desktop/scripts/verify-windows-release.ps1 -ExpectedVersion 0.1.8` 通过；内嵌 Python 实际从 release `resources/runtime/Lib/site-packages` 导入 0.1.8。
- 安装：NSIS 静默覆盖安装成功，路径 `%LOCALAPPDATA%\Programs\PKU All in Notion`；仅有 0.1.8 的包元数据。因使用静默安装，SmartScreen 是否出现未观察。
- 启动：`pku-desktop.exe` 显示名为 `PKU All in Notion` 的桌面窗口；`/app` 返回 HTTP 200。第二次启动没有产生第二个常驻进程。关窗后 app、sidecar 均退出，8791–8793 无监听，`panel.log` 存在。
- 数据：升级前后 12 门本机课程和 1 份笔记保留；安装目录没有 `.env`。启动后 Notion 课程同步任务完成，旧笔记恢复 1 条讲次链接及 1 条笔记链接，未重新转写或上传。
- 自动测试：0.1.8 功能回归 961 项通过；端口专项 7 项通过，共 968 项。覆盖账号验证、Notion 连接与跳转、课程、按需录像、下载容错及进度、笔记发布与升级恢复、练习和额度、兑换码、页面交互。
- 范围说明：浏览器界面的自动化测试通过；Windows 窗口的像素级检查因本机 UI 自动化助手启动失败未完成。真实教学网下载吞吐与新一次付费 AI 转写未在本轮重复执行。
- 相邻 pku-server：管理后台与成本/缓存命中专项 5 项通过。账号与兑换码后端测试未能收集，因为该仓库本机 `.venv` 缺少声明的 `argon2-cffi`，本机缓存没有该包且 PyPI 连接被拒绝；客户端兑换码与额度测试已包含在上述 968 项内。

## 2026-09-25 线上发布补记

0.1.8 Windows NSIS 安装包已以 `PKU-All-in-Notion-Windows-0.1.8.exe` 同步到 server-a/b/c 的 `/opt/pku-server/data/releases/`。三台落盘 SHA-256 均为 `f01bc682c56765125d3889abe9bea9017f39e22dc84fa2e31fe8b60e3e2a8bf2`。每台原 `latest.json` 留存为 `latest.json.pre-0.1.8`；新清单以原子重命名启用，三台清单哈希一致。

公网验收：官网显示 0.1.8；0.1.5 的 Windows 更新检查返回 200、目标版本 0.1.8，签名与构建产物逐字一致；0.1.8 检查返回 204；更新下载的 0–1023 字节范围返回 206 / 1024 字节；`/healthz` 为 200，无凭证 `/v1/quota` 为 401。管理后台与数据库均未改动。

先前本机构建会话的更新检查失败由进程级 `HTTPS_PROXY=http://127.0.0.1:7890` 引起，该端口没有监听。Windows 用户级代理为 `http://127.0.0.1:7897`，端口可用；通过它请求更新接口得到 204。桌面应用已按用户级代理重新启动，启动检查后 `panel.log` 没有新增更新连接错误。实际旧版客户端的签名安装升级未在本机降级重演。
