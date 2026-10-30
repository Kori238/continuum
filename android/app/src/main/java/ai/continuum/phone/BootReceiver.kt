package ai.continuum.phone

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Bring the link back after a reboot, without which the phone would silently
 * drop off Continuum's fabric until someone thought to open the app.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        // BOOT_COMPLETED is one of the few exemptions from Android 12's ban on
        // starting a foreground service from the background — but only if the
        // user hasn't force-stopped us, so failure here is normal and quiet.
        runCatching { ctx.startForegroundService(Intent(ctx, LinkService::class.java)) }
            .onFailure { Log.w("ContinuumLink", "boot start refused: ${it.message}") }
        // AlarmManager alarms don't survive a reboot — re-arm the stored set so a
        // wake-up alarm still rings the morning after a restart or a battery-flat.
        runCatching { AlarmScheduler.rearmAll(ctx) }
            .onFailure { Log.w("ContinuumAlarm", "boot re-arm failed: ${it.message}") }
    }
}
