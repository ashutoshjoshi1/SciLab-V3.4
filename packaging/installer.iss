; SciLab Windows installer (Inno Setup 6)
;
; Every value that identifies the product is passed in by packaging/build.py
; from scilab_version.py, so this file never carries a version number of its
; own and the installer can't disagree with the executable it wraps.
;
;   python packaging/build.py
;
; Compiling this file directly also works if the /D defines below are supplied.

#ifndef MyAppName
  #error Run packaging/build.py -- this script expects its /D defines.
#endif

[Setup]
; AppId must stay constant across releases: it is how Windows recognises an
; installed copy, so upgrades replace it instead of piling up entries in
; "Apps & features".
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyProductName} {#MyAppDisplayVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductName={#MyProductName}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyProductName} Setup
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}
AppUpdatesURL={#MyAppUrl}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
UninstallDisplayName={#MyProductName} {#MyAppDisplayVersion}
UninstallDisplayIcon={app}\{#MyExeName}

; Licence page: the wizard shows LICENSE and the operator must accept it
; before installing. Silent installs skip the page. Relative to this script
; (packaging/), so a direct ISCC compile finds it too.
LicenseFile=..\LICENSE

; The bundled vendor DLLs (avaspecx64.dll and friends) are 64-bit, so a 32-bit
; install would produce an app that cannot open any instrument.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Per-machine by default (an instrument PC is shared between operators), but a
; user without admin rights can still install into their own profile.
; Overridable so the packaging tests can compile a variant that is incapable
; of elevating (MyPrivileges=lowest); releases keep the admin default.
#ifndef MyPrivileges
  #define MyPrivileges "admin"
#endif
PrivilegesRequired={#MyPrivileges}
; "commandline" matters as much as "dialog": without it /CURRENTUSER is
; silently ignored and an unattended install lands machine-wide even when the
; caller asked for a per-user one.
PrivilegesRequiredOverridesAllowed=commandline dialog

OutputDir={#MyOutputDir}
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=scilab.ico
WizardStyle=modern
WizardImageFile=wizard_banner.bmp
WizardSmallImageFile=wizard_small.bmp
WizardImageStretch=no
Compression=lzma2/max
SolidCompression=yes
; Refuse to install over a running copy rather than leaving a half-updated
; folder; AppMutex lets Setup detect it even when started from Explorer.
AppMutex=SciLab.SingleInstance.Mutex
CloseApplications=yes
RestartApplications=no
SetupMutex={#MyAppName}Setup
MinVersion=10.0
ShowLanguageDialog=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller folder. recursesubdirs+createallsubdirs keeps the
; layout the app expects (_internal, "spectrometers dll files", ...).
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The licence and third-party notices go beside SciLab.exe, where an operator
; or an auditor will look for them.
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyExeName}"; Comment: "{#MyProductName}"
Name: "{group}\{#MyAppName} self-test"; Filename: "{app}\{#MyExeName}"; Parameters: "--self-test"; Comment: "Check this installation and write self-test.log"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyExeName}"; Comment: "{#MyProductName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller and Python leave __pycache__ and extracted temporaries behind;
; without this the app folder survives uninstall as a stub. User data in
; %LOCALAPPDATA%\SciLab is deliberately left alone -- it holds settings,
; measurement results and analysis output.
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
{ After install, verify the bundle really works on *this* machine rather than
  only on the build machine: a missing Visual C++ runtime or a blocked DLL
  shows up here instead of as a mystery failure on first use. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if Exec(ExpandConstant('{app}\{#MyExeName}'), '--self-test', '', SW_HIDE,
            ewWaitUntilTerminated, ResultCode) then
    begin
      if ResultCode <> 0 then
        MsgBox('{#MyAppName} was installed, but its self-test reported a problem.' + #13#10#13#10 +
               'See self-test.log in %LOCALAPPDATA%\SciLab and send it to support.',
               mbError, MB_OK);
    end;
  end;
end;
