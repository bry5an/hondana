; Inno Setup script. Build with:  iscc /DAppVersion=0.1.0 packaging\windows\hondana.iss
; (run after PyInstaller has made dist\Hondana). Installs for the current user: no admin prompt.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1E0D2A-5B7C-4C1E-9B53-7A1D5E6C0A11}
AppName=Hondana
AppVersion={#AppVersion}
AppPublisher=Hondana contributors
AppPublisherURL=https://github.com/bry5an/hondana
DefaultDirName={autopf}\Hondana
DefaultGroupName=Hondana
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=Hondana-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\hondana.exe
LicenseFile=..\..\LICENSE

[Files]
Source: "..\..\dist\Hondana\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Hondana"; Filename: "{app}\hondana.exe"
Name: "{autodesktop}\Hondana"; Filename: "{app}\hondana.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\hondana.exe"; Description: "Start Hondana"; Flags: nowait postinstall skipifsilent
