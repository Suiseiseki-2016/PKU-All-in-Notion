; PKU All in Notion - Windows installer script (Inno Setup 6, wave 1)
;
; Per library/packaging-design.md (user-approved 2026-09-18): the installer is
; an Inno Setup .exe WRAPPING the uv bootstrap - it does not bundle Python or
; the application runtime. bootstrap.ps1 installs the official standalone uv,
; provisions uv-managed Python 3.11 (python-build-standalone, no system Python
; required), and installs the app as a uv tool from the bundled release wheel.
;
; Build (release engineer, Windows host with Inno Setup 6 installed):
;   1. uv build                                   (repo root; wheel -> dist\)
;   2. ISCC.exe installer\windows\pku-all-in-notion.iss
;      (override the version: ISCC.exe /DMyAppVersion=x.y.z ...)
;   3. dist\PKU-All-in-Notion-Setup-x.y.z.exe is the unsigned release
;      artifact. The SmartScreen prompt it triggers is captured honestly in
;      the pilot evidence (see validation\m5-packaging-pilot\).
;
; Design invariants:
;   - Per-user install (PrivilegesRequired=lowest): no admin prompt on
;     student machines, everything lands in the user profile.
;   - The install dir holds ONLY the bootstrap assets; .env, DATA_DIR and
;     panel logs always live in %USERPROFILE%\PKU-All-in-Notion (created by
;     bootstrap.ps1 / launch-panel.ps1), so an update or uninstall can
;     never clobber credentials or data.
;   - The bootstrap runs as a REQUIRED install step; a nonzero exit aborts
;     the installation honestly (no silent broken installs).
;   - Uninstall removes the uv tool (best effort) and the install dir, but
;     NEVER deletes the per-user app dir; it says so explicitly.

#define MyAppName "PKU All in Notion"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "PKU All in Notion"
; TUNA PyPI mirror (China-network fast path). The value is passed to
; bootstrap.ps1 for this install and persisted as the user-level
; UV_DEFAULT_INDEX so later `uv tool upgrade` runs use the same mirror.
#define PyPIMirror "https://pypi.tuna.tsinghua.edu.cn/simple"

[Setup]
AppId={{1F2A4B6C-9D3E-4C5B-8A7F-2E9D0C1B4A6E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
; Per-user install: {localappdata}\Programs, no UAC prompt, no admin.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
; Broadcast the HKCU Environment change written by [Registry] (mirror task).
ChangesEnvironment=yes
OutputDir=..\..\dist
OutputBaseFilename=PKU-All-in-Notion-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Support evidence without a terminal: the installer log lands in %TEMP%.
SetupLogging=yes

[Languages]
; Ships with Inno Setup 6. If your Inno install lacks this file, fetch the
; official translation from https://jrsoftware.org or fall back to
; Default.isl (remove this line) - the installer still works.
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务:"
Name: "tunamirror"; Description: "使用清华 TUNA PyPI 镜像下载依赖（推荐国内网络；将写入用户环境变量 UV_DEFAULT_INDEX，卸载时移除）(&M)"; GroupDescription: "网络:"

[Files]
; The release wheel built by `uv build` at the repo root (dist\).
Source: "..\..\dist\pku_course_sync-{#MyAppVersion}-py3-none-any.whl"; DestDir: "{app}"; Flags: ignoreversion
Source: "bootstrap.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "launch-panel.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; Persistent user-level mirror for uv (also used by the in-app autoupdate's
; `uv tool upgrade`); removed at uninstall.
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "UV_DEFAULT_INDEX"; ValueData: "{#PyPIMirror}"; Tasks: tunamirror; Flags: uninsdeletevalue

[Icons]
; The shortcuts start the panel via the launcher, which pins CWD to the
; per-user app dir, auto-opens the browser at the selected port and appends
; every startup line (selected port included) to panel.log in the app dir.
; Wave 1 uses the PowerShell icon; a branded icon is post-pilot polish.
Name: "{autoprograms}\{#MyAppName}\{#MyAppName} 面板"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\launch-panel.ps1"""; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName} 面板"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\launch-panel.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  AppDir, WheelName, BootstrapParams, MirrorSuffix: string;
begin
  if CurStep = ssPostInstall then
  begin
    AppDir := ExpandConstant('{app}');
    WheelName := 'pku_course_sync-{#MyAppVersion}-py3-none-any.whl';
    // bootstrap.ps1 accepts the bundle dir and resolves the wheel itself;
    // the explicit name keeps the log honest about what is being installed.
    BootstrapParams := '-NoProfile -ExecutionPolicy Bypass -File "' + AppDir + '\bootstrap.ps1"' +
      ' -Wheel "' + AppDir + '\' + WheelName + '"';
    if IsTaskSelected('tunamirror') then
      MirrorSuffix := ' -Mirror "{#PyPIMirror}"'
    else
      MirrorSuffix := '';
    WizardForm.StatusLabel.Caption :=
      '正在安装运行环境（uv、Python 3.11、应用本体；下载可能需要几分钟）…';
    // SW_SHOW: the console window displays uv's live download progress.
    // A nonzero exit code ABORTS the install - a broken install must never
    // be reported as success (honesty invariant).
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        BootstrapParams + MirrorSuffix, AppDir, SW_SHOW, ewWaitUntilTerminated,
        ResultCode) then
    begin
      raise Exception.Create(
        '运行环境安装程序无法启动（powershell.exe）。' + #13#10 +
        '请重试安装；若仍失败，请把日志发给支持：' + GetEnv('USERPROFILE') +
        '\PKU-All-in-Notion\install.log');
    end;
    if ResultCode <> 0 then
    begin
      raise Exception.Create(
        '运行环境安装失败（退出码 ' + IntToStr(ResultCode) + '）。' + #13#10 +
        '请重试安装；若仍失败，请把日志发给支持：' + GetEnv('USERPROFILE') +
        '\PKU-All-in-Notion\install.log');
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  UvPath, AppDataDir: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Best effort: remove the uv tool. uv itself and the provisioned Python
    // stay (they are user-level, shared tooling).
    UvPath := GetEnv('USERPROFILE') + '\.local\bin\uv.exe';
    if FileExists(UvPath) then
    begin
      Exec(UvPath, 'tool uninstall pku-course-sync', GetEnv('USERPROFILE'),
        SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    AppDataDir := GetEnv('USERPROFILE') + '\PKU-All-in-Notion';
    MsgBox('应用已卸载。' + #13#10#13#10 +
      '用户数据（凭据、课程数据、日志）已按设计保留，未删除：' + AppDataDir + #13#10 +
      '如需彻底清理，请手动删除该目录。', mbInformation, MB_OK);
  end;
end;
