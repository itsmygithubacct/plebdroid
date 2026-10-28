// SPDX-License-Identifier: GPL-3.0-only
package io.github.itsmygithubacct.plebdroid;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.widget.Button;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;
import java.util.function.BooleanSupplier;
import org.json.JSONObject;
import org.json.JSONArray;

/** Integration checks run under the real app UID with its PTYs and GPU surface. */
public final class PlebdroidTest extends Instrumentation {
    private Activity activity;
    private File files;
    private InputConnection input;
    private boolean debianOnly;
    private boolean desktopApps;
    private String aptProxy = "";
    private final List<String> passed = new ArrayList<>();

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        debianOnly = arguments != null && "true".equals(arguments.getString("debian"));
        desktopApps = arguments != null && "true".equals(arguments.getString("apps"));
        if (arguments != null) aptProxy = arguments.getString("apt_proxy", "");
        start();
    }

    private static void require(boolean value, String message) {
        if (!value) throw new AssertionError(message);
    }

    private void await(BooleanSupplier condition, String message) {
        await(condition, message, 60000);
    }

    private void await(BooleanSupplier condition, String message, long timeoutMillis) {
        long end = SystemClock.uptimeMillis() + timeoutMillis;
        while (!condition.getAsBoolean() && SystemClock.uptimeMillis() < end) SystemClock.sleep(100);
        require(condition.getAsBoolean(), message);
    }

    private String read(File path) {
        try { return new String(Files.readAllBytes(path.toPath()), StandardCharsets.UTF_8); }
        catch (Exception error) { return ""; }
    }

    private JSONObject json(String name) {
        try { return new JSONObject(read(new File(files, name))); }
        catch (Exception error) { return new JSONObject(); }
    }

    private View find(View root, String name) {
        if (name.equals("surface") && root instanceof SurfaceView) return root;
        if (root instanceof Button && ((Button) root).getText().toString().equals(name)) return root;
        if (root instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) root;
            for (int i = 0; i < group.getChildCount(); i++) {
                View found = find(group.getChildAt(i), name);
                if (found != null) return found;
            }
        }
        return null;
    }

    private void click(String name) {
        runOnMainSync(() -> {
            View view = find(activity.getWindow().getDecorView(), name);
            require(view != null, "Missing control: " + name);
            view.performClick();
        });
    }

    private void request(String name) {
        String previous = json("activity-request.json").optString("id");
        click(name);
        await(() -> !json("activity-request.json").optString("id").equals(previous), "Control did not publish a request: " + name);
        String current = json("activity-request.json").optString("id");
        await(() -> json("activity-result.json").optString("id").equals(current), "Request did not finish: " + name);
        JSONObject result = json("activity-result.json");
        require(result.optString("status").equals("opened") || result.optString("status").equals("focused"), result.toString());
    }

    private void connectInput() {
        runOnMainSync(() -> {
            View view = find(activity.getWindow().getDecorView(), "surface");
            require(view != null, "Missing terminal surface");
            view.requestFocus();
            input = view.onCreateInputConnection(new EditorInfo());
        });
    }

    private void command(String text) {
        runOnMainSync(() -> {
            require(input.commitText(text, 1), "IME rejected committed text");
            input.sendKeyEvent(new KeyEvent(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_ENTER));
            input.sendKeyEvent(new KeyEvent(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_ENTER));
        });
    }

    private static String quote(String text) { return "'" + text.replace("'", "'\\''") + "'"; }

    private static String pythonString(String text) {
        // Android's JSON encoder escapes '/', which Python string literals
        // preserve as a backslash. The other JSON string escapes work here.
        return JSONObject.quote(text).replace("\\/", "/");
    }

    private String shell(String command) throws Exception {
        try (InputStream in = new ParcelFileDescriptor.AutoCloseInputStream(
                 getUiAutomation().executeShellCommand(command));
             ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[65536];
            for (int count; (count = in.read(buffer)) != -1;) bytes.write(buffer, 0, count);
            return bytes.toString("UTF-8");
        }
    }

    private Bitmap capture() throws Exception {
        try (InputStream in = new ParcelFileDescriptor.AutoCloseInputStream(
                getUiAutomation().executeShellCommand("screencap -p"))) {
            Bitmap bitmap = BitmapFactory.decodeStream(in);
            require(bitmap != null, "Display capture failed");
            return bitmap;
        }
    }

    private void screenshot(String name) throws Exception {
        Bitmap bitmap = capture();
        try {
            try (FileOutputStream out = new FileOutputStream(new File(files, name))) {
                require(bitmap.compress(Bitmap.CompressFormat.PNG, 100, out), "Display capture was not saved");
            }
        } finally {
            bitmap.recycle();
        }
    }

    private int[] surfaceBounds() {
        int[] bounds = new int[4];
        runOnMainSync(() -> {
            View surface = find(activity.getWindow().getDecorView(), "surface");
            require(surface != null, "Missing terminal surface");
            int[] location = new int[2];
            surface.getLocationOnScreen(location);
            bounds[0] = location[0]; bounds[1] = location[1];
            bounds[2] = surface.getWidth(); bounds[3] = surface.getHeight();
        });
        return bounds;
    }

    private void verifyPointer(String label) throws Exception {
        int[] bounds = surfaceBounds();
        int x = bounds[0] + bounds[2] * 3 / 4;
        int y = bounds[1] + bounds[3] / 3;
        tap(x, y);
        SystemClock.sleep(700);
        Bitmap bitmap = capture();
        try {
            int white = 0, dark = 0, teal = 0;
            int firstX = Integer.MAX_VALUE, firstY = Integer.MAX_VALUE;
            // The cursor's hot spot is its upper-left tip. This patch is on
            // empty desktop space, far from icons, windows and the taskbar.
            for (int py = y - 5; py <= y + 30; py++) {
                for (int px = x - 5; px <= x + 25; px++) {
                    int color = bitmap.getPixel(px, py);
                    int r = Color.red(color), g = Color.green(color), b = Color.blue(color);
                    boolean light = r > 210 && g > 210 && b > 210;
                    boolean black = r < 50 && g < 50 && b < 50;
                    if (light) white++;
                    if (black) dark++;
                    if (light || black) { firstX = Math.min(firstX, px); firstY = Math.min(firstY, py); }
                    if (r < 12 && Math.abs(g - 128) < 12 && Math.abs(b - 128) < 12) teal++;
                }
            }
            require(white >= 5 && dark >= 3 && teal >= 500
                    && Math.abs(firstX - x) <= 6 && Math.abs(firstY - y) <= 6,
                label + " cursor is not at touch (" + x + "," + y + "): " + white + "/" + dark + "/" + teal
                    + " tip=" + firstX + "," + firstY);
            try (FileOutputStream out = new FileOutputStream(new File(files, "plebdroid-touch-" + label + ".png"))) {
                require(bitmap.compress(Bitmap.CompressFormat.PNG, 100, out), "Touch evidence was not saved");
            }
        } finally { bitmap.recycle(); }
        passed.add(label + " touchscreen position matches the rendered desktop cursor within 6 pixels");
    }

    private void tap(int x, int y) {
        long now = SystemClock.uptimeMillis();
        for (int action : new int[]{MotionEvent.ACTION_DOWN, MotionEvent.ACTION_UP}) {
            MotionEvent event = MotionEvent.obtain(now, SystemClock.uptimeMillis(), action, x, y, 0);
            event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
            require(getUiAutomation().injectInputEvent(event, true), "Touch injection failed");
            event.recycle();
            SystemClock.sleep(80);
        }
    }

    private List<Integer> desktopProcesses() {
        return applicationProcesses("kilix-95");
    }

    private List<Integer> applicationProcesses(String program) {
        ArrayList<Integer> pids = new ArrayList<>();
        File[] entries = new File("/proc").listFiles();
        if (entries == null) return pids;
        for (File entry : entries) {
            if (!entry.getName().matches("[0-9]+")) continue;
            String command = read(new File(entry, "cmdline"));
            String[] argv = command.split("\u0000", -1);
            // Broker ancestors also carry the desktop argv. Count only the
            // executable itself, including the emulator's ARM64 runner.
            int executable = argv.length > 1 && argv[0].endsWith("ndk_translation_program_runner_binfmt_misc_arm64") ? 1 : 0;
            if (argv[executable].endsWith("/libplebdroid_python.so")
                    && command.contains("/apps/" + program + "/main.py")) pids.add(Integer.parseInt(entry.getName()));
        }
        pids.sort(Integer::compareTo);
        return pids;
    }

    private void rotate(int rotation) throws Exception {
        shell("wm user-rotation lock " + rotation);
        await(() -> {
            View surface = find(activity.getWindow().getDecorView(), "surface");
            return surface != null && (rotation == 0 ? surface.getHeight() > surface.getWidth()
                : surface.getWidth() > surface.getHeight());
        }, "Viewport did not rotate");
        SystemClock.sleep(1800);
    }

    @Override public void onStart() {
        files = getTargetContext().getFilesDir();
        if (debianOnly) { runDebianChecks(); return; }
        Bundle result = new Bundle();
        String originalRotation = "";
        try {
            originalRotation = shell("wm user-rotation").trim();
            activity = startActivitySync(new Intent(getTargetContext(), PlebdroidActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            await(() -> new File(files, "engine-ready").isFile(), "Engine did not start", 240000);
            await(() -> !json("startup-result.json").optString("status").isEmpty(), "Desktop launch was not reported");
            require(json("startup-result.json").optString("status").equals("opened"), json("startup-result.json").toString());
            await(() -> desktopProcesses().size() == 1, "Desktop Python process did not remain running");
            SystemClock.sleep(3000);
            List<Integer> desktop = desktopProcesses();
            require(desktop.size() == 1, "Desktop exited during startup");
            screenshot("plebdroid-desktop.png");
            passed.add("packaged desktop starts as an actual Python PTY child");

            for (int i = 0; i < 3; i++) request("Desktop");
            require(desktopProcesses().equals(desktop), "Desktop button duplicated the desktop process");
            passed.add("Desktop refocuses its existing process");
            verifySessionService(desktop);

            request("Terminal"); connectInput();
            File proof = new File(files, "terminal-proof.txt");
            Files.deleteIfExists(proof.toPath());
            command("printf '%s' " + quote("Plebdroid π 世界") + " > " + quote(proof.getAbsolutePath()));
            await(() -> read(proof).equals("Plebdroid π 世界"), "Local PTY/IME Unicode did not reach the shell");
            passed.add("local terminal receives Unicode through the Android input connection");

            String nativeDir = getTargetContext().getApplicationInfo().nativeLibraryDir;
            File pythonProof = new File(files, "python-proof.json");
            Files.deleteIfExists(pythonProof.toPath());
            String font = new File(files, "runtime/apps/kilix-95/assets/fonts/DejaVuSans.ttf").getAbsolutePath();
            String script = "import json,os,sys,pathlib,PIL; from PIL import Image,ImageDraw,ImageFont; "
                + "im=Image.new('RGB',(64,32),(12,34,56)); ImageDraw.Draw(im).text((0,0),'Pleb',font=ImageFont.truetype("
                + pythonString(font) + ",14),fill='white'); "
                + "im.save(" + pythonString(new File(files, "pillow-proof.png").getAbsolutePath()) + "); "
                + "pathlib.Path(" + pythonString(pythonProof.getAbsolutePath()) + ").write_text(json.dumps({'python':sys.version,'pillow':PIL.__version__,'pages':os.sysconf('SC_PAGE_SIZE'),'pixel':im.getpixel((63,31))}))";
            command("PLEBDROID_RUNTIME_ROOT=" + quote(new File(files, "runtime/python").getAbsolutePath())
                + " " + quote(nativeDir + "/libplebdroid_python.so") + " -c " + quote(script));
            await(() -> pythonProof.isFile(), "Standalone packaged Python/Pillow did not produce its proof");
            require(json("python-proof.json").getJSONArray("pixel").toString().equals("[12,34,56]"), "Pillow pixels are wrong");
            passed.add("standalone Python, Pillow, TrueType text and PNG run under the app UID");

            DocumentStore documents = new DocumentStore(files);
            byte[] original = "Plebdroid document π 世界\n".getBytes(StandardCharsets.UTF_8);
            String document = documents.importFile(new ByteArrayInputStream(original), "qualification-" + UUID.randomUUID() + ".txt");
            ByteArrayOutputStream exported = new ByteArrayOutputStream();
            new DocumentStore(files).exportFile(document, exported);
            require(Arrays.equals(original, exported.toByteArray()), "Document import/export changed bytes");
            passed.add("document import/export preserves bytes on Android");

            request("Desktop"); rotate(0); screenshot("plebdroid-portrait.png");
            require(desktopProcesses().equals(desktop), "Rotation restarted the desktop");
            verifyPointer("portrait");
            int fullHeight = surfaceBounds()[3];
            click("⌨");
            await(() -> surfaceBounds()[3] < fullHeight - 80, "Keyboard did not resize the desktop");
            SystemClock.sleep(1200);
            verifyPointer("keyboard");
            runOnMainSync(() -> {
                View surface = find(activity.getWindow().getDecorView(), "surface");
                ((android.view.inputmethod.InputMethodManager) activity.getSystemService(android.content.Context.INPUT_METHOD_SERVICE))
                    .hideSoftInputFromWindow(surface.getWindowToken(), 0);
            });
            await(() -> surfaceBounds()[3] >= fullHeight - 10, "Keyboard dismissal did not restore the desktop");
            rotate(1); screenshot("plebdroid-landscape.png");
            require(desktopProcesses().equals(desktop), "Landscape rotation restarted the desktop");
            verifyPointer("landscape");
            passed.add("desktop process survives portrait and landscape");

            ActivityMonitor monitor = addMonitor(PlebdroidActivity.class.getName(), null, false);
            runOnMainSync(() -> activity.recreate());
            Activity replacement = waitForMonitorWithTimeout(monitor, 10000);
            removeMonitor(monitor);
            require(replacement != null, "Activity recreation did not complete");
            activity = replacement;
            SystemClock.sleep(2000);
            require(desktopProcesses().equals(desktop), "Activity recreation restarted the desktop");
            require(new DocumentStore(files).list().contains(document), "Activity recreation lost a document");
            request("Desktop"); screenshot("plebdroid-recreated.png");
            passed.add("Activity recreation retains the desktop and user documents");
            if (desktopApps) verifyDesktopApps();
            JSONObject report = new JSONObject().put("status", "passed").put("checks", new JSONArray(passed))
                .put("desktop_pids", new JSONArray(desktop)).put("document", document).put("python", json("python-proof.json"));
            Files.write(new File(files, "plebdroid-test.json").toPath(), report.toString(2).getBytes(StandardCharsets.UTF_8));
            result.putString("result", report.toString());
        } catch (Throwable error) {
            try { screenshot("plebdroid-failure.png"); } catch (Exception ignored) { }
            result.putString("failure", error.toString());
            try {
                Files.write(new File(files, "plebdroid-test.json").toPath(), new JSONObject()
                    .put("status", "failed").put("checks", new JSONArray(passed)).put("failure", error.toString())
                    .toString(2).getBytes(StandardCharsets.UTF_8));
            } catch (Exception ignored) { }
        } finally {
            try {
                String restore = originalRotation.startsWith("lock") && originalRotation.matches("lock [0-3]")
                    ? originalRotation : "free";
                shell("wm user-rotation " + restore);
            } catch (Exception ignored) { }
        }
        finish(result.containsKey("failure") ? Activity.RESULT_CANCELED : Activity.RESULT_OK, result);
    }

    private boolean backgroundServiceRunning() {
        android.app.ActivityManager manager = getTargetContext().getSystemService(android.app.ActivityManager.class);
        for (android.app.ActivityManager.RunningServiceInfo info : manager.getRunningServices(50)) {
            if (SessionService.class.getName().equals(info.service.getClassName()) && info.foreground) return true;
        }
        return false;
    }

    private void verifySessionService(List<Integer> desktop) {
        boolean previous = SessionService.isEnabled(getTargetContext());
        try {
            runOnMainSync(() -> SessionService.enable(activity));
            await(this::backgroundServiceRunning, "Session service did not enter foreground");
            runOnMainSync(() -> SessionService.disable(activity));
            await(() -> !backgroundServiceRunning(), "Session service did not stop");
            require(!SessionService.isEnabled(activity), "Disable did not persist the user's choice");
            // A quick toggle must also satisfy Android's requirement to enter
            // foreground before cancelling a queued startForegroundService.
            runOnMainSync(() -> { SessionService.enable(activity); SessionService.disable(activity); });
            SystemClock.sleep(6000);
            require(!backgroundServiceRunning() && !SessionService.isEnabled(activity), "Queued service cancellation failed");
            require(desktopProcesses().equals(desktop), "Background toggle interrupted desktop sessions");
            passed.add("foreground service starts, stops and handles a quick toggle without restarting sessions");
        } finally {
            runOnMainSync(() -> {
                if (previous) SessionService.enable(activity); else SessionService.disable(activity);
            });
        }
    }

    private void runDebianChecks() {
        Bundle result = new Bundle();
        JSONObject report = new JSONObject();
        boolean previousBackground = SessionService.isEnabled(getTargetContext());
        try {
            activity = startActivitySync(new Intent(getTargetContext(), PlebdroidActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            await(() -> new File(files, "engine-ready").isFile(), "Engine did not start", 240000);
            await(() -> desktopProcesses().size() == 1, "Desktop did not start");
            File rootfs = new File(files, "debian/rootfs");
            boolean installedBefore = rootfs.isDirectory();
            String previousInstall = read(new File(files, "debian/installed.json"));
            report.put("installed_before", installedBefore)
                .put("hello_before", new File(rootfs, "usr/bin/hello").isFile());
            request("Debian");
            await(this::backgroundServiceRunning, "Debian did not enable its foreground service");
            passed.add("Debian button opens a terminal and enables the foreground session service");
            if (!android.os.Build.SUPPORTED_ABIS[0].equals("arm64-v8a")) {
                await(() -> read(new File(files, "debian/last-error.txt")).contains("ARM translation"),
                    "Translation limitation was not explained");
                require(!rootfs.exists(), "Unsupported device installed a rootfs");
                require(applicationProcesses("debian").size() == 1, "The explanation tab vanished");
                screenshot("plebdroid-debian-unsupported.png");
                passed.add("ARM translation gets a visible explanation before installation");
            } else {
                await(() -> rootfs.isDirectory() && new File(files, "debian/installed.json").isFile(),
                    "Debian installation did not finish: " + read(new File(files, "debian/last-error.txt")), 180000);
                require(applicationProcesses("debian").size() == 1, "Debian launcher exited");
                if (installedBefore) {
                    require(read(new File(files, "debian/installed.json")).equals(previousInstall), "Opening Debian replaced its existing install record");
                    passed.add("existing Debian rootfs and install record are preserved");
                } else passed.add("packaged Debian rootfs installs under the real Android app UID");
                connectInput(); SystemClock.sleep(1500);
                String prefix = "debian-qualification-" + UUID.randomUUID();
                File documents = new File(files, "home/Documents");
                File log = new File(documents, prefix + ".log");
                File exit = new File(documents, prefix + ".exit");
                File script = new File(rootfs, "root/" + prefix + ".sh");
                String network = aptProxy.isEmpty() ? "" : " -o Acquire::http::Proxy=" + quote(aptProxy)
                    + " -o Acquire::https::Proxy=" + quote(aptProxy)
                    + " -o Acquire::Connect::AddrConfig=false";
                String content = "set -eu\nuname -m\ndpkg --print-architecture\nid\ncat /etc/resolv.conf\n"
                    + "apt-get" + network + " -o APT::Update::Error-Mode=any -o Acquire::Languages=none -o Acquire::Retries=1 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update\n"
                    + "DEBIAN_FRONTEND=noninteractive apt-get" + network + " -y install hello\nhello\n"
                    + "printf 'Debian shared file\\n' > /root/Documents/" + prefix + ".txt\n"
                    + "ln /root/Documents/" + prefix + ".txt /root/Documents/" + prefix + "-link.txt\n"
                    + "ps -p $$ -o comm=\n";
                Files.write(script.toPath(), content.getBytes(StandardCharsets.UTF_8));
                command("bash /root/" + prefix + ".sh > /root/Documents/" + prefix + ".log 2>&1; "
                    + "printf '%s' \"$?\" > /root/Documents/" + prefix + ".exit");
                await(() -> exit.isFile(), "Debian package check timed out", 900000);
                require(read(exit).equals("0"), "Debian check failed: " + read(log));
                require(read(log).contains("Hello, world!"), "Installed Debian program did not run");
                passed.add("native ARM64 Debian runs apt update, installs hello and executes it");
                DocumentStore store = new DocumentStore(files);
                String alias = prefix + "-link.txt";
                require(store.list().contains(alias), "Debian hard-link alias is not exportable");
                ByteArrayOutputStream exported = new ByteArrayOutputStream();
                store.exportFile(alias, exported);
                require(exported.toString("UTF-8").equals("Debian shared file\n"), "Debian alias export changed bytes");
                String imported = store.importFile(new ByteArrayInputStream("from Android".getBytes(StandardCharsets.UTF_8)), prefix + "-import.txt");
                passed.add("Debian Documents and emulated hard links export through Android");
                command("exit");
                await(() -> applicationProcesses("debian").isEmpty(), "Debian exit did not close its launcher");
                request("Debian"); connectInput(); SystemClock.sleep(1500);
                File persisted = new File(documents, prefix + "-persist.txt");
                command("test -x /usr/bin/hello && test \"$(cat /root/Documents/" + imported
                    + ")\" = 'from Android' && printf 'persisted' > /root/Documents/" + persisted.getName());
                await(() -> read(persisted).equals("persisted"), "A new Debian session lost the installed package or shared file");
                passed.add("a second Debian session keeps apt packages and reads Android-imported documents");
                File background = new File(documents, prefix + "-background.txt");
                command("(sleep 3; printf 'background' > /root/Documents/" + background.getName() + ") &");
                runOnMainSync(() -> activity.moveTaskToBack(true));
                await(() -> read(background).equals("background"), "Background Debian child did not complete");
                require(backgroundServiceRunning(), "Foreground session service stopped in the background");
                shell("am start --activity-reorder-to-front -n io.github.itsmygithubacct.plebdroid/.PlebdroidActivity");
                SystemClock.sleep(1000);
                passed.add("Debian child completes while the Activity is backgrounded and service is foreground");
                screenshot("plebdroid-debian.png");
                report.put("log", log.getName()).put("install", json("debian/installed.json"))
                    .put("apt_proxy", aptProxy);
            }
            report.put("status", "passed").put("checks", new JSONArray(passed));
            result.putString("result", report.toString());
        } catch (Throwable error) {
            try { screenshot("plebdroid-debian-failure.png"); } catch (Exception ignored) { }
            result.putString("failure", error.toString());
            try { report.put("status", "failed").put("checks", new JSONArray(passed)).put("failure", error.toString()); }
            catch (Exception ignored) { }
        } finally {
            if (!previousBackground) runOnMainSync(() -> SessionService.disable(getTargetContext()));
            try { Files.write(new File(files, "plebdroid-debian-test.json").toPath(), report.toString(2).getBytes(StandardCharsets.UTF_8)); }
            catch (Exception ignored) { }
        }
        finish(result.containsKey("failure") ? Activity.RESULT_CANCELED : Activity.RESULT_OK, result);
    }

    private void key(int key, int modifiers) {
        runOnMainSync(() -> {
            long now = SystemClock.uptimeMillis();
            input.sendKeyEvent(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, key, 0, modifiers));
            input.sendKeyEvent(new KeyEvent(now, now, KeyEvent.ACTION_UP, key, 0, modifiers));
        });
    }

    private void quickLaunch(int index) throws Exception {
        int[] bounds = surfaceBounds();
        Bitmap bitmap = capture();
        int lastTeal = -1;
        try {
            for (int y = bounds[1] + bounds[3] - 1; y >= bounds[1]; y--) {
                int color = bitmap.getPixel(bounds[0] + bounds[2] - 12, y);
                if (Color.red(color) < 12 && Math.abs(Color.green(color) - 128) < 12
                        && Math.abs(Color.blue(color) - 128) < 12) { lastTeal = y; break; }
            }
        } finally { bitmap.recycle(); }
        require(lastTeal > bounds[1], "Desktop taskbar was not visible");
        // Default Android desktop: 44 physical pixels per 32px icon; these
        // are the centers of its existing 23px quick-launch controls.
        double scale = Math.max(1.375, Math.max(bounds[2], bounds[3]) / 1280.0);
        tap(bounds[0] + 4 + (int)Math.round((68 + index * 23 + 11) * scale),
            lastTeal + (int)Math.round(14 * scale));
        SystemClock.sleep(800);
    }

    private void verifyDesktopApps() throws Exception {
        rotate(0);
        quickLaunch(1); connectInput(); click("⌨"); SystemClock.sleep(800);
        runOnMainSync(() -> require(input.commitText("[", 1), "Bracket input rejected"));
        SystemClock.sleep(600); screenshot("plebdroid-notepad-bracket.png");
        runOnMainSync(() -> require(input.commitText("hé π 世界\nline two\tend", 1), "Notepad input rejected"));
        SystemClock.sleep(500);
        key(KeyEvent.KEYCODE_S, KeyEvent.META_CTRL_ON);
        SystemClock.sleep(800);
        String name = "notepad-qualification-" + UUID.randomUUID() + ".txt";
        runOnMainSync(() -> input.commitText(name, 1));
        key(KeyEvent.KEYCODE_ENTER, 0);
        File saved = new File(files, "home/Documents/" + name);
        await(() -> read(saved).equals("[hé π 世界\nline two    end"), "Notepad did not save the typed Unicode, Enter and Tab");
        screenshot("plebdroid-notepad-saved.png");
        passed.add("touch-opened Notepad saves soft-keyboard Unicode, brackets, Enter and Tab into Documents");
        quickLaunch(3); connectInput();
        runOnMainSync(() -> input.commitText("12+30=", 1));
        SystemClock.sleep(800); screenshot("plebdroid-calculator.png");
        passed.add("Calculator soft-keyboard result captured for visual verification (expected 42)");
    }
}
