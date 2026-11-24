; The roomfix installer (Inno Setup 6). Built by packaging/build.py, which passes
; Version, Source (the PyInstaller folder) and Icon.
;
; Installs for the current user, without administrator rights, into
; %LOCALAPPDATA%\Programs\roomfix, with a Start menu entry. Settings, measurements and
; saved corrections stay in %APPDATA%\roomfix, also when roomfix is uninstalled.

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "..\dist\roomfix"
#endif
#ifndef Icon
  #define Icon "..\build\roomfix.ico"
#endif

[Setup]
AppId={{6F1C2B9E-6F0A-4C1E-9B7D-3C0F5D7A2E11}
AppName=roomfix
AppVersion={#Version}
AppVerName=roomfix {#Version}
AppPublisher=roomfix
AppPublisherURL=https://github.com/Akeksi/roomfix
AppSupportURL=https://github.com/Akeksi/roomfix/issues
AppUpdatesURL=https://github.com/Akeksi/roomfix/releases
DefaultDirName={autopf}\roomfix
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputBaseFilename=roomfix-setup-{#Version}
SetupIconFile={#Icon}
UninstallDisplayIcon={app}\roomfix.exe
UninstallDisplayName=roomfix
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: desktopicon; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; the libraries of the version before: not mixed with the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\roomfix"; Filename: "{app}\roomfix.exe"; Comment: "Speaker and room correction"
Name: "{autodesktop}\roomfix"; Filename: "{app}\roomfix.exe"; Comment: "Speaker and room correction"; Tasks: desktopicon

[Run]
; the correction ran before the update: it runs again, from the new version
Filename: "{app}\roomfix.exe"; Parameters: "--engine"; Flags: nowait runhidden; Check: WasRunning
Filename: "{app}\roomfix.exe"; Description: "Open roomfix"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\roomfix.exe"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitRoomfix"

[Code]
var
  Running: Boolean;

function WasRunning: Boolean;
begin
  Result := Running;
end;

{ An update: close the roomfix that runs from here (its correction and its page) before its
  files are replaced, and remember whether the correction ran. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Running := False;
  if FileExists(ExpandConstant('{app}\roomfix.exe')) then
    if Exec(ExpandConstant('{app}\roomfix.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Running := (Code = 0);
  Result := '';
end;

{ Uninstalled: the correction no longer starts with Windows, if it started from here. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Startup: String;
  Text: AnsiString;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Startup := ExpandConstant('{userstartup}\roomfix.vbs');
    if LoadStringFromFile(Startup, Text) then
      if Pos(Lowercase(ExpandConstant('{app}')), Lowercase(String(Text))) > 0 then
        DeleteFile(Startup);
  end;
end;
