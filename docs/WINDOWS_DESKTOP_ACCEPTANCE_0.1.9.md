# Windows 桌面版 0.1.9 验收记录

- 构建机：Windows 11 专业版，x64，10.0.26200。
- 构建命令：`uv build --wheel --offline`，`cd desktop && npm run build`；`desktop/scripts/verify-windows-release.ps1 -ExpectedVersion 0.1.9` 通过，包内 Python 导入版本为 0.1.9。
- NSIS 产物：`desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.9_x64-setup.exe`，119,093,033 字节；SHA-256 `a7bb9e12eefeab1607711b636ca942aecb9d58731a37e120ecda9c299ec70123`。同目录有对应 updater `.sig`。
- 安装：静默覆盖 0.1.8 成功，程序位于 `%LOCALAPPDATA%\Programs\PKU All in Notion`；其中有 `pku-desktop.exe`、`pku-sync.exe`、`resources\runtime`，没有 `.env`。静默安装未观察 SmartScreen 弹窗。
- 启动：安装后的 exe 打开标题为 `PKU All in Notion` 的桌面窗口；本机 `/app` 返回 200。第二次启动未生成第二个常驻实例。关窗后桌面进程和 sidecar 均退出，端口 8791–8793 无监听，用户目录下 `panel.log` 存在。
- 数据：升级前后本机 12 门课程仍在；`%USERPROFILE%\PKU-All-in-Notion\.env` 的 SHA-256 未变化。用户数据目录与安装目录分离。
- 回归：客户端 975 项、服务端 186 项测试通过；前端 JavaScript 语法检查和 `git diff --check` 通过。覆盖分段转写与断点复用、按需 OSS 授权、录像单独下载与保留、笔记分窗口续跑、页面及账号路径。
- 真实服务：用本机生成的约 8 秒中文测试音频，经生产转写接口得到 2 段文字，扣除 8 秒额度；随后经短时授权直传阿里云 OSS，复用同音频的缓存，得到 2 段文字且未重复扣费。另一次基于已有 3 段文字的真实笔记请求成功，扣除 0.005 AI 点。
- 发布：相同 SHA-256 的 Windows 安装包已上传 server-a/b/c；三台更新清单一致。公网 0.1.8 → 0.1.9 更新检查返回 200，签名匹配；0.1.9 返回 204；分段下载返回 206；官网和健康检查返回 200。生产 relay 在 server-a 运行，server-b/c 保留待切换容器。
- 范围：本轮没有重新跑完整真实教学网课程的录像下载、长音频转写、笔记发布到 Notion 的端到端流程，也没有重新执行旧版客户端的自动安装升级；长录像实际耗时尚未量测。Inno 包为可选项，本轮交付 NSIS。
