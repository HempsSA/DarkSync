; ============================================================================
; DarkSync 2.0 — Inno Setup Installer Script
; ============================================================================
; Build with: "Compile Inno Setup" (ISCC.exe DarkSync.iss)
;
; Prerequisites (run once):
;   1. Install Inno Setup 6+   https://jrsoftware.org/isdl.php
;   2. Place this .iss file in an "installer/" folder at the repo root.
;   3. Run from the repo root:  iscc installer\DarkSync.iss
;
; What the installer does:
;   - Copies all application files to the chosen folder
;   - Checks for Python 3.8+ (must already be installed on the system)
;   - Installs pip dependencies from requirements.txt via the detected Python
;   - Creates Desktop and Start-Menu shortcuts for both editions
;   - Registers an uninstaller in Windows "Add or Remove Programs"
; ============================================================================

#define MyAppName      "DarkSync"
#define MyAppVersion   "2.6.5"
#define MyAppPublisher "HempsSA"
#define MyAppURL       "https://github.com/HempsSA/DarkSync"
#define MyAppExeName1  "DarkSync 2.0.py"
#define MyAppExeName2  "darksync_desktop.py"

[Setup]
AppId={{B9F6A3D1-7E2C-4F5A-9A1B-3C8D5E7F0A2B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\DarkSync
DefaultGroupName={#MyAppName}
LicenseFile=..\README.md
OutputDir=..\dist
OutputBaseFilename=DarkSync-{#MyAppVersion}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=120
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\icon_main.ico
UninstallDisplayIcon={app}\icon_main.ico
UninstallDisplayName={#MyAppName}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoCopyright=Copyright (C) {#MyAppPublisher}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
MinVersion=10.0.17763

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; -- Application source -------------------------------------------
Source: "..\DarkSync 2.0.py";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\darksync_desktop.py";   DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt";      DestDir: "{app}"; Flags: ignoreversion

; -- Icons --------------------------------------------------------
Source: "..\icon_main.ico";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon_main.png";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon_desktop.ico";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon_desktop.png";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\darksync_icon.png";     DestDir: "{app}"; Flags: ignoreversion

; -- Helper scripts -----------------------------------------------
Source: "..\setup.bat";             DestDir: "{app}"; Flags: ignoreversion
Source: "..\update.bat";            DestDir: "{app}"; Flags: ignoreversion
Source: "..\update.sh";             DestDir: "{app}"; Flags: ignoreversion
Source: "..\create_shortcuts.bat";  DestDir: "{app}"; Flags: ignoreversion
Source: "..\create_shortcuts.ps1";  DestDir: "{app}"; Flags: ignoreversion

; -- Documentation ------------------------------------------------
Source: "..\README.md";             DestDir: "{app}"; Flags: ignoreversion
Source: "..\DAILY_RUN_GUIDE.md";    DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\logs";                Flags: uninsalwaysuninstall
Name: "{app}\.darksync_undo";      Flags: uninsalwaysuninstall
Name: "{app}\.darksync_guard";     Flags: uninsalwaysuninstall

[Icons]
; -- Start Menu ---------------------------------------------------
Name: "{group}\DarkSync 2.0";      Filename: "python"; Parameters: """{app}\{#MyAppExeName1}"""; WorkingDir: "{app}"; IconFilename: "{app}\icon_main.ico"; Comment: "DarkSync 2.0 Multi-Job Edition"
Name: "{group}\DarkSync Desktop";  Filename: "python"; Parameters: """{app}\{#MyAppExeName2}"""; WorkingDir: "{app}"; IconFilename: "{app}\icon_desktop.ico"; Comment: "DarkSync Desktop Edition"
Name: "{group}\Readme";            Filename: "{app}\README.md"
Name: "{group}\Uninstall";         Filename: "{uninstallexe}"

[Run]
; -- Install Python dependencies ----------------------------------
Filename: "python"; Parameters: "-m pip install --upgrade pip --quiet"; StatusMsg: "Upgrading pip..."; Flags: runhidden waituntilterminated
Filename: "python"; Parameters: "-m pip install -r ""{app}\requirements.txt"""; StatusMsg: "Installing Python dependencies..."; Flags: runhidden waituntilterminated

; -- Create desktop shortcuts (interactive installs only) ---------
Filename: "python"; Parameters: """{app}\create_shortcuts.ps1"""; StatusMsg: "Creating Desktop shortcuts..."; Flags: runhidden waituntilterminated skipifsilent

; -- Launch after install -----------------------------------------
Filename: "python"; Parameters: """{app}\{#MyAppExeName1}"""; Description: "Launch DarkSync 2.0 now"; Flags: nowait postinstall skipifsilent
Filename: "python"; Parameters: """{app}\{#MyAppExeName2}"""; Description: "Launch DarkSync Desktop now"; Flags: nowait postinstall skipifsilent

[Code]
// ============================================================================
// Pascal Script - pre-install checks
// ============================================================================

function IsPythonInstalled: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('python', '--version', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

function GetPythonVersion: String;
var
  TmpFile: String;
  Content: AnsiString;
  ResultCode: Integer;
begin
  Result := '';
  TmpFile := ExpandConstant('{tmp}\pyver.txt');
  Exec('cmd', '/c python --version > "' + TmpFile + '" 2>&1', '', SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode);
  if LoadStringFromFile(TmpFile, Content) then
  begin
    Result := Trim(String(Content));
    if Pos('Python ', Result) = 1 then
      Result := Copy(Result, 8, Length(Result));
  end;
end;

function CompareVersions(V1, V2: String): Integer;
var
  P1, P2, N1, N2: Integer;
begin
  Result := 0;
  repeat
    P1 := Pos('.', V1);
    P2 := Pos('.', V2);
    if P1 > 0 then N1 := StrToIntDef(Copy(V1, 1, P1 - 1), 0)
    else           N1 := StrToIntDef(V1, 0);
    if P2 > 0 then N2 := StrToIntDef(Copy(V2, 1, P2 - 1), 0)
    else           N2 := StrToIntDef(V2, 0);
    if N1 < N2 then begin Result := -1; Exit; end;
    if N1 > N2 then begin Result := 1; Exit; end;
    if P1 > 0 then Delete(V1, 1, P1);
    if P2 > 0 then Delete(V2, 1, P2);
  until (P1 = 0) and (P2 = 0);
end;

function InitializeSetup: Boolean;
var
  Version: String;
  ErrCode: Integer;
begin
  Result := True;

  if not IsPythonInstalled then
  begin
    if MsgBox('Python 3.8 or newer was not found in PATH.' + #13#10 +
              'Python is required to run DarkSync.' + #13#10 + #13#10 +
              'Would you like to open the Python download page now?',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://www.python.org/downloads/', '', '', SW_SHOWNORMAL, ewNoWait, ErrCode);
    Result := False;
    Exit;
  end;

  Version := GetPythonVersion;
  if Version = '' then
  begin
    MsgBox('Could not determine the Python version.' + #13#10 +
           'Please ensure Python 3.8+ is installed and in PATH.',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  if CompareVersions(Version, '3.8.0') < 0 then
  begin
    MsgBox('Python ' + Version + ' found, but DarkSync requires 3.8 or newer.',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  Log('Python ' + Version + ' detected OK');
end;

// ============================================================================
// Wizard customisation
// ============================================================================
