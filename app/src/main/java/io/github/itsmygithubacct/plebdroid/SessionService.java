// SPDX-License-Identifier: GPL-3.0-only
package io.github.itsmygithubacct.plebdroid;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

/**
 * Optional foreground importance for user-started terminal sessions, which
 * run in this same process. It is started only from a visible Activity and is
 * never restarted automatically. It does not protect sessions from force-stop,
 * phantom-process limits or the user dismissing the app.
 */
public final class SessionService extends Service {
    static final String PREFERENCES = "background-sessions", ENABLED = "enabled";
    private static final String TAG = "PlebdroidSessions", CHANNEL = "background-sessions";
    private static final String ACTION_START = "io.github.itsmygithubacct.plebdroid.action.BACKGROUND_SESSIONS";
    private static final String ACTION_DISABLE = "io.github.itsmygithubacct.plebdroid.action.DISABLE_BACKGROUND_SESSIONS";
    private static final int NOTIFICATION_ID = 1;
    // Main-thread state shared with the static helpers; the service always runs in this process.
    private static volatile boolean wanted, created;

    /** The user's saved choice. It is not proof that the service is running now. */
    public static boolean isEnabled(Context context) {
        return preferences(context).getBoolean(ENABLED, false);
    }

    /**
     * Starts background mode. Call from a visible Activity. Throws when Android
     * refuses the start (for example ForegroundServiceStartNotAllowedException);
     * the preference is then left unchanged.
     */
    public static void enable(Context context) {
        Context app = context.getApplicationContext();
        boolean previous = wanted;
        wanted = true;
        try {
            ComponentName started = app.startForegroundService(
                    new Intent(app, SessionService.class).setAction(ACTION_START));
            if (started == null) throw new IllegalStateException("SessionService is not declared in the manifest");
        } catch (RuntimeException error) {
            wanted = previous;
            throw error;
        }
        preferences(app).edit().putBoolean(ENABLED, true).apply();
    }

    /** Stops background mode and saves the choice. Sessions keep running while the app is open. */
    public static void disable(Context context) {
        Context app = context.getApplicationContext();
        wanted = false;
        preferences(app).edit().putBoolean(ENABLED, false).apply();
        // Before onCreate has called startForeground, stopService would make
        // Android fail the pending foreground start. That start sees wanted
        // is false in onStartCommand and stops itself instead.
        if (created) app.stopService(new Intent(app, SessionService.class));
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
    }

    @Override public void onCreate() {
        super.onCreate();
        if (enterForeground()) created = true;
        else stopSelf();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_DISABLE.equals(action)) {
            wanted = false;
            preferences(this).edit().putBoolean(ENABLED, false).apply();
            finish();
        } else if (!ACTION_START.equals(action) || !wanted || !enterForeground()) {
            // A null or unknown intent, a start the user already cancelled, or a refused foreground start.
            finish();
        }
        return START_NOT_STICKY;
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public void onTimeout(int startId) { finish(); }

    @Override public void onTimeout(int startId, int foregroundServiceType) { finish(); }

    @Override public void onDestroy() {
        // Android may destroy the service without the user choosing to; the saved choice stays.
        created = false;
        stopForeground(STOP_FOREGROUND_REMOVE);
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) manager.cancel(NOTIFICATION_ID);
        super.onDestroy();
    }

    private void finish() {
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    private boolean enterForeground() {
        try {
            Notification notification = notification();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            else startForeground(NOTIFICATION_ID, notification);
            return true;
        } catch (RuntimeException error) {
            Log.w(TAG, "Background mode was not allowed", error);
            return false;
        }
    }

    private Notification notification() {
        NotificationManager manager = getSystemService(NotificationManager.class);
        NotificationChannel channel = new NotificationChannel(CHANNEL, "Background sessions",
                NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Shown while terminal sessions can continue in the background");
        channel.setSound(null, null);
        channel.enableVibration(false);
        channel.setShowBadge(false);
        manager.createNotificationChannel(channel);

        // ACTION_MAIN with the launcher category brings back the existing task
        // instead of starting a second PlebdroidActivity.
        Intent open = new Intent(this, PlebdroidActivity.class).setAction(Intent.ACTION_MAIN)
                .addCategory(Intent.CATEGORY_LAUNCHER)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        int flags = PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT;
        PendingIntent openApp = PendingIntent.getActivity(this, 0, open, flags);
        PendingIntent disable = PendingIntent.getService(this, 1,
                new Intent(this, SessionService.class).setAction(ACTION_DISABLE), flags);
        Icon icon = Icon.createWithResource(this, R.drawable.session_notification);

        Notification.Builder builder = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.session_notification)
                .setContentTitle("Plebdroid")
                .setContentText("Terminal sessions can continue in the background")
                .setContentIntent(openApp)
                .addAction(new Notification.Action.Builder(icon, "Disable background mode", disable).build())
                .setCategory(Notification.CATEGORY_SERVICE)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        return builder.build();
    }
}
