# Optional Debian userspace: notices

Plebdroid can install a Debian 13 (trixie) arm64 userspace and run it under
PRoot. Three separately licensed parts are packaged for it.

## PRoot (`libproot.so`, `libproot_loader.so`)

- Upstream: <https://github.com/termux/proot>, tag `v5.1.107.101`, commit
  `b27d81a68d765f3462e807f4a14e01a03f8e89ea`. A Termux-maintained copy of
  <https://github.com/proot-me/PRoot>.
- License: GNU General Public License, version 2 or (at your option) any later
  version. Copyright STMicroelectronics and the PRoot contributors. The full
  upstream `COPYING` file is in `PRoot-COPYING.txt`.
- Plebdroid change: `native/debian/patches/proot-0001-ashmem-memfd-string-h.patch`
  adds a missing `#include <string.h>`. The loader is built unbundled, as a
  separate executable, so Android can run it from the app's native library
  directory.

## talloc (statically linked into `libproot.so`)

- Upstream: <https://talloc.samba.org/>, release 2.5.0,
  `https://www.samba.org/ftp/talloc/talloc-2.5.0.tar.gz`
  (SHA-256 `912afa237510ae542a7733998eb18a12bcda35ab6729c8e2ddb43e8d0ebab007`).
- License: GNU Lesser General Public License, version 3 or (at your option) any
  later version. This applies to `talloc.c` and to the `lib/replace` objects
  linked with it. The license text is in `talloc-LGPL-3.0.txt`. The GNU GPL
  version 3 text that it supplements is Plebdroid's own `LICENSE`.
- The combined `libproot.so` is distributed under GPL-3.0-or-later terms, which
  both components allow. Its complete source is the two pinned archives plus
  the files in `native/debian/` and `tools/build_debian_runtime.py`.

## Debian rootfs (`assets/debian/rootfs.tar.xz`)

- 80 binary packages from Debian 13.7 (trixie) arm64, snapshot
  `20261027T000000Z` of <https://snapshot.debian.org/>. `tools/build_debian_rootfs.sh`
  rebuilds it, and `native/debian/pins.json` records its digests.
- Each package keeps its own license. Its copyright and license statement is
  installed inside the rootfs at `/usr/share/doc/<package>/copyright`.
  `/usr/share/common-licenses/` holds the common license texts.
- The corresponding source packages for every installed version are in the
  same snapshot. In a running system, `dpkg-query -W` lists them. A
  distribution of this APK must also make those exact source packages
  available alongside it, as the GPL-licensed packages require.

After installation, apt updates the system from the live Debian mirrors under
Debian's own terms.
