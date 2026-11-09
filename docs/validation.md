# Validating Plebdroid

Host checks, the on-device instrumentation test and the startup lifecycle
check, moved from the README.

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
sleep and wake, and requires the display to leave the awake state before the
wake step. The same application process must render the desktop and record
that the desktop opened. It leaves the device awake. Keep its evidence outside
the checkout:

```sh
python3 tools/test_startup_lifecycle.py --serial DEVICE_SERIAL \
  --case asleep-start --output /path/to/evidence/asleep-start
python3 tools/test_startup_lifecycle.py --serial DEVICE_SERIAL \
  --case interrupt-start --output /path/to/evidence/interrupt-start
```

Use `--case awake-start` as the control and `--adb /path/to/adb` when ADB is not
on the host's PATH. Review the screenshots and engine log as well as the result.
