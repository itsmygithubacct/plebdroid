# Plebdroid

Plebian-OS's Kilix desktop as an Android application. Plebdroid uses the real
Kilix terminal engine, Kilix 95 desktop and shared desktop SDK. Python, Pillow,
fonts and native helpers are packaged in the APK. The application ID is
`io.github.itsmygithubacct.plebdroid`.

This is an unpublished development build. The desktop runs on the native
Android runtime. A separate Debian terminal provides a persistent userspace
and `apt`. A successful APK build alone does not establish device support.

## Use

The desktop opens in a terminal tab. **Desktop** returns to the existing
desktop process, and **Terminal** opens the Android system shell. **Debian**
installs the packaged Debian userspace on first use and opens its Bash shell.
**More** includes Lights, terminal splitting, background mode and pane/tab navigation. **Host** connects to a
Kilix host using its address, token and pinned TLS certificate fingerprint.

The desktop includes Notepad, Calculator, Paint, File Manager, Minesweeper
and Solitaire. Android mode uses touch-sized controls and maps pointer
coordinates to the same logical canvas used for drawing. The terminal's
keyboard button opens the Android keyboard; Esc, Tab, Ctrl, Alt and arrows
remain available on screen.

Save desktop files in **Documents**. The native **Files** control imports a
file through Android's document picker or exports a saved document. Imports
are limited to 256 MiB and receive a new name if one already exists. Documents
and desktop settings live outside the replaceable runtime directory and
survive application updates. Uninstalling the application removes its private
files; export documents that you want to keep.

Debian needs a native ARM64 device and about 450 MiB of free space for its
first installation. Each Debian tab uses the same installed system, with
Documents available at `/root/Documents`. Packages and user changes survive
app updates; a newer packaged image never replaces an existing installation.
Use `apt update` and `apt install PACKAGE` inside Debian. DNS comes from
Android's active network when a Debian session opens. Open a new session if
the network's DNS servers change. X11 applications are not included.

Opening Debian enables background mode. **More → Background sessions** can
disable it or request the session notification. This keeps the Android
service in the foreground while you use other apps, but cannot prevent
force-stop, device resource limits or manufacturer task killers. There is
no automatic start at boot.

## Build

The build host is Linux x86-64. Use JDK 17, Android SDK platform 36, build
tools 36.0.0, NDK 30.0.16248370 and Android CMake 3.22.1. The wrapper pins
Gradle 8.13 and the project pins Android Gradle Plugin 8.13.2. The APK targets
API 36 and ARM64, with API 29 as its declared minimum. The engine requires
OpenGL ES 3.2.

Supply the component source checkouts listed in `dependencies.lock.json`, a
CPython 3.14 Android ARM64 prefix, and the Kilix Android native dependency
prefix. The component revisions are local development candidates and are
not all available from public remotes yet. Kilix Android's
`tools/build_native_dependencies.py` builds its prefix from pinned archives.
The Python prefix must contain the standard library, headers, `libpython`
and its OpenSSL and SQLite libraries.

The shared Android runtime downloads Symbols Nerd Font Mono from its pinned
upstream release. Its helper verifies both the archive and font hashes and
packages the font with its notices. The first build needs HTTPS access to
GitHub, or the verified archive in the runtime's `build/downloads/symbol-font`
cache. Cleaning that runtime's build directory also removes this cache.

Build the pinned Debian image with `tools/build_debian_rootfs.sh OUTPUT_DIR`.
It requires mmdebstrap 1.5.7, xz 5.8.1, Debian archive keys, qemu-user-binfmt
and unprivileged user namespaces with subordinate UID/GID ranges. Its
snapshot, package list, neutral host identity and output hashes are pinned
in `native/debian/pins.json`. The root filesystem is a build input, not a
Git-tracked binary.

Set `JAVA_HOME` and `ANDROID_SDK_ROOT`, then provide each source explicitly:

```sh
./gradlew --no-daemon --max-workers=2 \
  -PkilixAndroidSource=/path/to/kilix-android \
  -PkilixEngineSource=/path/to/kilix-engine \
  -PkilixDesktopSource=/path/to/kilix-95 \
  -PkilixHostSource=/path/to/kilix \
  -PkilixStateSource=/path/to/kilix-state \
  -PkilixContentSource=/path/to/kilix-content \
  -PkilixFramePresenterSource=/path/to/kitty-frame-presenter \
  -PkilixPtyBrokerSource=/path/to/kitty-pty-broker \
  -PkilixTouchSource=/path/to/kilix-touch \
  -PkilixMuxSource=/path/to/kilix-multiplexer \
  -PkilixGamesSource=/path/to/kilix-games \
  -PkilixGameKitSource=/path/to/kilix-game-kit \
  -PkilixFramebufferSource=/path/to/kitty-framebuffer \
  -PkilixPythonPrefix=/path/to/android-python-prefix \
  -PkilixNativeDependencies=/path/to/android-native-prefix \
  -PplebdroidDebianRootfs=/path/to/debian-trixie-arm64-20261027T000000Z.tar.xz \
  :app:assembleDebug :app:assembleDebugAndroidTest
```

