; Inno Setup script: MicroCNC-Setup-<version>.exe from the PyInstaller folder.
; Build with packaging\build_installer.ps1 (passes /DAppVersion=...).
; AppId is unchanged from the MicroPCB installers, so this one replaces them.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0B7C2A-4D1F-4C55-9B8E-1A3F7C5D2E90}
AppName=MicroCNC
AppVersion={#AppVersion}
AppVerName=MicroCNC {#AppVersion}
AppPublisher=Microchip Solutions (Pvt) Ltd
AppPublisherURL=https://www.microchip.lk/microcnc
AppSupportURL=https://www.microchip.lk/microcnc
AppUpdatesURL=https://www.microchip.lk/MicroCNC
DefaultDirName={autopf}\MicroCNC
DefaultGroupName=MicroCNC
DisableProgramGroupPage=yes
; per-user install by default (no admin needed); the user may pick all users
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=MicroCNC-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=MicroCNC {#AppVersion}
UninstallDisplayIcon={app}\MicroCNC.exe

[Tasks]
Name: "desktopicon"; Description: "Desktop shortcuts"; GroupDescription: "Shortcuts:"

[InstallDelete]
; program files of the older MicroPCB builds in the same folder
Type: files; Name: "{app}\MicroPCB.exe"
Type: files; Name: "{app}\MicroPCB-CAM.exe"
Type: files; Name: "{group}\MicroPCB.lnk"
Type: files; Name: "{group}\MicroPCB CAM.lnk"
Type: files; Name: "{group}\Uninstall MicroPCB.lnk"
Type: files; Name: "{autodesktop}\MicroPCB.lnk"
Type: files; Name: "{autodesktop}\MicroPCB CAM.lnk"

[Files]
Source: "..\dist\MicroCNC\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\examples\*.nc"; DestDir: "{app}\examples"; Flags: ignoreversion
Source: "..\docs\safety.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\user-guide.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\MicroCNC"; Filename: "{app}\MicroCNC.exe"
Name: "{group}\MicroCNC CAM"; Filename: "{app}\MicroCNC-CAM.exe"
Name: "{group}\Example G-code"; Filename: "{app}\examples"
Name: "{group}\Uninstall MicroCNC"; Filename: "{uninstallexe}"
Name: "{autodesktop}\MicroCNC"; Filename: "{app}\MicroCNC.exe"; Tasks: desktopicon
Name: "{autodesktop}\MicroCNC CAM"; Filename: "{app}\MicroCNC-CAM.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MicroCNC.exe"; Description: "Start MicroCNC"; Flags: nowait postinstall skipifsilent
