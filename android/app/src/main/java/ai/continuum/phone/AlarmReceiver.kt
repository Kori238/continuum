package ai.continuum.phone

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * The AlarmManager alarm fires here. We don't ring on the receiver thread —
 * that has ~10 seconds before Android kills it — we hand straight off to
 * [AlarmService], which holds the wake lock, plays the sound and shows the UI.
 *
 * A firing exact alarm grants a short background-start allowlist, which is what
 * lets a receiver launch a foreground service (and the service's full-screen
 * intent launch an activity) even from deep doze.
 */
class AlarmReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != AlarmScheduler.ACTION_FIRE) return
        val id = intent.getStringExtra(AlarmScheduler.EXTRA_ID) ?: return
        val snooze = intent.getBooleanExtra(AlarmScheduler.EXTRA_SNOOZE, false)
        val label = intent.getStringExtra(AlarmScheduler.EXTRA_LABEL)
            ?: AlarmScheduler.labelFor(ctx, id)
        Log.i("ContinuumAlarm", "fired $id snooze=$snooze")

        // A natural fire retires a one-off and re-arms a recurring alarm's next
        // occurrence. A snooze fire must NOT touch the schedule — it's transient.
        if (!snooze) {
            runCatching { AlarmScheduler.onFired(ctx, id) }
        }

        val svc = Intent(ctx, AlarmService::class.java).apply {
            action = AlarmService.ACTION_RING
            putExtra(AlarmScheduler.EXTRA_ID, id)
            putExtra(AlarmScheduler.EXTRA_LABEL, label)
        }
        runCatching { ContextCompat.startForegroundService(ctx, svc) }
            .onFailure { Log.w("ContinuumAlarm", "could not start alarm service: ${it.message}") }
    }
}
