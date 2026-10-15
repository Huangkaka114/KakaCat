; KakaCat 安装程序（Inno Setup 6/7）
;
; 由 packaging/build.py --installer 调用；版本号从命令行传进来（/DMyAppVersion=…），
; 保证和 exe 里的版本资源、源码里的 __version__ 是同一个来源。
;
; 几个刻意的选择：
;
; * PrivilegesRequired=lowest + DefaultDirName={autopf}：装到
;   %LOCALAPPDATA%\Programs\KakaCat，**不弹 UAC**。这不只是图省事——程序在
;   便携模式下会把运行数据写在自己目录下的 data\ 里，装到用户目录里那一套
;   才能正常工作；装进 Program Files 会因为只读而退回 %LOCALAPPDATA%。
; * 不写注册表里除卸载信息之外的任何东西，开始菜单也只放一个快捷方式。
; * 卸载默认不删 data\（里面有用户的日志/配置），由用户在向导里勾选。

#define MyAppName "KakaCat"
#define MyAppExeName "KakaCat.exe"
#define MyAppPublisher "Huangkaka114"
#define MyAppUrl "https://github.com/Huangkaka114/KakaCat"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{9D87C7B3-17C2-41EF-9123-EFEF87C734D5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}
; 安装包自身的文件属性。不写这几行的话，安装程序在资源管理器「详细信息」里的
; 版本/公司/说明全是空的——Inno 不会拿 AppVersion 自动填。
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} —— HLS 视频下载器 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
DefaultDirName={autopf}\KakaCat
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=KakaCat-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
MinVersion=10.0
; 程序自带图标，不要在向导里放宣传图
WizardImageStretch=no

[Languages]
; 简体中文。Inno Setup 官方安装包**不自带**中文（只有日韩泰等），所以这份
; languages\ChineseSimplified.isl 是从 Inno Setup 官方仓库取的：
;   https://raw.githubusercontent.com/jrsoftware/issrc/refs/heads/main/Files/Languages/ChineseSimplified.isl
; 放进仓库是为了让打包不依赖网络。它要求 Inno Setup 6.5.0+（本项目用 6.7.3）。
; 需要英文界面的话换成下面这行：
;   Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 整个打包目录原样装过去（含 bin\ 下的 ffmpeg / mpv，如果有的话）
Source: "..\dist\KakaCat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载**明确清干净**：data\ 里只是程序自己的运行数据（临时文件、诊断报告），
; 卸载时一并删掉，不给用户留一个说不清来历的空目录。
;
; 这里写死而不是只留个 dirifempty：实测只写 dirifempty 时，data\ 有没有被删
; 在两次运行里表现不一致，靠不住。要保留用户数据的话，把第一行去掉即可
; （那时 app 目录会连同 data\ 一起留下）。
Type: filesandordirs; Name: "{app}\data"
Type: dirifempty; Name: "{app}"
