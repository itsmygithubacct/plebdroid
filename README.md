# Plebdroid

Plebdroid packages the Plebian-OS Kilix desktop as an Android application.

Plebian-OS's Kilix desktop as an Android application. Plebdroid uses the real
Kilix terminal engine, Kilix 95 desktop and shared desktop SDK. Python, Pillow,
fonts and native helpers are packaged in the APK. The application ID is
`io.github.itsmygithubacct.plebdroid`.

This is an unpublished development build. The desktop runs on the native
Android runtime. A separate Debian terminal provides a persistent userspace
and `apt`. A successful APK build alone does not establish device support.

**Status:** not part of any Plebian-OS release. Nothing in Plebian-OS 0.2.2,
0.2.3 (release candidate `v0.2.3-rc6`), Pleb or Kilix builds or installs it,
and the repository publishes no APK. The version is `0.1.0-dev`, and you build
it yourself from source checkouts listed in `dependencies.lock.json`.

## What it does

- Opens the Kilix 95 desktop (Notepad, Calculator, Paint, File Manager,
  Minesweeper and Solitaire) in a terminal tab on an Android device, with
  touch-sized controls.
- Opens the Android system shell in a terminal tab.
- Installs a packaged Debian userspace on first use and opens Bash in it, with
  `apt` and a persistent installation shared by every Debian tab.
- Imports and exports files between Android's document picker and the
  desktop's **Documents** folder, which survives application updates.
- Connects to a Kilix host by address, token and pinned TLS certificate
  fingerprint.
- Keeps sessions running in the background through a foreground service while
  Debian is in use.

## Quick start

There is no published APK. Building one needs a Linux x86-64 host with JDK 17
and the Android SDK and NDK versions listed in
[docs/building.md](docs/building.md), plus a checkout of each component source
named in `dependencies.lock.json`. Several of those revisions are local
development candidates that are not available from public remotes, so a clean
build from public sources alone is not possible yet.

With the sources in place, the build is one Gradle call that names each source
with a `-Pkilix...Source=` property; the full command is in
[docs/building.md](docs/building.md), and
[docs/validation.md](docs/validation.md) shows the `adb install` and device
test steps. The host checks run without a
device; `tools/test_documents.py` compiles with the JDK's `javac`, taken from
`JAVA_HOME` or `/usr/bin`:

```sh
python3 tools/test_documents.py
python3 tools/test_packaging.py
python3 -m unittest tests/debian/test_debian.py
```

## Usage

The session controls along the top of the application:

| Control | Effect |
| --- | --- |
| **Desktop** | open the Kilix 95 desktop, or return to the running one |
| **Terminal** | open the Android system shell |
| **Debian** | install Debian on first use, then open its Bash shell; turns on background mode |
| **Files** | import a file into Documents (up to 256 MiB) or export a saved document |
| **Host** | connect to a Kilix host |
| **More** | Lights, terminal splitting, background sessions, pane and tab navigation |

Inside Debian, `apt update` and `apt install PACKAGE` work as usual, and
Documents is at `/root/Documents`. Debian needs a native ARM64 device and about
450 MiB of free space for its first installation; X11 applications are not
included. [docs/using-plebdroid.md](docs/using-plebdroid.md) has the details,
including what background mode can and cannot prevent.

## How it works

```text
 Plebdroid APK (io.github.itsmygithubacct.plebdroid, ARM64, API 29-36)
   PlebdroidActivity  -- extends the shared Kilix Android runtime's activity
     |  session controls: Desktop | Terminal | Debian | Files | Host | More
     v
 Kilix terminal engine (OpenGL ES 3.2)
     ├── Kilix 95 desktop + desktop SDK, run by the packaged CPython and Pillow
     ├── Android system shell in a local PTY
     └── Debian userspace through a pinned PRoot and loader
           (rootfs installed into app storage on first use)
```

The application is a small Java layer (`app/src/main/java/...`) on top of the
shared Kilix Android runtime. It adds the session controls, the Documents store
and the foreground session service; the terminal engine, desktop, SDK, state,
content, presenter, PTY broker, touch, multiplexer and game components come
from the source checkouts pinned in `dependencies.lock.json`. The build tools in
`tools/` build the packaged Python launcher and Pillow, verify every native
output hash, and combine the desktop, SDK, fonts and notices with the runtime.

The Debian image is built separately with `tools/build_debian_rootfs.sh` from
the snapshot, package list and hashes pinned in `native/debian/pins.json`, and
`tools/build_debian_runtime.py` builds the PRoot and loader with the same NDK.
The root filesystem is packaged as its own asset and installed into persistent
application storage only when the user opens Debian; a newer packaged image
never replaces an existing installation.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/using-plebdroid.md](docs/using-plebdroid.md) | the session controls, desktop apps, Documents, Debian and background mode |
| [docs/building.md](docs/building.md) | toolchain versions, component sources, the Debian image, the Gradle command, pin checks |
| [docs/validation.md](docs/validation.md) | host checks, the instrumentation test and its options, the startup lifecycle check |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | packaged third-party components and their notices |

## Related repositories

- [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme): the desktop
  the **Desktop** control opens.
- [Kilix](https://github.com/itsmygithubacct/kilix#readme): the host SDK
  (`-PkilixHostSource`) packaged with the desktop.
- Kilix components packaged in the APK:
  [kilix-state](https://github.com/itsmygithubacct/kilix-state#readme),
  [kilix-content](https://github.com/itsmygithubacct/kilix-content#readme),
  [kitty-frame-presenter](https://github.com/itsmygithubacct/kitty-frame-presenter#readme),
  [kitty-pty-broker](https://github.com/itsmygithubacct/kitty-pty-broker#readme),
  [kilix-multiplexer](https://github.com/itsmygithubacct/kilix-multiplexer#readme),
  [kilix-games](https://github.com/itsmygithubacct/kilix-games#readme),
  [kilix-game-sdk](https://github.com/itsmygithubacct/kilix-game-sdk#readme)
  (its `kilix-game-kit` directory) and
  [kitty-framebuffer](https://github.com/itsmygithubacct/kitty-framebuffer#readme).

The Android runtime, engine and touch sources the build also needs are not
published.

## Part of Plebian-OS

Plebdroid is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
It is not shipped: it is an unpublished Android development build made from
source. The map of every repository and how it ships is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

Plebdroid source is GPL-3.0-only. See `LICENSE` and
`THIRD_PARTY_NOTICES.md`. Packaged components, fonts and Plebian artwork keep
their upstream notices in the application runtime.
