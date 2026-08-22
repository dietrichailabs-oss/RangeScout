# Qt/PySide6 corresponding-source instructions

RangeScout 1.1.0 ships Qt, PySide6, and shiboken6 version 6.11.1 under their applicable
LGPL/GPL license alternatives. Recipients may inspect, modify, rebuild, replace, and relink those
libraries as permitted by the included LGPLv3 license.

The exact corresponding-source archives below are retained under RangeScout distributor control and
must remain available unchanged through the stable project-controlled corresponding-source location
linked from the public release README:

- `pyside-setup-everywhere-src-6.11.1.tar.xz`
- `qtbase-everywhere-src-6.11.1.tar.xz`
- `qtdeclarative-everywhere-src-6.11.1.tar.xz`
- `qtsvg-everywhere-src-6.11.1.tar.xz`

The following exact archive is retained under RangeScout project control but is not
embedded in the master handoff because its 578,914,356-byte size would exceed the 512 MiB handoff
limit:

- `qtwebengine-everywhere-src-6.11.1.tar.xz`

The release owner must make that retained archive available unchanged from the README-linked
project-controlled source location. Before publication, verify its byte size and
SHA-256 against `notices/QT_CORRESPONDING_SOURCE_ASSET_MANIFEST.json`. RangeScout publication remains
blocked if this retained asset cannot be produced and verified. This is a distributor-controlled
availability plan; the upstream URL records provenance only and is not the delivery mechanism.

Verify every archive against `notices/QT_CORRESPONDING_SOURCE_ASSET_MANIFEST.json`. That manifest
records the exact byte size, SHA-256, provenance, covered source module, and mapping to the final
runtime SBOM and whether each asset is embedded in this handoff or retained in the controlled release
vault. The source archives are retained by the RangeScout project at a stable project-controlled
source location linked from the public README; they are not custom GitHub Release assets.

Qt Virtual Keyboard is not distributed in the RangeScout 1.1 Windows runtime and its source module is not part
of this corresponding-source set. The Windows platform plugin `qwindows.dll` remains included.
