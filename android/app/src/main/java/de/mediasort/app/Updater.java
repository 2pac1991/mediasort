package de.mediasort.app;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Selbst-Update: fragt beim Server nach, welche App-Version im Docker-Image steckt, lädt die neue APK
 * und installiert sie über den PackageInstaller des Systems.
 *
 * Android fragt beim allerersten Update einmal nach (die App wurde ja über den Browser installiert).
 * Ab Android 12 laufen weitere Updates, die Saugmeister selbst installiert hat, meist ohne Rückfrage durch.
 */
class Updater {
    interface Listener {
        void onUpdate(int code, String name);   // neue Version verfügbar
        void onProgress(String text);           // Fortschritt beim Laden
        void onFailed(String text);
    }

    static final long CHECK_EVERY_MS = 30 * 60 * 1000L;
    private static long lastCheck = 0;
    private static volatile boolean busy = false;

    private final Activity act;
    private final Listener listener;
    private final Handler ui = new Handler(Looper.getMainLooper());
    int availableCode = 0;
    String availableName = "";

    Updater(Activity act, Listener listener) {
        this.act = act;
        this.listener = listener;
    }

    static int installedCode(Context ctx) {
        try {
            PackageInfo pi = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return (int) (Build.VERSION.SDK_INT >= 28 ? pi.getLongVersionCode() : pi.versionCode);
        } catch (Exception e) {
            return 0;
        }
    }

    /** Anmelde-Cookie der Weboberfläche mitschicken, die APK liegt hinter der Anmeldung. */
    private HttpURLConnection open(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(30000);
        String cookie = CookieManager.getInstance().getCookie(url);
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        c.setRequestProperty("User-Agent", "MediasortApp/updater");
        return c;
    }

    /** Im Hintergrund nachsehen, ob es eine neuere Version gibt (höchstens alle 30 Minuten, außer force). */
    void check(boolean force) {
        long now = System.currentTimeMillis();
        if (busy || (!force && now - lastCheck < CHECK_EVERY_MS)) return;
        lastCheck = now;
        String base = MainActivity.baseUrl(act);
        new Thread(() -> {
            try {
                HttpURLConnection c = open(base + "/api/app/version");
                if (c.getResponseCode() != 200) return;
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                try (InputStream in = c.getInputStream()) {
                    byte[] b = new byte[4096];
                    int n;
                    while ((n = in.read(b)) > 0) buf.write(b, 0, n);
                }
                JSONObject j = new JSONObject(buf.toString("UTF-8"));
                int code = j.optInt("versionCode", 0);
                String name = j.optString("versionName", "");
                if (code > installedCode(act)) {
                    availableCode = code;
                    availableName = name;
                    ui.post(() -> listener.onUpdate(code, name));
                }
            } catch (Exception ignored) {
                // Server nicht erreichbar: dann eben beim nächsten Mal
            }
        }, "update-check").start();
    }

    /** Darf die App Pakete installieren? Sonst die passende Einstellungsseite öffnen. */
    boolean ensurePermission() {
        if (act.getPackageManager().canRequestPackageInstalls()) return true;
        try {
            act.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + act.getPackageName())));
        } catch (Exception e) {
            act.startActivity(new Intent(Settings.ACTION_SECURITY_SETTINGS));
        }
        return false;
    }

    /** APK laden und dem System zum Installieren geben. */
    void install() {
        if (busy) return;
        if (!ensurePermission()) {
            listener.onFailed("Bitte „Aus dieser Quelle zulassen“ einschalten und dann nochmal auf Update tippen");
            return;
        }
        busy = true;
        String base = MainActivity.baseUrl(act);
        listener.onProgress("Update wird geladen …");
        new Thread(() -> {
            PackageInstaller.Session session = null;
            try {
                HttpURLConnection c = open(base + "/mediasort.apk");
                if (c.getResponseCode() == 401) throw new Exception("Bitte zuerst in Saugmeister anmelden");
                if (c.getResponseCode() != 200) throw new Exception("Download fehlgeschlagen (" + c.getResponseCode() + ")");
                long total = c.getContentLengthLong();

                PackageInstaller pi = act.getPackageManager().getPackageInstaller();
                PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                        PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                params.setAppPackageName(act.getPackageName());
                if (total > 0) params.setSize(total);
                if (Build.VERSION.SDK_INT >= 31) {
                    // Ohne Rückfrage, wenn Saugmeister die App schon selbst installiert hat
                    params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
                }
                int id = pi.createSession(params);
                session = pi.openSession(id);

                try (InputStream in = c.getInputStream(); OutputStream out = session.openWrite("saugmeister.apk", 0, total)) {
                    byte[] b = new byte[64 * 1024];
                    long done = 0;
                    int n, lastPct = -1;
                    while ((n = in.read(b)) > 0) {
                        out.write(b, 0, n);
                        done += n;
                        if (total > 0) {
                            int pct = (int) (done * 100 / total);
                            if (pct != lastPct) {
                                lastPct = pct;
                                ui.post(() -> listener.onProgress("Update wird geladen … " + pct + " %"));
                            }
                        }
                    }
                    session.fsync(out);
                }

                Intent result = new Intent(act, InstallReceiver.class);
                int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
                PendingIntent pending = PendingIntent.getBroadcast(act, id, result, flags);
                ui.post(() -> listener.onProgress("Wird installiert …"));
                session.commit(pending.getIntentSender());
                session.close();
                session = null;
            } catch (Exception e) {
                if (session != null) session.abandon();
                String msg = e.getMessage() != null ? e.getMessage() : "Update fehlgeschlagen";
                ui.post(() -> listener.onFailed(msg));
            } finally {
                busy = false;
            }
        }, "update-install").start();
    }
}
