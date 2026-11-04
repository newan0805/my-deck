; Inno Setup script for the LAN Deck Windows installer.
; Build after PyInstaller:  iscc /DMyAppVersion=1.0.0 packaging\windows\LANDeck.iss
; Installs for the current user only (no admin rights needed).

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{6F1B9C3E-4A7D-4E21-9C58-2B7A0D9E5F14}
AppName=LAN Deck
AppVersion={#MyAppVersion}
AppVerName=LAN Deck {#MyAppVersion}
AppPublisher=newan0805
AppPublisherURL=https://github.com/newan0805/my-deck
DefaultDirName={localappdata}\Programs\LAN Deck
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\release
OutputBaseFilename=LAN-Deck-{#MyAppVersion}-windows-setup
SetupIconFile=..\..\build\icon.ico
UninstallDisplayIcon={app}\LANDeck.exe
UninstallDisplayName=LAN Deck
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force

[Tasks]
Name: "autostart"; Description: "Start LAN Deck when I sign in to Windows"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\..\dist\LANDeck.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\LAN Deck"; Filename: "{app}\LANDeck.exe"
Name: "{autodesktop}\LAN Deck"; Filename: "{app}\LANDeck.exe"; Tasks: desktopicon

[Registry]
; Same entry the app's own "Start at login" switch writes, so the two stay in sync.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LAN Deck"; ValueData: """{app}\LANDeck.exe"" --background"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\LANDeck.exe"; Description: "Start LAN Deck now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM LANDeck.exe"; Flags: runhidden; RunOnceId: "StopLANDeck"
