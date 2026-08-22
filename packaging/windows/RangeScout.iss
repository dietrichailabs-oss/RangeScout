#ifndef RuntimeRoot
  #error RuntimeRoot must point to the frozen one-directory runtime.
#endif
#ifndef OutputDir
  #error OutputDir must point to the public release output directory.
#endif
#ifndef AppVersion
  #define AppVersion "1.6.2"
#endif
#ifndef AppPublisher
  #define AppPublisher "Dietrich AI Labs"
#endif
#ifndef BuildIdentity
  #define BuildIdentity "rs-v1.6.2-ux-data-fusion-eng1"
#endif
#ifndef SetupBaseFilename
  #define SetupBaseFilename "RangeScout_1.6.2_Setup"
#endif
#ifndef AppIcon
  #error AppIcon must point to the frozen RangeScout icon.
#endif

[Setup]
AppId={{4A97E81B-630D-4A27-B60B-9004B61D69F3}
AppName=RangeScout
AppVersion={#AppVersion}
AppVerName=RangeScout {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/dietrichailabs-oss/RangeScout
AppSupportURL=https://github.com/dietrichailabs-oss/RangeScout/issues
AppUpdatesURL=https://github.com/dietrichailabs-oss/RangeScout/releases
DefaultDirName={autopf}\RangeScout
DefaultGroupName=RangeScout
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#SetupBaseFilename}
SetupIconFile={#AppIcon}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
Uninstallable=yes
UninstallDisplayName=RangeScout {#AppVersion}
UninstallDisplayIcon={app}\RangeScout.exe
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=RangeScout {#AppVersion} Setup
VersionInfoProductName=RangeScout
VersionInfoProductVersion={#AppVersion}.0
VersionInfoCopyright=Copyright (c) 2026 {#AppPublisher}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\notices"

[Files]
Source: "{#RuntimeRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs notimestamp

[Icons]
Name: "{autoprograms}\RangeScout"; Filename: "{app}\RangeScout.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\RangeScout"; Filename: "{app}\RangeScout.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\RangeScout.exe"; Description: "Launch RangeScout"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent unchecked

[Registry]
Root: HKA; Subkey: "Software\RangeScout"; ValueType: string; ValueName: "BuildIdentity"; ValueData: "{#BuildIdentity}"; Flags: uninsdeletevalue uninsdeletekeyifempty
