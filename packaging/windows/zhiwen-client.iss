; ============================================================================
; 知纹客户端 · Windows 安装包脚本（Inno Setup 6）
; ----------------------------------------------------------------------------
; 这个脚本把 PyInstaller 打出来的 dist\zhiwen-client.exe 做成一个真正的安装
; 程序：开始菜单项、可选桌面快捷方式、可选开机自启、可卸载。
;
; 编译（CI 里就是这么调的，版本号从 git tag 传入）：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" ^
;       /DMyAppVersion=0.3.0 /DMyAppVersion4=0.3.0.0 packaging\windows\zhiwen-client.iss
;
; 本地手动编译（不带 /D）：用下面 #ifndef 里的默认版本号，能出包但不带真实版本。
;
; 产物：dist\zhiwen-client-Windows-x64-setup.exe（纯 ASCII 文件名，故意的）
;
; ----------------------------------------------------------------------------
; 编码说明（踩过的坑，别改）
; ----------------------------------------------------------------------------
; 本文件是 **UTF-8 无 BOM**。Inno Setup 6 的编译器直接按 UTF-8 读取 .iss，
; 中文不必转义、不需要 #pragma 指令、也不需要 BOM。
; （只有 Inno Setup 5 才要求"Unicode .iss + BOM"，6.x 已无此限制；
;   反过来给 6.x 加 BOM 也不报错，只是没必要。）
; 界面中文的实现方式：Inno 官方发行包里**没有** ChineseSimplified.isl
; （那是社区翻译，要单独下载），所以这里只用自带的英文语言文件做底座，
; 再把所有可见文案用 [CustomMessages] + [Messages] 覆盖成中文 ——
; 不依赖任何额外 .isl 文件，CI 上零配置。
; ============================================================================

; ── 版本号：CI 用 /D 传入，本地编译退回默认值 ────────────────────────────
#ifndef MyAppVersion
  #define MyAppVersion "0.3.0"
#endif
; Windows 文件属性要的是四段纯数字（x.y.z.w），CI 会额外传一份
#ifndef MyAppVersion4
  #define MyAppVersion4 "0.0.0.0"
#endif

; ── 显示名 vs 路径名（必须分开，别合并）──────────────────────────
; MyAppName   ：只用于**显示**（添加/删除程序里的名字、快捷方式注释）。
;               可以是中文，用户看着舒服。
; MyAppDirName：用于**所有磁盘路径与文件/文件夹名**。必须是纯 ASCII。
;
; 为什么必须分开：中文路径会在很多地方踩坑 ——
;   · 某些老版本命令行工具不解 UTF-8 路径
;   · 备份/同步脚本按 GBK 处理路径会乱码
;   · 用户从别的机器粘贴路径时容易出错
;   · 上报日志、生成配置里带中文路径很难 grep
; 所以：**只要会落到磁盘上的名字，一律 ASCII**。
#define MyAppName "知纹客户端"
#define MyAppDirName "ZhiwenClient"
#define MyAppPublisher "pscly"
#define MyAppExeName "zhiwen-client.exe"
#define MyAppURL "https://github.com/pscly/zhiwen-brwo"

