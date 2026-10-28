// SPDX-License-Identifier: GPL-3.0-only
package io.github.itsmygithubacct.plebdroid;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import org.json.JSONArray;
import org.json.JSONObject;

/** Framework-owned APK paths and current network DNS for the Debian launcher. */
final class DebianSource {
    static synchronized void refresh(Context context) throws Exception {
        File directory = new File(context.getFilesDir().getCanonicalFile(), "debian");
        if (Files.exists(directory.toPath(), LinkOption.NOFOLLOW_LINKS)
                && !directory.getCanonicalFile().equals(directory))
            throw new IOException("Debian storage must be a local directory");
        Files.createDirectories(directory.toPath());
        JSONArray dns = new JSONArray();
        ConnectivityManager manager = context.getSystemService(ConnectivityManager.class);
        Network active = manager == null ? null : manager.getActiveNetwork();
        LinkProperties link = active == null ? null : manager.getLinkProperties(active);
        if (link != null) for (InetAddress address : link.getDnsServers()) dns.put(address.getHostAddress());
        JSONObject source = new JSONObject().put("format", 1)
            .put("apk", context.getApplicationInfo().sourceDir)
            .put("native_dir", context.getApplicationInfo().nativeLibraryDir)
            .put("dns_servers", dns);
        AtomicFile file = new AtomicFile(new File(directory, "source.json"));
        FileOutputStream stream = null;
        try {
            stream = file.startWrite();
            stream.write(source.toString().getBytes(StandardCharsets.UTF_8));
            file.finishWrite(stream);
        } catch (Exception error) {
            if (stream != null) file.failWrite(stream);
            throw error;
        }
    }
}
