# Windows 桌面版 0.1.12 验收

日期：2026-09-26。构建机：Windows 11 专业版 x64，10.0.26200。

## 修复

- 800 × 560 窗口的“账户与额度”弹层限制在视口内，内部可滚动；退出登录能滚动到可见位置。
- “开始使用”改成分开的四步进度列表。
- 课程卡片缩短课程号和教学班标识，完整编号保留在课程详情。
- 没有录像的课程改成资料优先视图，可直接下载附件；隐藏音频传输说明和无意义的匹配操作。
- 录像时长缺失时提供“读取时长”：用户主动触发后只访问教学网播放器元数据，不下载媒体或发起转写；若源站仍不提供时长，界面会明确提示。
- 演示模式只在总览引导显示一次说明，课程页不再重复占据首屏。

## 测试与安装

- 构建：`uv lock --check --offline`、`uv build --wheel --offline`、Windows `npm run build`，生成已签名 Tauri NSIS 安装包。
- Python 与 Edge 浏览器完整回归：984 项通过，1 条第三方弃用警告；覆盖 800 × 560 菜单、纯资料课程、课程标识、演示提示，以及时长接口只读元数据与缓存。
- NSIS 安装包：[PKU All in Notion_0.1.12_x64-setup.exe](../desktop/src-tauri/target/release/bundle/nsis/PKU%20All%20in%20Notion_0.1.12_x64-setup.exe)，119,131,316 字节，SHA-256 `85c8b61ac74ae2cb411d70063fbef437b4c500d94e72482ef9fb7af65a470974`，配套签名存在。
- 安装路径：`%LOCALAPPDATA%\Programs\PKU All in Notion`。覆盖安装返回 0；用户目录 `%USERPROFILE%\PKU-All-in-Notion\.env` 前后 SHA-256 相同，安装目录无凭据文件；已安装界面文件与测试源码逐字节一致。
- 已安装版本启动后出现桌面窗口且 `/app` 可访问；无辅助终端；第二次启动聚焦已有窗口；关窗后 sidecar/Python 进程退出，8791–8793 端口释放，`panel.log` 已写入。
- 静默覆盖安装未观察 SmartScreen；双击安装时是否弹出未重复检查。未构建可选 Inno 包。

## 发布

- 分发副本：[PKU-All-in-Notion-Windows-0.1.12.exe](../dist/PKU-All-in-Notion-Windows-0.1.12.exe)，三台服务器 SHA-256 均与本机一致。
- server-a/b/c 的 `latest.json` SHA-256 均为 `9f255614ca9820bc8a54f95cbbdfc0104c157c8f492d15ee7d8427a3ec3b6b8f`，旧清单保留为 `latest.json.pre-0.1.12`。
- 公网 0.1.11 更新请求返回 200、目标 0.1.12、签名一致；0.1.12 更新请求返回 204；下载入口 Range 返回 206，健康检查返回 200。

本轮没有使用真实账号重新完成教学网同步、时长读取、转写、Notion 授权或云端扣费；这些行为仍需真实用户路径验收。
