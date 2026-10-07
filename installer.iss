#define MyAppName "SpotDL GUI Pro v4"
#ifndef MyAppVersion
#define MyAppVersion "4.0"
#endif
#ifndef MyOutputDir
#define MyOutputDir "installer"
#endif
#ifndef MyOutputBaseFilename
#define MyOutputBaseFilename "SpotDL_GUI_Pro_v4_Setup"
#endif
#ifndef MySourceDir
#define MySourceDir "dist\SpotDL GUI Pro v4"
#endif
#define MyAppPublisher "SpotDL GUI Pro"
#define MyAppExeName "SpotDL GUI Pro v4.exe"

[Setup]
AppId={{6A8A33A1-F0C2-43D2-9B10-4D6A29A2B24F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#MyOutputDir}
OutputBaseFilename={#MyOutputBaseFilename}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
