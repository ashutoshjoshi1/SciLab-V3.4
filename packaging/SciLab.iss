; Inno Setup script for SciLab.
; Compiled by packaging/build.py, which passes the version and folders:
;   ISCC.exe /DMyAppVersion=3.4.1.0 /DSourceDir=..\dist\SciLab /DOutputDir=..\dist\installer /DRepoDir=.. SciLab.iss

#define MyAppName "SciLab"
#define MyAppLongName "SciLab Spectrometer Characterization System"
#define MyAppPublisher "SciGlob Instruments and Services LLC"
#define MyAppURL "https://sciglob.com/"
#define MyAppExeName "SciLab.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\SciLab"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif
#ifndef RepoDir
  #define RepoDir ".."
#endif

[Setup]
; AppId must stay constant across versions so upgrades replace the previous install.
AppId={{8F3B6A21-5C4D-4E7A-9B0F-3D6E1A7C24B5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppLongName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppLongName} Setup
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppLongName}
; OceanDirect.dll and the app bundle are 64-bit only.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install by default (no UAC, app can write its settings/results next
; to the exe); the dialog lets an administrator install for all users instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
OutputDir={#OutputDir}
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=scilab.ico
WizardImageFile=wizard_banner.bmp
WizardSmallImageFile=wizard_small.bmp
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoDir}\docs\*.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
