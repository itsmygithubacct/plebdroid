# Building Plebdroid

Toolchain, component sources, the Debian image and the Gradle command,
moved from the README.

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
