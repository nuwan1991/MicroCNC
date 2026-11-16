; Inno Setup script: MicroPCB-Setup-<version>.exe from the PyInstaller folder.
; Build with packaging\build_installer.ps1 (passes /DAppVersion=...).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0B7C2A-4D1F-4C55-9B8E-1A3F7C5D2E90}
AppName=MicroPCB
AppVersion={#AppVersion}
AppVerName=MicroPCB {#AppVersion}
AppPublisher=MicroPCB
DefaultDirName={autopf}\MicroPCB
DefaultGroupName=MicroPCB
DisableProgramGroupPage=yes
; per-user install by default (no admin needed); the user may pick all users
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=MicroPCB-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=MicroPCB {#AppVersion}
UninstallDisplayIcon={app}\MicroPCB.exe

[Tasks]
Name: "desktopicon"; Description: "Desktop shortcuts"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\MicroPCB\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\examples\*.nc"; DestDir: "{app}\examples"; Flags: ignoreversion
Source: "..\docs\safety.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\MicroPCB"; Filename: "{app}\MicroPCB.exe"
Name: "{group}\MicroPCB CAM"; Filename: "{app}\MicroPCB-CAM.exe"
Name: "{group}\Example G-code"; Filename: "{app}\examples"
Name: "{group}\Uninstall MicroPCB"; Filename: "{uninstallexe}"
Name: "{autodesktop}\MicroPCB"; Filename: "{app}\MicroPCB.exe"; Tasks: desktopicon
Name: "{autodesktop}\MicroPCB CAM"; Filename: "{app}\MicroPCB-CAM.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MicroPCB.exe"; Description: "Start MicroPCB"; Flags: nowait postinstall skipifsilent
