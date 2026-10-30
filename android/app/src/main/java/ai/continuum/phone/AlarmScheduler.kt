package ai.continuum.phone

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.util.Log
import org.json.JSONArray

/**
 * Owns the alarm set on the phone and arms it with [AlarmManager].
 *
 * Why the phone, not the desktop: this is a *wake-up* alarm, so it has to ring
 * even when the laptop is asleep and the socket dropped overnight. The desktop
 * is only a remote control that syncs the set down; the phone schedules and
 * rings on its own, survives reboots (see [BootReceiver]) and doze
 * (`setAlarmClock` is exact and allow-while-idle by contract).
 *
 * The list of truth is [Prefs.alarms]. Arming is derived from it and fully
 * idempotent: [rearmAll] cancels every alarm's PendingIntent and re-arms the
 * enabled ones, so any mutation just re-derives the AlarmManager state. Each
 * alarm gets a stable request code from its id, so re-arming replaces rather
 * than duplicates.
 */
object AlarmScheduler {

    private const val TAG = "ContinuumAlarm"
    const val ACTION_FIRE = "ai.continuum.phone.ALARM_FIRE"
    const val EXTRA_ID = "alarm_id"
    const val EXTRA_LABEL = "alarm_label"
    const val EXTRA_SNOOZE = "alarm_snooze"

    const val SNOOZE_MINUTES = 9

    // ---- reading / mutating the set --------------------------------------

    fun list(ctx: Context): List<Alarm> = Alarm.listFromJson(Prefs.alarms(ctx))

    fun listJson(ctx: Context): JSONArray = Alarm.listToJson(list(ctx))

    private fun save(ctx: Context, alarms: List<Alarm>) =
        Prefs.setAlarms(ctx, Alarm.listToJson(alarms))

    /** Add or replace an alarm (matched by id), then re-arm everything. */
    fun upsert(ctx: Context, alarm: Alarm): Alarm {
        val cur = list(ctx).filter { it.id != alarm.id }
        save(ctx, cur + alarm)
        rearmAll(ctx)
        return alarm
    }

    /** Drop an alarm by id. Returns true if one was actually removed. */
    fun remove(ctx: Context, id: String): Boolean {
        val cur = list(ctx)
        if (cur.none { it.id == id }) return false
        disarm(ctx, id, snooze = false)
        disarm(ctx, id, snooze = true)
        save(ctx, cur.filter { it.id != id })
        rearmAll(ctx)
        return true
    }

    /** A scheduled alarm just fired: retire it if one-off, then re-arm the rest
     *  (which re-arms a recurring alarm's *next* occurrence). */
    fun onFired(ctx: Context, id: String) {
        val alarm = list(ctx).firstOrNull { it.id == id }
        if (alarm != null && alarm.oneOff) {
            save(ctx, list(ctx).filter { it.id != id })
        }
        rearmAll(ctx)
    }

    fun labelFor(ctx: Context, id: String): String =
        list(ctx).firstOrNull { it.id == id }?.label ?: "Alarm"

    // ---- arming ----------------------------------------------------------

    fun rearmAll(ctx: Context) {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        for (a in list(ctx)) {
            disarm(ctx, a.id, snooze = false)
            val next = a.nextTrigger()
            if (next != null) armAt(ctx, am, a.id, a.label, next, snooze = false)
        }
    }

    /** Ring [id] again after a snooze — a one-shot that does NOT disturb the
     *  alarm's own recurring schedule (separate request code). */
    fun snooze(ctx: Context, id: String, minutes: Int = SNOOZE_MINUTES) {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val at = System.currentTimeMillis() + minutes * 60_000L
        armAt(ctx, am, id, labelFor(ctx, id), at, snooze = true)
    }

    private fun armAt(ctx: Context, am: AlarmManager, id: String, label: String,
                      whenMs: Long, snooze: Boolean) {
        val show = PendingIntent.getActivity(
            ctx, requestCode(id, snooze) xor 0x5A5A,
            Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val info = AlarmManager.AlarmClockInfo(whenMs, show)
        am.setAlarmClock(info, firePending(ctx, id, label, snooze))
        Log.i(TAG, "armed ${if (snooze) "snooze " else ""}$id at $whenMs")
    }

    private fun disarm(ctx: Context, id: String, snooze: Boolean) {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        am.cancel(firePending(ctx, id, "", snooze))
    }

    private fun firePending(ctx: Context, id: String, label: String, snooze: Boolean): PendingIntent {
        val intent = Intent(ctx, AlarmReceiver::class.java).apply {
            action = ACTION_FIRE
            putExtra(EXTRA_ID, id)
            putExtra(EXTRA_LABEL, label)
            putExtra(EXTRA_SNOOZE, snooze)
        }
        return PendingIntent.getBroadcast(
            ctx, requestCode(id, snooze), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun requestCode(id: String, snooze: Boolean): Int =
        (if (snooze) "snooze:$id" else id).hashCode()
}
