package de.mediasort.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Nach dem Neustart des Handys ist Click'n'Load gleich wieder bereit. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) CnlService.start(ctx);
    }
}
