// SPDX-License-Identifier: GPL-3.0-only
package io.github.itsmygithubacct.plebdroid;

import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.Toast;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.List;
import io.github.itsmygithubacct.kilix.android.KilixActivity;

/** Plebian-OS's desktop and local tools, hosted by the shared Kilix Android runtime. */
public final class PlebdroidActivity extends KilixActivity {
    private static final int IMPORT_DOCUMENT = 1, EXPORT_DOCUMENT = 2;
    private String pendingExport;

    @Override protected String applicationLabel() { return "Plebdroid"; }

    @Override public void onCreate(Bundle saved) {
        if (saved != null) pendingExport = saved.getString("export-document");
        super.onCreate(saved);
        try { new DocumentStore(getFilesDir()); }
        catch (Exception error) { message("Cannot prepare Documents: " + error.getMessage()); }
    }

    @Override protected void onResume() {
        super.onResume();
        try {
            DebianSource.refresh(this);
            if (SessionService.isEnabled(this)) SessionService.enable(this);
        } catch (Exception error) { message("Cannot prepare sessions: " + error.getMessage()); }
    }

    @Override protected void onSaveInstanceState(Bundle state) {
        super.onSaveInstanceState(state);
        state.putString("export-document", pendingExport);
    }

    @Override protected void populateSessionControls(LinearLayout row) {
        addButton(row, "Desktop", () -> requestApplication("kilix-95"));
        addButton(row, "Terminal", () -> requestSession("local", "", "", ""));
        addButton(row, "Debian", this::openDebian);
        addButton(row, "Files", this::filesDialog);
        addButton(row, "Host", this::connectDialog);
        addButton(row, "More", this::moreDialog);
    }

    private void openDebian() {
        try {
            DebianSource.refresh(this);
            SessionService.enable(this);
            requestApplication("debian");
        } catch (Exception error) { message("Cannot open Debian: " + error.getMessage()); }
    }

    private void backgroundDialog() {
        boolean enabled = SessionService.isEnabled(this);
        new AlertDialog.Builder(this).setTitle("Background sessions")
            .setMessage(enabled
                ? "Background mode is enabled. Disabling it lets Android suspend terminal sessions when you leave the app."
                : "Keep terminal sessions running while you use other apps. Android can still stop the app if it needs resources.")
            .setPositiveButton(enabled ? "Disable" : "Enable", (dialog, which) -> {
                try {
                    if (enabled) SessionService.disable(this);
                    else SessionService.enable(this);
                } catch (Exception error) { message(error.getMessage()); }
            })
            .setNeutralButton("Show notification", (dialog, which) -> {
                if (android.os.Build.VERSION.SDK_INT >= 33
                        && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 3);
                else message("The background-session notification is available when background mode is enabled.");
            }).setNegativeButton("Cancel", null).show();
    }

    private void addButton(LinearLayout row, String title, Runnable action) {
        Button button = new Button(this);
        button.setText(title);
        button.setAllCaps(false);
        button.setSingleLine(true);
        button.setTextSize(14);
        button.setPadding(dp(6), 0, dp(6), 0);
        button.setOnClickListener(view -> action.run());
        row.addView(button, new LinearLayout.LayoutParams(dp(90), dp(48)));
    }

    private void moreDialog() {
        String[] choices = {"Lights", "Split terminal", "Next pane or tab", "Detach host", "Background sessions", "About Plebdroid"};
        new AlertDialog.Builder(this).setTitle("Plebdroid").setItems(choices, (dialog, which) -> {
            switch (which) {
                case 0: requestApplication("kilix-lights"); break;
                case 1: requestSession("split", "", "", ""); break;
                case 2: requestMode("focus-next"); break;
                case 3: requestMode("detach"); break;
                case 4: backgroundDialog(); break;
                default: new AlertDialog.Builder(this).setTitle("Plebdroid")
                    .setMessage("Plebian-OS on Android\n\nKilix 95 desktop, local terminal, built-in desktop tools and Lights. "
                        + "Connect to a Kilix host for its remote terminal sessions.\n\n"
                        + "Your desktop files stay in this app. Use Files to import or export Documents. "
                        + "Uninstalling Plebdroid removes its private files.\n\n"
                        + "Terminal opens the Android system shell. Debian installs a private Linux userspace on first use, "
                        + "with apt and a shared Documents folder. Debian enables background mode for its sessions. "
                        + "The first installation needs about 450 MiB free. X11 applications are not included.")
                    .setPositiveButton("OK", null).show();
            }
        }).show();
    }

    private void filesDialog() {
        new AlertDialog.Builder(this).setTitle("Documents")
            .setItems(new String[]{"Import a file (up to 256 MiB)", "Export a document"}, (dialog, which) -> {
                if (which == 0) {
                    Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT)
                        .addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");
                    startActivityForResult(intent, IMPORT_DOCUMENT);
                } else selectExport();
            }).setNegativeButton("Close", null).show();
    }

    private void selectExport() {
        try {
            List<String> files = new DocumentStore(getFilesDir()).list();
            if (files.isEmpty()) { message("Save a file in Documents first."); return; }
            new AlertDialog.Builder(this).setTitle("Export from Documents")
                .setItems(files.toArray(new String[0]), (dialog, which) -> {
                    pendingExport = files.get(which);
                    Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT)
                        .addCategory(Intent.CATEGORY_OPENABLE).setType("application/octet-stream")
                        .putExtra(Intent.EXTRA_TITLE, new File(pendingExport).getName());
                    startActivityForResult(intent, EXPORT_DOCUMENT);
                }).setNegativeButton("Cancel", null).show();
        } catch (Exception error) { message(error.getMessage()); }
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != IMPORT_DOCUMENT && request != EXPORT_DOCUMENT) return;
        String export = pendingExport;
        if (request == EXPORT_DOCUMENT) pendingExport = null;
        if (result != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        new Thread(() -> {
            try {
                DocumentStore store = new DocumentStore(getFilesDir());
                if (request == IMPORT_DOCUMENT) {
                    String name = "imported-file";
                    try (Cursor cursor = getContentResolver().query(uri,
                            new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
                        if (cursor != null && cursor.moveToFirst()) name = cursor.getString(0);
                    }
                    try (InputStream in = getContentResolver().openInputStream(uri)) {
                        message("Imported to Documents: " + store.importFile(in, name));
                    }
                } else {
                    if (export == null) throw new IllegalStateException("Select the document again");
                    try (OutputStream out = getContentResolver().openOutputStream(uri, "wt")) {
                        store.exportFile(export, out);
                    }
                    message("Exported " + export);
                }
            } catch (Exception error) { message("File transfer failed: " + error.getMessage()); }
        }, "plebdroid-documents").start();
    }

    private void message(String text) {
        runOnUiThread(() -> Toast.makeText(getApplicationContext(), text, Toast.LENGTH_LONG).show());
    }
}
