# Windows 桌面版 0.1.11 验收

日期：2026-09-26。构建机：Windows 11 专业版 x64，10.0.26200。

## 修复范围

- 800–900 px 窗口提供“账户与额度”菜单，保留额度、兑换码、退出登录和 Notion 连接操作；950 px 显示完整侧栏。
- 教学网附件在课程页可下载；下载时以本机教学网账号重新取得附件地址，不向页面泄露临时链接。无附件的条目说明下一步。
- “整理这节录像”禁用时显示原因；课程页增加手动关联、忽略和恢复未匹配资料。
- 资料、作业或测验、课程信息分别计数；课程卡显示课程号、教学班和目录更新时间；增加课程、日期与内容类型查找。
- 录像显示已知时长、本地文件大小及预计转写分钟数；未知时明确说明无法估算。
- 首页集中显示首次使用步骤；目录更新显示已完成课程数，并可在当前课程读取结束后取消。
- 演示模式明确标记模拟 Notion 连接；教学网账号表单提供标签和取消入口；更新提示改为中文。

## 构建与测试

- 命令：`uv build --wheel --offline`；`cd desktop && npm run build`（使用原有 Tauri 更新签名密钥）；`verify-windows-release.ps1 -ExpectedVersion 0.1.11`；`smoke-release.ps1`。
- Python/Edge 全套回归：982 项通过，1 条第三方弃用警告。
- NSIS 安装包：[PKU All in Notion_0.1.11_x64-setup.exe](../desktop/src-tauri/target/release/bundle/nsis/PKU%20All%20in%20Notion_0.1.11_x64-setup.exe)，119,111,628 字节；SHA-256 `3316787aca34584c69a09759b269679b5c905ec8ef216654ad3aab9172ac0164`；配套 `.sig` 已生成。
- 分发副本：[PKU-All-in-Notion-Windows-0.1.11.exe](../dist/PKU-All-in-Notion-Windows-0.1.11.exe)。未构建可选 Inno 包。

## 已安装路径验收

- NSIS 静默覆盖安装到 `%LOCALAPPDATA%\Programs\PKU All in Notion`，内含 Tauri 程序、sidecar 和 Python 0.1.11 运行时；安装目录未发现 `.env`。
- 用户 `%USERPROFILE%\PKU-All-in-Notion\.env` 安装前后 SHA-256 相同。静默安装未观察 SmartScreen；双击时是否提示未在本轮重复观察。
- 已安装程序出现桌面窗口，`/app` 返回 200；sidecar/Python 无可见终端窗口；再次启动退出并聚焦现有实例。关窗后进程树退出、8791–8793 端口释放，`panel.log` 存在。
- 已安装的 `app.js`、`app.css` 与 Edge 回归所测源码 SHA-256 一致。
- 使用本机已保存的教学网账号，从已安装版课程页接口只读下载一份真实课件：HTTP 200，3,230,747 字节，文件头为 `%PDF-`；临时副本已删除，关窗后进程与端口释放。

## 发布验收

- server-a/b/c 的安装包 SHA-256 均与本机一致；三台 `latest.json` SHA-256 均为 `a76efc536a7198436d85c95d332d466fa39422d8dd283e9a88d85297e660fa26`，原 0.1.10 清单各自备份为 `latest.json.pre-0.1.11`。
- 公网 0.1.10 更新请求返回 200、目标版本 0.1.11，签名与本地文件一致；0.1.11 更新请求返回 204。下载端点 Range 请求返回 206，文件总大小 119,111,628 字节；健康检查返回 200。

本轮未重复进行真实 Notion 授权、完整教学网目录同步或云端扣费；这些结果不能由演示模式和模拟接口测试代替。

