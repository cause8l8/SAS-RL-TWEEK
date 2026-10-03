; Inno Setup 6.7+ / 7 recipe. Environment variables are supplied by build.ps1.

#define MyAppName "SAS Game Booster"
#define MyAppExeName "SAS Game Booster.exe"
#define MyAppPublisher "SAS"
#define MyAppVersion GetEnv("SAS_BOOSTER_VERSION")
#define MySourceDir GetEnv("SAS_BOOSTER_SOURCE_DIR")
#define MyOutputDir GetEnv("SAS_BOOSTER_OUTPUT_DIR")
#define MyProjectRoot GetEnv("SAS_BOOSTER_PROJECT_ROOT")

#if MyAppVersion == ""
  #error SAS_BOOSTER_VERSION is required
#endif
#if MySourceDir == ""
  #error SAS_BOOSTER_SOURCE_DIR is required
#endif
#if MyOutputDir == ""
  #error SAS_BOOSTER_OUTPUT_DIR is required
#endif
#if MyProjectRoot == ""
  #error SAS_BOOSTER_PROJECT_ROOT is required
#endif

[Setup]
AppId={{5E4D0B3B-7C64-4D6C-8B62-1AD2C71AE0D3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#MyOutputDir}
OutputBaseFilename=SAS-Game-Booster-{#MyAppVersion}-win-x64-setup
SetupIconFile={#MyProjectRoot}\assets\sas_booster.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern dark includetitlebar hidebevels
WizardBackColor=#09070F
WizardImageBackColor=#09070F
WizardSmallImageBackColor=#09070F
WizardSmallImageFile={#MyProjectRoot}\assets\sas_booster.png
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=yes
AppMutex=Local\SASGameBoosterOptimization
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
#ifdef ReleaseSignTool
SignTool={#ReleaseSignTool}
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Remove only the old dependency tree so upgrades cannot retain stale modules.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
