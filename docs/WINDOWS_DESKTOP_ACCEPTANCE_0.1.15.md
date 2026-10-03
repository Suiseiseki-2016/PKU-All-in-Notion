# Windows 桌面版 0.1.15 验收记录（2026-09-26）

- 构建机：Windows 11 专业版 x64，版本 10.0.26200。执行 `uv lock --check --offline`、`uv build --wheel --offline`、`desktop` 中的 `npm run build`；`verify-windows-release.ps1 -ExpectedVersion 0.1.15` 通过。
- NSIS：`desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.15_x64-setup.exe`，119,141,342 字节，SHA-256 `2a27bbff35abe5fd500204ad2f782f416576322d45c1ef2b0fa88df7a47da938`；匹配的 Tauri updater 签名已生成。发行树包含桌面 exe、`pku-sync.exe`、可重定位 Python 运行时，内含 `pku-course-sync 0.1.15`。
- 本机覆盖安装退出码 0，路径 `%LOCALAPPDATA%\Programs\PKU All in Notion`；用户配置 `%USERPROFILE%\PKU-All-in-Notion\.env` 安装前后 SHA-256 相同。静默安装未显示 SmartScreen，本轮未判断交互式安装时是否出现该提示。
- 发行树和本机安装版分别通过 `smoke-release.ps1`：桌面窗口打开学生页面 `/app`，辅助进程无可见终端窗口，第二次启动保持单实例，关窗后 8791–8793 端口释放；`panel.log` 存在。
- 自动化测试：Windows 上全量 `pytest -q` 为 998 passed，另有一项第三方库弃用警告。Notion OAuth 失效本机代理回归测试、文件上传与图片插入位置测试均通过。
- Notion 真实验收：重新授权成功，原因是运行环境残留的 `127.0.0.1:7890` 代理阻断授权码交换，系统代理本身已关闭；修复后 relay 健康检查为 200。9 月 9 日原有笔记补入 14 张课堂画面，9 月 17 日原有笔记补入 8 张。通过 Notion API 回读：22 张均为 Notion 托管图片，各自紧跟时间段标题，说明文字唯一；重复执行未给第一篇增加图片。补图未重做转写或发起 AI 整理。
- 分发副本：`dist/PKU-All-in-Notion-Windows-0.1.15.exe` 及签名；server-a/b/c 上安装包 SHA-256 均为 `2a27bbff35abe5fd500204ad2f782f416576322d45c1ef2b0fa88df7a47da938`，`latest.json` SHA-256 均为 `c1e36960f296ee843c38a182f932353424c5dd1fc63e37208871057177262d55`。各服务器保留了 `latest.json.pre-0.1.15`。
- 公网首页与健康检查返回 200；0.1.14 更新请求返回 200、目标 0.1.15 且含签名；0.1.15 请求返回 204。安装包及 updater 下载入口的 0–1023 字节范围请求均返回 206。

**证据边界：** 未做新的真实录像下载、转写或作业提交；本轮验证的是现有两篇真实笔记补图、代码回归和新版桌面安装路径。图片筛选优先选择清晰课件画面；若录像只有远景或黑屏，仍可能没有适合插入的图片。
