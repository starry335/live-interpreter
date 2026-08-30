#define AppName "Live Interpreter"
#define AppVersion "0.2.1"
#define Publisher "starry335"
#define AppUrl "https://github.com/starry335/live-interpreter"

[Setup]
AppId={{7A156F43-8EF4-4BE4-B7BC-38917E4D3FB5}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={localappdata}\Programs\LiveInterpreter
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=LiveInterpreter-Setup-v{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\launcher\LiveInterpreter.exe
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\LiveInterpreter\*"; DestDir: "{app}\launcher"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\LiveInterpreterBackend\*"; DestDir: "{app}\backend"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\edge_extension\*"; DestDir: "{app}\edge_extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\corpus.phrases"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\launcher\LiveInterpreter.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\launcher\LiveInterpreter.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\launcher\LiveInterpreter.exe"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
