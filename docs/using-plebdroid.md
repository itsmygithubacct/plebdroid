# Using Plebdroid

The session controls, the desktop, Documents, Debian and background mode,
moved from the README.

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
