# Windows 桌面版 0.1.16 验收记录（2026-09-26）

- 构建机：Windows 11 专业版 x64，版本 10.0.26200。执行 `uv lock --check --offline`、`uv build --wheel --offline`、`desktop` 中的 `npm run build`；`verify-windows-release.ps1 -ExpectedVersion 0.1.16` 通过。
- NSIS：`desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.16_x64-setup.exe`，119,145,064 字节。分发副本 `dist/PKU-All-in-Notion-Windows-0.1.16.exe` 的 SHA-256 为 `7012fcae36f7244c726588a68e40bbed8d192867a3dfc95a07533c5d4f453fdf`，配套 updater 签名已生成。发行树含桌面 exe、`pku-sync.exe`、可重定位 Python 运行时；安装后的 Python 包版本为 0.1.16。
- 测试：Windows 全量 `pytest -q` 为 **1003 passed**，另有一项第三方库弃用警告。对两份真实 `notes.md` 运行转换检查，分别生成 9 张和 8 张 Notion 原生表格；含公式的笔记另生成 4 个独立公式块，均无原样 Markdown 泄漏。真实 Notion API 的临时公式和嵌套列表块写入、读回后已清理。
- 真实页面修复：9 月 9 日和 9 月 17 日两篇现有笔记在修复前已与本机生成的原文逐块核对，未发现手工改动。修复后通过 Notion API 回读：分别为 945、940 个顶层块，各只保留 1 个课程大标题、9 和 8 张原生表格、14 和 8 张原有课堂图片；未检出原样标题、表格、斜体或公式标记。页面链接与图片均保留。未重做转写或 AI 整理。
- 本机覆盖安装退出码 0，路径 `%LOCALAPPDATA%\Programs\PKU All in Notion`；`%USERPROFILE%\PKU-All-in-Notion\.env` 安装前后 SHA-256 一致。静默安装未显示 SmartScreen，本轮未判断交互式安装时是否出现提示。
- 发行树及实际安装目录分别通过 `smoke-release.ps1`：窗口打开学生页面 `/app`，辅助进程无可见终端，第二次启动保持单实例，关窗后 8791–8793 端口释放；`panel.log` 存在。
- server-a/b/c 的安装包 SHA-256 均为 `7012fcae36f7244c726588a68e40bbed8d192867a3dfc95a07533c5d4f453fdf`，`latest.json` SHA-256 均为 `384552b09b17782ea699577f0ec3f13ad7350c132f76a3c5f2a2105ec55a834b`；各服务器保留 `latest.json.pre-0.1.16`。
- 公网首页与健康检查返回 200；0.1.15 更新请求返回 200、目标 0.1.16 且签名匹配；0.1.16 请求返回 204。安装包和 updater 下载入口的 0–1023 字节范围请求均返回 206。

**证据边界：** 本轮验证了已有两篇真实笔记的原样 Markdown 修复、真实 Notion 块类型读写、完整代码回归和 Windows 安装更新路径。历史页面中原本扁平的多层列表未整体重建，后续新笔记会按嵌套列表发布。没有重新进行新的录像下载、转写、AI 整理或作业提交，也未对所有课程笔记做人工视觉检查。

## 2026-09-27 自动更新签名勘误

上文首次发布的 `latest.json` SHA-256 `384552…` 对应一个错误更新清单：`signature` 被额外 Base64 编码一次。其他电脑下载更新后会报签名无效。NSIS 安装包及其 Tauri `.sig` 本身未损坏，安装包 SHA-256 仍为 `7012fcae…`。

已将清单改为直接使用 `.sig` 文件文本（去掉末尾换行），同步到 server-a/b/c；新 `latest.json` SHA-256 为 `bf83d696fe4e896ddd3f582d7e07f3bcd29c2e164d07b043928f81ae48678e3d`。三台服务器各保留 `latest.json.pre-signature-fix-0.1.16`。本机用客户端同款 minisign 校验逻辑验证：安装包与内置公钥、签名和签名中的版本均匹配；旧清单报 `InvalidEncoding`，新增发布校验器也会拒绝它。公网 0.1.15 更新响应现返回与 `.sig` 完全一致的 452 字符签名。
