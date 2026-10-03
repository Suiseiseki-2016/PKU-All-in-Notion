; PKU All in Notion - Windows installer (Inno Setup 6)
;
; Wave 2 (desktop product path): one setup.exe installs the Tauri shell +
; bundled relocatable Python runtime (sidecar). Start Menu / Desktop shortcuts
; launch the Tauri exe (desktop window), not PowerShell + browser.
;
; Per-user data stays in %USERPROFILE%\PKU-All-in-Notion (.env / data / logs);
; the install dir never holds credentials.
;
; Build (Windows release host):
;   1. cd desktop && npm install && npm run build
;      (stages sidecar + runtime, produces NSIS under src-tauri\target\release\bundle\)
;   2. powershell -File desktop\scripts\stage-inno-payload.ps1
;   3. ISCC.exe installer\windows\pku-all-in-notion.iss
;      Optional: ISCC.exe /DMyAppVersion=x.y.z ...
;
; Preferred end-user artifact can also be Tauri's own NSIS output:
;   desktop\src-tauri\target\release\bundle\nsis\*-setup.exe
; This Inno script packages the same payload for a Chinese-language wizard and
; explicit uninstall messaging about the retained app data directory.
;
; Legacy uv-tool bootstrap (pilot wave 1) lives in bootstrap.ps1 / launch-panel.ps1
; and is no longer invoked by this script.

#define MyAppName "PKU All in Notion"
#define MyAppVersion "0.1.21"
#define MyAppPublisher "PKU All in Notion"
; Exe name as produced by Tauri (productName). Override with /DMyAppExeName=...
#ifndef MyAppExeName
  #define MyAppExeName "PKU All in Notion.exe"
#endif

[Setup]
AppId={{1F2A4B6C-9D3E-4C5B-8A7F-2E9D0C1B4A6E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
; Per-user install: {localappdata}\Programs, no UAC prompt.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=PKU-All-in-Notion-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes
; Uninstall does not remove %USERPROFILE%\PKU-All-in-Notion
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务:"

[Files]
; Payload staged by desktop/scripts/stage-inno-payload.ps1 from a Tauri release
; build (app exe + pku-sync.exe sidecar + resources\runtime\...).
Source: "payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
; Python may create bytecode after first launch; user data is outside {app}.
Type: filesandordirs; Name: "{app}\resources\runtime"

[Icons]
Name: "{autoprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  if not FileExists(ExpandConstant('{#SourcePath}\payload\{#MyAppExeName}')) then
  begin
    MsgBox(
      '缺少 installer\windows\payload\ 下的 Tauri 应用文件。' + #13#10 + #13#10 +
      '请先在 Windows 上执行：' + #13#10 +
      '  cd desktop && npm run build' + #13#10 +
      '  powershell -File desktop\scripts\stage-inno-payload.ps1' + #13#10 +
      '然后再编译本安装脚本。',
      mbError, MB_OK);
    Result := False;
  end
  else
    Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDataDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    AppDataDir := GetEnv('USERPROFILE') + '\PKU-All-in-Notion';
    MsgBox('应用已卸载。' + #13#10#13#10 +
      '用户数据（凭据、课程数据、日志）已按设计保留，未删除：' + AppDataDir + #13#10 +
      '如需彻底清理，请手动删除该目录。', mbInformation, MB_OK);
  end;
end;