; 固定 AppId：Windows 靠它认出"还是同一个应用"，升级时走覆盖安装、卸载时
; 能找到旧记录。**永远不要改这一行**（改了就成了另一个应用，用户会有两份）。
; 双花括号是 Inno 的转义写法，落到注册表里是一个 {GUID}。
#define MyAppId "{{8A4F2C31-7D5E-4B62-9E08-1B5A6F3D2C74}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; {autopf} = Program Files（管理员/全局安装）或 %LOCALAPPDATA%\Programs（免管理员）。
; 配合下面的 PrivilegesRequired=lowest，用户是普通账号也能装成功。
DefaultDirName={autopf}\{#MyAppDirName}
DefaultGroupName={#MyAppDirName}
DisableProgramGroupPage=yes

; ⚠ 升级时**不要**沿用上次的安装目录。
; Inno 默认 UsePreviousAppDir=yes：它从注册表读上次装在哪，然后装回那里。
; 于是从旧版本（中文目录 知纹客户端）升级上来时，目录名仍然是中文 ——
; 用户看到的"装完还是中文路径"就是这么来的，光改 DefaultDirName 没用。
; 这里强制每次都用 DefaultDirName，保证升级后落在纯英文路径。
UsePreviousAppDir=no
UsePreviousGroup=no
; 同理：不要复用上次的语言/任务选择，避免旧状态把新行为带过去
UsePreviousTasks=yes

; lowest = 默认按当前用户装、不弹 UAC；想全局装就运行 setup /ALLUSERS。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline

; 只允许 x64 Windows：exe 是 PyInstaller 在 x64 上打的，装到 32 位系统上是坏的。
; （若以后要支持 ARM64 原生安装，把这两行改成 x64compatible，需要 Inno Setup ≥ 6.3）
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

; 安装包自身落在仓库根的 dist\ 下 —— CI 从那里取走改名再上传。
; 相对路径是相对本脚本所在目录（packaging\windows\）算的。
OutputDir=..\..\dist
OutputBaseFilename=zhiwen-client-Windows-x64-setup

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
DisableWelcomePage=yes

; 覆盖安装时如果客户端还开着，让安装程序提示关闭
CloseApplications=yes
RestartApplications=no

; 装坏了能查：日志写 %TEMP%\Setup Log*.txt
SetupLogging=yes

; 卸载项在「设置 → 应用」里显示成什么样
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}

; 文件属性里看到的版本信息
VersionInfoVersion={#MyAppVersion4}
VersionInfoProductVersion={#MyAppVersion4}
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoCopyright=MIT License, pscly

; 最低系统版本：Windows 10（客户端依赖 Python 3.12 的运行库）
MinVersion=10.0

; ── 图标（可选，现在没有 ico 就跳过）────────────────────────────────────
; 想加图标：把一个 256x256 的 zhiwen-client.ico 放进 packaging\windows\，
; 然后取消下面这一行的注释即可（安装程序与卸载项会一起用上）。
; 客户端 exe 自己的图标由 packaging\zhiwen-client.spec 的 icon= 决定。
; SetupIconFile=zhiwen-client.ico

[Languages]
; 只用 Inno 自带的英文语言文件：不依赖 ChineseSimplified.isl（官方不随包发）。
; 中文界面靠下面的 [CustomMessages] / [Messages] 覆盖。
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 桌面快捷方式：默认勾上（checkedonce = 用户第一次取消勾选后，下次重装记住这个选择）
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce
; 开机自启：默认**不勾**。勾了写 HKCU\...\Run；不勾则安装后清掉上次写的值（见 [Code] 段）。
Name: "autostart"; Description: "{cm:AutoStartTask}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 主程序。源路径相对本脚本目录：packaging\windows\ + ..\..\dist\zhiwen-client.exe
; ignoreversion：升级时无条件覆盖旧 exe，不弹"是否保留较新的文件"对话框。
Source: "..\..\dist\zhiwen-client.exe"; DestDir: "{app}"; DestName: "zhiwen-client.exe"; Flags: ignoreversion
; MIT 许可证要求随二进制分发许可证原文
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
; 开始菜单项（{group} = 开始菜单里的「知纹客户端」文件夹）
Name: "{group}\{#MyAppDirName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "{cm:AppComment}"
Name: "{group}\Uninstall {#MyAppDirName}"; Filename: "{uninstallexe}"
; 桌面快捷方式（只在勾了 desktopicon 任务时才建；{autodesktop} 随安装模式走）
Name: "{autodesktop}\{#MyAppDirName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "{cm:AppComment}"; Tasks: desktopicon

[Run]
; 装完直接打开（postinstall = 在完成页显示成勾选框，默认勾上）
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram}"; Flags: nowait postinstall skipifsilent

[Registry]
; 开机自启：勾了 autostart 才写，卸载时 uninsdeletevalue 把这个值删掉。
; 用户随时可以在 Windows「设置 → 应用 → 启动」或任务管理器「启动」里关掉它，
; 也可以重跑安装程序把 autostart 取消（[Code] 段会顺手清掉旧值）。
; 只写 HKCU：免管理员安装也能生效，且只影响当前用户。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ZhiwenClient"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[InstallDelete]
; 老版本（≤0.4.0）装在中文目录里。升级到本版本后安装位置变成 ZhiwenClient，
; 旧目录如果留着就是一份孤儿文件（卸载项指向新目录，旧文件再也删不掉）。
; 这里在安装前清掉它 —— 目录名是我们自己创建的，不会误删别人的东西。
Type: filesandordirs; Name: "{autopf}\知纹客户端"
Type: filesandordirs; Name: "{group}\知纹客户端"
Type: files; Name: "{autodesktop}\知纹客户端.lnk"

[UninstallDelete]
; 安装目录整体删（含运行期掉进来的日志、临时文件）
Type: filesandordirs; Name: "{app}"
; 开始菜单文件夹（卸载程序自己会删它建的快捷方式，这里兜底）
Type: dirifempty; Name: "{group}"
; 桌面快捷方式兜底（万一用户自己复制过同名的）
Type: files; Name: "{autodesktop}\{#MyAppDirName}.lnk"

[CustomMessages]
; 下面这些名字里，CreateDesktopIcon / AdditionalIcons / LaunchProgram 是 Inno
; 自带的 {cm:} 消息名，在这里覆盖成中文；其余是本脚本自己的。
AppComment=指纹浏览器机群管理客户端
AdditionalIcons=附加快捷方式：
CreateDesktopIcon=创建桌面快捷方式(&D)
AutoStartTask=开机自动启动（登录后自动连上次的服务器；可随时在 Windows「设置 → 应用 → 启动」里关闭）
LaunchProgram=立即运行 知纹客户端(&L)

[Messages]
; 覆盖 Inno 内置的英文界面文案（[Messages] 是官方给的做法，比改 .isl 稳）。
; 保留原有的 %1 / [name] 占位符，否则安装向导里会丢掉应用名。
SetupAppTitle=知纹客户端 安装程序
SetupWindowTitle=知纹客户端 安装程序 - %1
UninstallAppTitle=知纹客户端 卸载程序
UninstallAppFullTitle=%1 卸载程序
InformationTitle=提示
ConfirmTitle=确认
ErrorTitle=错误
SetupAppRunningError=检测到 %1 正在运行。%n%n请先把它关掉，然后点「确定」继续安装；点「取消」退出安装。
ExitSetupTitle=退出安装
ExitSetupMessage=安装还没完成。现在退出的话，程序不会被安装。%n%n你可以稍后重新运行安装程序继续。%n%n要退出安装吗？
ButtonBack=< 上一步(&B)
ButtonNext=下一步(&N) >
ButtonInstall=安装(&I)
ButtonOK=确定
ButtonCancel=取消
ButtonYes=是(&Y)
ButtonNo=否(&N)
ButtonFinish=完成(&F)
ButtonBrowse=浏览(&B)...
ClickNext=点「下一步」继续，或点「取消」退出安装。
WizardSelectDir=选择安装位置
SelectDirDesc={#MyAppName} 要装到哪里？
SelectDirLabel3=安装程序将把 {#MyAppName} 装到下面的文件夹。
SelectDirBrowseLabel=点「下一步」继续；想换文件夹就点「浏览」。
DiskSpaceGBLabel=至少需要 [gb] GB 可用磁盘空间。
DiskSpaceMBLabel=至少需要 [mb] MB 可用磁盘空间。
CannotInstallToNetworkDrive=不能安装到网络驱动器。
WizardSelectTasks=选择附加任务
SelectTasksDesc=要执行哪些附加任务？
SelectTasksLabel2=选择安装 {#MyAppName} 时要一起做的附加任务，然后点「下一步」。
WizardReady=准备安装
ReadyLabel1=安装程序已准备好在你的电脑上安装 {#MyAppName}。
ReadyLabel2a=点「安装」开始安装；想再检查或修改设置就点「上一步」。
ReadyLabel2b=点「安装」开始安装。
ReadyMemoDir=安装位置：
ReadyMemoGroup=开始菜单文件夹：
ReadyMemoTasks=附加任务：
WizardInstalling=正在安装
InstallingLabel=正在安装 {#MyAppName}，请稍候。
FinishedHeadingLabel={#MyAppName} 安装完成
FinishedLabel={#MyAppName} 已装到你的电脑上。从开始菜单或桌面快捷方式就能打开。
FinishedLabelNoIcons={#MyAppName} 已装到你的电脑上。
ClickFinish=点「完成」退出安装程序。
StatusExtractFiles=正在解压文件……
StatusCreateIcons=正在创建快捷方式……
StatusCreateRegistryEntries=正在写入注册表……
StatusRunProgram=正在收尾……
StatusRollback=正在回滚改动……
ConfirmUninstall=确定要卸载 %1 及其全部组件吗？
UninstallStatusLabel=正在从你的电脑上卸载 %1，请稍候。
UninstalledAll=%1 已卸载完成。
UninstalledMost=%1 卸载完成。%n%n有部分内容没能删掉，可以手动清理。
StatusUninstalling=正在卸载 %1……
WindowsVersionNotSupported=这个安装程序只支持 Windows 10 / 11。
OnlyOnTheseArchitectures=这个安装程序只能装在以下处理器架构的 Windows 上：%n%n%1

[Code]
// 这一小段只做一件事：[Registry] 只能表达"勾了才写"，没法表达"没勾就删"。
// 重装时如果用户把「开机自动启动」取消勾选了，得把上次留下的 Run 键清掉，
// 否则"取消勾选"对已经装过的机器等于没生效。
//
// 用到的函数签名（已按 Inno Setup 官方文档核对，别凭印象改）：
//   function RegDeleteValue(const RootKey: HKEY; const SubKeyName, ValueName: String): Boolean;
//   function WizardIsTaskSelected(const Tasks: String): Boolean;
//   procedure CurStepChanged(CurStep: TSetupStep);   // ssPostInstall = 文件装完之后
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('autostart') then
      Log('开机自启：已写入 Run 键')
    else if RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ZhiwenClient') then
      Log('开机自启：已清掉上次写入的 Run 键')
    else
      Log('开机自启：注册表里本来就没有这个值，无需清理');
  end;
end;
