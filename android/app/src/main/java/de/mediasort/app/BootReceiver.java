package de.mediasort.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Nach dem Neustart des Handys oder einem Update ist Click'n'Load gleich wieder bereit. */
public class BootReceiver extends BroadcastReceiver {
    static final String CH_UPDATE = "update";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        String a = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(a)) {
            CnlService.start(ctx);
        } else if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
            CnlService.start(ctx);
            notifyUpdated(ctx);
        }
    }

    /** Kurzer Hinweis nach dem Update, Antippen öffnet die App wieder. */
    private static void notifyUpdated(Context ctx) {
        try {
            NotificationManager nm = ctx.getSystemService(NotificationManager.class);
            nm.createNotificationChannel(new NotificationChannel(CH_UPDATE, "App-Updates", NotificationManager.IMPORTANCE_DEFAULT));
            Intent open = new Intent(ctx, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            PendingIntent pi = PendingIntent.getActivity(ctx, 7, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            String ver = "";
            try {
                ver = " auf " + ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0).versionName;
            } catch (Exception ignored) {
            }
            Notification n = new Notification.Builder(ctx, CH_UPDATE)
                    .setSmallIcon(R.drawable.ic_stat)
                    .setContentTitle("Saugmeister aktualisiert")
                    .setContentText("Update" + ver + " installiert – antippen zum Öffnen")
                    .setContentIntent(pi)
                    .setAutoCancel(true)
                    .build();
            nm.notify(7, n);
        } catch (Exception ignored) {
            // ohne Benachrichtigungs-Erlaubnis eben ohne Hinweis
        }
    }
}
