# Windows 远程开发与自动更新签名

本页处理两种容易混淆的情况：从另一台电脑通过 SSH 开发，以及制作能给已安装客户端自动更新的 Windows 发布包。**SSH 登录使用的是目标 Windows 电脑的账号与用户目录；源码不包含更新私钥。**

本机的 `desktop/.env` 已配置旧私钥**文件路径**和 DPAPI 密码文件路径，并被 `.gitignore` 忽略；它没有保存明文私钥或密码。`build-signed-windows.ps1` 会读取这两个路径，然后在构建期间向 Tauri 提供签名变量。Tauri CLI 本身不会自动读取 `.env`。如确需迁移到另一台构建机，脚本也接受 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD=...` 明文配置，但明文文件一旦被复制或泄露，就会暴露发布签名能力；现有构建机不需要这样做。服务 API 令牌与更新签名私钥用途不同，可按各自的配置约定管理。

## 先确认是在开发，还是在发布

- **开发界面**：在 Windows 机器上运行 `cd desktop; npm run tauri dev`。开发态不会运行客户端的自动更新检查，也不需要更新签名。通过 SSH 启动的桌面窗口通常不会出现在 SSH 客户端电脑上；请在 Windows 机器的交互式桌面会话中查看窗口。
- **构建发布包**：`desktop/src-tauri/tauri.conf.json` 已设置 `createUpdaterArtifacts: true`，因此 `npm run build` 需要更新签名私钥及密码。仅复制源码到另一台电脑，会报缺少 Tauri 自动更新签名。不要为旧用户重新生成一对密钥；新公钥无法验证旧安装版的更新。
- **测试已安装客户端更新**：在要测试的 Windows 电脑上，从开始菜单启动已安装的正式版。开发态没有自动更新；正式版启动约 8 秒后检查。当前公网最新版本应以接口返回为准；若已安装版本不低于公网版本，“没有可用更新”是正常结果。

## 在原 Windows 构建机上通过 SSH 发布

请先确认 SSH 登录的是**原先保存密钥的 Windows 用户**。密钥位于该用户的 `AppData\Local\PKU-Admin\tauri-updater.key`；密码由同一用户的 Windows DPAPI 加密，位于 `tauri-updater-password.dpapi`。脚本优先读取 `desktop/.env` 指向的文件；没有本机 `.env` 时，会按当前登录用户的 SID 查找注册目录。不同 Windows 用户、另一台电脑或未正确加载用户配置文件的会话可能无法解密。

在 SSH 会话的 PowerShell 中，从仓库根目录执行：

```powershell
whoami
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\build-signed-windows.ps1 -CheckOnly
```

检查通过后制作发布包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\build-signed-windows.ps1
```

脚本只从当前 Windows 用户目录读取现有私钥与 DPAPI 密码，在构建进程运行期间设置 Tauri 所需环境变量，结束后清除；不会打印密码。产物应同时出现 `desktop\src-tauri\target\release\bundle\nsis\*-setup.exe` 和相应的 `*.exe.sig`。在发布前运行 `desktop/scripts/verify-updater-release.ps1` 核对已准备好的发布清单、安装包及签名；该检查不会替代签名构建。

如果 `-CheckOnly` 失败，先看报错中列出的 `Searched` 路径，再核对 `whoami` 是否为原构建用户。当前机器若已知旧密钥曾存在，不能只凭 SSH 会话的 `%LOCALAPPDATA%` 就断定密钥遗失。可在 SSH 会话中只检查文件存在性（不要输出文件内容）：

```powershell
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$profile = (Get-ItemProperty -LiteralPath "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid" -Name ProfileImagePath).ProfileImagePath
$adminDir = Join-Path $profile 'AppData\Local\PKU-Admin'
[pscustomobject]@{
    Account = whoami
    RegisteredProfile = $profile
    SSHLocalAppData = $env:LOCALAPPDATA
    KeyExists = Test-Path -LiteralPath (Join-Path $adminDir 'tauri-updater.key')
    PasswordStoreExists = Test-Path -LiteralPath (Join-Path $adminDir 'tauri-updater-password.dpapi')
}
```

两个文件都存在但 DPAPI 解密失败时，在该 Windows 用户的交互式桌面登录一次后重试。若实际是另一台 Windows 电脑，先在原构建机完成签名构建。确需迁移构建机时，必须安全迁移**原私钥及密码**并重新保护密码；不要提交私钥、密码或明文环境变量到仓库、`.env`、聊天记录或服务器。

## 判断另一台电脑上的“没有更新”或“签名不正确”

在**运行已安装客户端的那台 Windows 电脑**上打开 PowerShell：

```powershell
# 查询旧版本能否看到更新；把 0.1.15 改为那台电脑的已安装版本。
$update = Invoke-RestMethod 'https://pku.aeoluswu.info/updates/windows/x86_64/0.1.15'
$update.version
$update.url

# 查看客户端更新错误，不要发送包含账号或凭据的完整日志。
Select-String -Path "$env:USERPROFILE\PKU-All-in-Notion\panel.log" -Pattern '\[updater\]' |
    Select-Object -Last 10
```

- 接口返回 `204 No Content`：该版本没有更高的公开版本；先确认所安装版本与公网最新版本。
- 接口返回更新，但客户端提示签名错误：保留报错原文，核对安装包是否是官网发布的版本、机器能否访问更新地址，再检查服务端清单的 `signature` 是否为对应 `.exe.sig` 文件的原文。不要对 `.sig` 内容再编码一次。
- `npm run build` 报缺少签名：这是**构建机缺私钥/密码**，与已安装客户端访问更新接口是两个环节；按上一节在原构建机运行签名脚本。

Tauri 对更新包签名的要求见[官方 Updater 文档](https://v2.tauri.app/plugin/updater/)。
