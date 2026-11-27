; SPDX-License-Identifier: GPL-3.0-only
; The Windows installer, I-Need-A-Budget-<version>-Setup.exe (Inno Setup 6.3 or later: x64compatible, and
; this file is UTF-8 without a BOM). scripts/build-windows.sh
; compiles it from the PyInstaller folder:
;
;   iscc /DVersion=1.5.0 /DDist=<dist\I Need A Budget> /DOutput=<folder> scripts\installer.iss
;
; Per-user: no administrator, into %LOCALAPPDATA%\Programs\I Need A Budget, with a Start menu
; entry and an uninstaller (Settings → Apps). Unsigned (docs/adr/0009): Windows shows "Windows
; protected your PC" the first time (More info → Run anyway).
;
; It never touches the person's data: this computer's data folder (%LOCALAPPDATA%\I Need A Budget),
; the budget folder, the backups and the window's place in the registry stay through an update, an
; uninstall and a reinstall, which picks the budget back up.
;
; A newer installer installs over the older one in the same folder (the same AppId). An update
; (#188) runs it once the app has quit, quietly:
;
;   I-Need-A-Budget-<version>-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;
; While the app is open (the mutex in AppMutex, computer_windows.RUNNING_MUTEX) it asks for it to
; be closed first, and quietly it gives up instead, with a non-zero exit code. So does the uninstaller.

#ifndef Version
  #error Say which version: /DVersion=1.2.3 (main.py's VERSION)
#endif
#ifndef Dist
  #error Say where the PyInstaller folder is: /DDist=...\I Need A Budget
#endif
#ifndef Output
  #define Output "."
#endif
#define Name "I Need A Budget"
#define Exe "I Need A Budget.exe"
#define Repository "https://github.com/DigitalN/I-Need-A-Budget"

[Setup]
; Never change AppId: it's how a newer installer finds the copy to replace.
AppId={{BA0375DB-A1F9-4F91-A81B-A0B01ABF18B6}
AppName={#Name}
AppVersion={#Version}
AppVerName={#Name} {#Version}
AppPublisher=DigitalN
AppPublisherURL={#Repository}
AppSupportURL={#Repository}/issues
AppUpdatesURL={#Repository}/releases
AppCopyright=© 2026 DigitalN. Free software under the GNU GPL v3.
VersionInfoVersion={#Version}
VersionInfoDescription={#Name} Setup
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#Name}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AppMutex={#Name} is running
CloseApplications=no
RestartApplications=no
SetupIconFile=..\Application Files\INeedABudget.ico
UninstallDisplayIcon={app}\{#Exe}
UninstallDisplayName={#Name}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir={#Output}
OutputBaseFilename=I-Need-A-Budget-{#Version}-Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[InstallDelete]
; The app is replaced whole: a file an older version had and this one doesn't would still be loaded.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Dist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The licenses beside the app too, where people look for them.
Source: "{#Dist}\_internal\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\_internal\NOTICE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\_internal\Third-party licenses.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#Name}"; Filename: "{app}\{#Exe}"

[Run]
Filename: "{app}\{#Exe}"; Description: "Open {#Name}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Runtime = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Page = 'https://developer.microsoft.com/microsoft-edge/webview2/';

{ Microsoft's WebView2 Runtime draws the app's page. Windows 11 has it; most Windows 10 PCs too. }
function HasWebView2(Root: Integer; Key: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, Key + WebView2Runtime, 'pv', Version)
    and (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup: Boolean;
var
  ErrorCode: Integer;
begin
  Result := HasWebView2(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\')
    or HasWebView2(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\');
  if not Result then
  begin
    Log('Microsoft Edge WebView2 Runtime is missing.');
    if SuppressibleMsgBox('{#Name} needs Microsoft''s WebView2 Runtime, which this PC doesn''t have.'
        + #13#10#13#10 + 'Open Microsoft''s download page? Install the Evergreen runtime from there, then run this installer again.',
        mbConfirmation, MB_YESNO, IDNO) = IDYES then
      ShellExec('open', WebView2Page, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;
