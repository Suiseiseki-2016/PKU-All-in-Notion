# Windows 桌面版 0.1.14 验收记录（2026-09-26）

- 构建机：Windows 11 专业版 x64，版本 10.0.26200。命令：uv lock --check --offline；uv build --wheel --offline；在 desktop 执行 npm run build；verify-windows-release.ps1 -ExpectedVersion 0.1.14。
- NSIS：desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.14_x64-setup.exe，119,139,768 字节，SHA-256 4e7cef34a1a865f25dc044d78c99db9301fa84445bb70882d03f7fa5cb8d63d7；匹配的 updater 签名已生成。发行树有 pku-desktop.exe、pku-sync.exe、resources/runtime/python.exe，包内 pku-course-sync 为 0.1.14。
- 本机覆盖安装退出码 0。安装路径 %LOCALAPPDATA%\Programs\PKU All in Notion；用户目录 %USERPROFILE%\PKU-All-in-Notion 仍在，.env 升级前后 SHA-256 相同。静默安装不显示 SmartScreen，本轮未判断 SmartScreen 弹窗。
- 对发行树和本机安装版各运行 smoke-release.ps1：桌面窗口进入 /app，后台辅助进程没有可见终端，第二次启动保留原窗口，关窗后端口 8791–8793 释放，panel.log 存在。
- 自动测试：Python 全量 993 项通过；真实 Edge 测试覆盖 800×560 首屏登录、课程浏览与作业草稿预检。node --check 通过。
- 使用本机保存的教学网凭据只读刷新 12 门课程：53 项资料中 53 张资料卡有直达附件路径（共 56 个附件路径），9 条通知，3 条正式作业；从老师公告中另提取 2 条不重复的交付截止事项。附件链接仍要求学生具有教学网登录会话。
- 已同步到用户此前选定的 Notion 学习主页。随后逐页反查 Notion API：资料直达附件 53/53，通知单条公告锚点 9/9，公告待办原文链接 2/2。主页新增“近期待办与新通知”及首页入口。34 个课堂页标题显示 2 个“已整理”、32 个“待整理”；“课堂记录”索引把已整理笔记排前。
- 分发副本：dist/PKU-All-in-Notion-Windows-0.1.14.exe。server-a/b/c 安装包 SHA-256 均为 4e7cef34a1a865f25dc044d78c99db9301fa84445bb70882d03f7fa5cb8d63d7；三台 latest.json SHA-256 均为 19b5d2be874c164e256dc2f6f1bed3a67e3e23c135d13d398669f35b26475f49，旧清单各自保留为 latest.json.pre-0.1.14。
- 公网首页、健康检查返回 200。0.1.13 的更新请求返回 200、目标 0.1.14 且带签名；0.1.14 请求返回 204。公开安装包与 updater 下载入口的 0–1023 字节范围请求均返回 206。

**证据边界：** 本轮未使用真实产品账号登录后的作业提交路径，也未执行真实作业提交。教学网附件仅用已保存账号做只读访问验证；Notion 链接仍受教学网会话权限控制。原有 0.1.13 老卡片采取保留原内容、把行动摘要和新链接插入页面前部的升级方式，因此页面下方仍可看到先前的同步记录。

