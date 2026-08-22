# RangeScout 1.1 release engineering

RangeScout 1.1 uses one frozen PyInstaller one-directory runtime as the input to its internal Windows verification formats.

## Internal engineering artifact contract

The internal engineering and QA set contains:

- `RangeScout_1.1.0_Setup.exe`
- `RangeScout_1.1.0_Portable.zip`
- `RangeScout_1.1.0_Source.zip`
- `SHA256SUMS.txt`

The installer is compiled with Inno Setup 6 from `packaging/windows/RangeScout.iss`. The portable ZIP and installer consume the same runtime directory; the installer adds only Inno's uninstaller metadata and operating-system shortcuts/registration.

## Public GitHub release asset contract

The public GitHub Release contains exactly two custom uploaded assets:

- `RangeScout_1.1.0_Setup.exe`
- `README.md`

Portable/source ZIPs, checksums, QA evidence, SBOMs, logs, handoff bundles, and internal manifests remain internal. GitHub-generated source links are allowed. Corresponding source required by third-party licensing remains at a stable project-controlled location linked from the public README.

## Build

```powershell
python scripts/release_engineering.py `
  --output G:\RangeScoutBuild\internal `
  --work G:\RangeScoutBuild\work
```

Set `INNO_ISCC` when `ISCC.exe` is not installed in an Inno Setup 6 default location.

## Installer behavior

- Default elevated installation uses the Windows Program Files location.
- `/CURRENTUSER` provides a supported per-user installation override.
- Paths containing spaces are supported.
- A Start Menu shortcut is installed.
- The Desktop shortcut is optional and unchecked by default.
- Add/Remove Programs registration is provided by Inno Setup.
- Known runtime directories are refreshed during upgrade.
- Uninstall removes installed application payload and shortcuts.
- `%AppData%\RangeScout` and user-exported CSV files are not deleted by install, upgrade, or uninstall directives.

The M0 installer evidence harness verifies portable launch, default and spaced-path installation,
installed payload parity, installed-copy uninstall, simulated legacy-runtime upgrade, exported-CSV
preservation, and `%AppData%\RangeScout` preservation.

`RangeScout.exe` is never published as a standalone portable application; it requires the adjacent `_internal` runtime.
