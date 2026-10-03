# Windows 0.1.17 更新签名恢复记录

2026-09-27，在 `DESKTOP-K717641\A` 用户下核对：旧 Tauri 更新私钥和 DPAPI 密码文件均位于 `C:\Users\A\AppData\Local\PKU-Admin`。之前只依据 SSH 会话的 `%LOCALAPPDATA%` 判断“旧私钥不存在”并不充分。`build-signed-windows.ps1` 已改为优先按当前账户 SID 对应的注册用户目录查找；模拟错误的 `%LOCALAPPDATA%` 时 `-CheckOnly` 仍通过。

后续按构建偏好增加了被 Git 忽略的 `desktop/.env`，仅记录这两个现有文件的路径。脚本优先读取它，没有 `.env` 时仍可按 SID 查找；没有把私钥或密码明文写入仓库。

本机签名构建命令：

```powershell
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\build-signed-windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\verify-updater-release.ps1 -ExpectedVersion 0.1.17
```

第一次构建被构建会话继承的失效本机代理阻断；仅在重试构建的进程中清空代理变量后成功。没有修改 Windows 系统代理或安装包内的网络配置。

本地结果：

- NSIS：`desktop\src-tauri\target\release\bundle\nsis\PKU All in Notion_0.1.17_x64-setup.exe`
- 同目录签名：`PKU All in Notion_0.1.17_x64-setup.exe.sig`
- 待发布复制：`dist\PKU-All-in-Notion-Windows-0.1.17.exe`
- 待发布清单：`dist\latest-0.1.17.json`
- 安装包 SHA-256：`A675315998E6A028D1C823863A74264E04D60D8115B64E942D099F8CBF98E3D3`。待发布复制与签名 NSIS 相同。
- 发布前验证器通过：清单版本、安装包字节、`.sig` 内容、客户端公钥及签入的 `0.1.17` 版本均匹配。

旧的 `dist\PKU-All-in-Notion-Windows-0.1.17.exe` 与新签名包字节不同，已保留为 `dist\PKU-All-in-Notion-Windows-0.1.17.pre-signing.exe`，不能作为更新包发布。

## 公网发布（2026-09-27）

用户明确要求发布 0.1.17 后，先在 server-a/b/c 核对原 `latest.json` SHA-256 均为 `bf83d696fe4e896ddd3f582d7e07f3bcd29c2e164d07b043928f81ae48678e3d`，目标文件不存在，且每台磁盘空间足够。将已签名安装包和清单先传到三台服务器的临时文件并逐台校验，再按 server-b、server-c、server-a 顺序启用。每台都保留 `latest.json.pre-0.1.17`，无容器重启或代码部署。

- 三台正式安装包 SHA-256：`a675315998e6a028d1c823863a74264e04d60d8115b64e942d099f8cbf98e3d3`。
- 三台新 `latest.json` SHA-256：`ffb9706649581379bb47083282aed34d9aa5e5bb36539267f90239b4a08b4ab9`。
- 旧版 `0.1.16` 的公网更新请求返回 `0.1.17`，签名与本地 `.sig` 原文完全相同；`0.1.17` 请求返回 HTTP 204。
- `/download/windows` 的 GET 返回 302，指向版本化的 0.1.17 安装包；新安装包范围请求返回 206；官网 `/` 与 `/healthz` 返回 200，未授权 `/v1/quota` 仍返回 401。
- 从公网完整下载 `119,152,445` 字节并核对 SHA-256，结果与本机签名安装包相同。

公网下载：[Windows 0.1.17 安装包](https://pku.aeoluswu.info/download/release/PKU-All-in-Notion-Windows-0.1.17.exe)。

**仍待验收**：在另一台已安装旧版本的 Windows 电脑上完成客户端弹窗、下载安装与重启的真实自动更新流程。SSH 会话的 `-CheckOnly` 报路径不存在；本次发布使用的是已经在 Windows 实机构建、验签并逐台核对字节的现成签名产物。