The game kit path is the `kilix-game-kit` subdirectory of its enclosing
repository; its recursive submodules must be initialized. The host SDK's
state, content and presenter components are supplied explicitly and packaged
in the layout its existing loaders expect. No second SDK implementation is
included.

`tools/build_python_apps.py` builds the packaged Python launcher, Pillow and
state library. `tools/prepare_desktop.py` verifies every native output hash
before combining the desktop, SDK, fonts and notices with Kilix Android's
runtime. Generated files stay under `app/build`. The native source/download
cache can be set with `-PplebdroidNativeCache=/path/to/cache`.

`tools/build_debian_runtime.py` builds the pinned PRoot and loader with the
same NDK, verifies their ELF layout and hashes, and checks the rootfs archive
and decompressed tar. Its cache can be set with `-PplebdroidDebianCache`.
The rootfs asset stays outside `runtime.zip`; installation into persistent
app storage happens only when the user opens Debian.

Release builds require clean source checkouts matching the lock file. Add
`-PkilixVerifyPins=true` to apply the same checks to a debug build. Ordinary
debug builds accept explicit development trees. The source gate does not
certify an arbitrary supplied Python or native prefix.

## Validation

Host checks:

```sh
python3 tools/test_documents.py
python3 tools/test_packaging.py
python3 -m unittest tests/debian/test_debian.py
```

The device test runs under the application's UID. It exercises desktop
startup and refocusing, local PTY Unicode input, packaged Python/Pillow,
document persistence, rotation and Activity recreation. Use an awake device
so Android supplies a rendering surface. Install both APKs, then run:

```sh
adb -s DEVICE_SERIAL install -r app/build/outputs/apk/debug/app-debug.apk
adb -s DEVICE_SERIAL install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb -s DEVICE_SERIAL shell am instrument -w \
  io.github.itsmygithubacct.plebdroid.test/io.github.itsmygithubacct.plebdroid.PlebdroidTest
```

The test saves its report and screenshots in app-private files. Inspect the
report and rendered screenshots before claiming the desktop works on a
device. ARM64 translation on an x86 emulator does not qualify a physical
ARM64 device. Minimum-API and physical 16 KiB device support remain unverified.

Add `-e apps true` to check touch-opened Notepad, committed Unicode text,
Enter and Tab, and Calculator input. Review the Calculator screenshot for
the expected result, 42. The base suite also measures the rendered cursor
against touchscreen coordinates and checks foreground-service start, stop
and rapid cancellation without restarting the desktop.

Add `-e debian true` to the instrumentation command for the Debian checks.
On native ARM64 this installs Debian if absent, runs `apt update`, installs
the small `hello` package, checks Documents in both directions and across
sessions, and exercises a background child with the foreground service.
It retains the installed package and qualification documents. On an x86
emulator it checks that ARM translation is explained before installation;
translated PRoot is unsupported. The result is `plebdroid-debian-test.json`.

For a test device reached through a network proxy, add `-e apt_proxy URL`.
This passes the proxy only to the qualification commands, without changing
Debian's saved APT configuration. In that mode the test disables APT's
address-configuration filter so an offline device can reach a loopback
proxy over ADB reverse. HTTPS certificate and package-signature checks remain
enabled. Record proxy use separately from direct network qualification.

The startup lifecycle check requires host Pillow and a debug APK using the
default teal desktop. It stops Plebdroid, preserves application data, exercises
sleep and wake, and requires the same application process to render the desktop
afterward. It leaves the device awake. Keep its evidence outside the checkout:

```sh
python3 tools/test_startup_lifecycle.py --serial DEVICE_SERIAL \
  --case asleep-start --output /path/to/evidence/asleep-start
python3 tools/test_startup_lifecycle.py --serial DEVICE_SERIAL \
  --case interrupt-start --output /path/to/evidence/interrupt-start
```

Use `--case awake-start` as the control and `--adb /path/to/adb` when ADB is not
on the host's PATH. Review the screenshots and engine log as well as the result.

## License

Plebdroid source is GPL-3.0-only. See `LICENSE` and
`THIRD_PARTY_NOTICES.md`. Packaged components, fonts and Plebian artwork keep
their upstream notices in the application runtime.
