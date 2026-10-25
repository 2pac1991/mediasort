package de.mediasort.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.widget.Toast;

/** Rückmeldung des Systems zur Installation: Bestätigung anzeigen oder Fehler melden. */
public class InstallReceiver extends BroadcastReceiver {
    @Override
    @SuppressWarnings("deprecation")
    public void onReceive(Context ctx, Intent intent) {
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        switch (status) {
            case PackageInstaller.STATUS_PENDING_USER_ACTION:
                // Android möchte, dass man das Update einmal bestätigt
                Intent confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT);
                if (confirm != null) {
                    confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    try {
                        ctx.startActivity(confirm);
                    } catch (Exception e) {
                        Toast.makeText(ctx, "Update konnte nicht gestartet werden", Toast.LENGTH_LONG).show();
                    }
                }
                break;
            case PackageInstaller.STATUS_SUCCESS:
                break; // die App wird gleich neu gestartet, siehe BootReceiver
            case PackageInstaller.STATUS_FAILURE_ABORTED:
                Toast.makeText(ctx, "Update abgebrochen", Toast.LENGTH_SHORT).show();
                break;
            case PackageInstaller.STATUS_FAILURE_CONFLICT:
            case PackageInstaller.STATUS_FAILURE_INCOMPATIBLE:
                Toast.makeText(ctx, "Update passt nicht zur installierten App (anderer Signaturschlüssel?). "
                        + "Dann einmal deinstallieren und neu installieren.", Toast.LENGTH_LONG).show();
                break;
            default:
                String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                Toast.makeText(ctx, "Update fehlgeschlagen" + (msg != null ? ": " + msg : ""), Toast.LENGTH_LONG).show();
        }
    }
}
