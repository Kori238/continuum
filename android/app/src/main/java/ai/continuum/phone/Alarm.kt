package ai.continuum.phone

import org.json.JSONArray
import org.json.JSONObject
import java.util.Calendar
import java.util.Locale

/**
 * One alarm, and the arithmetic for when it next rings.
 *
 * The wire shape (what the desktop sends and stores) uses the SAME day
 * convention as the routine store on the desktop — Mon=0 … Sun=6 — so a
 * recurring alarm reads consistently on both sides. Android's Calendar uses
 * Sun=1 … Sat=7 internally; the conversion is confined to this file.
 *
 * `days` empty means a **one-off**: it fires at the next occurrence of
 * hour:minute and is then removed. A non-empty `days` is a weekly recurrence
 * that carries on until cancelled.
 */
data class Alarm(
    val id: String,
    val hour: Int,
    val minute: Int,
    val label: String,
    val days: List<Int>,       // Mon=0 … Sun=6; empty = one-off
    val enabled: Boolean,
) {
    val oneOff: Boolean get() = days.isEmpty()

    fun toJson(now: Long = System.currentTimeMillis()): JSONObject = JSONObject().apply {
        put("id", id)
        put("hour", hour)
        put("minute", minute)
        put("label", label)
        put("days", JSONArray(days))
        put("enabled", enabled)
        val next = nextTrigger(now)
        if (next != null) put("next_fire_ms", next)
    }

    /**
     * Epoch millis of the next fire strictly after [now], or null if disabled.
     * Walks the next eight days so a full weekly pattern is always covered, and
     * skips today's slot once it has passed (offset 0 must be in the future).
     */
    fun nextTrigger(now: Long = System.currentTimeMillis()): Long? {
        if (!enabled) return null
        val daySet = days.toSet()
        for (offset in 0..7) {
            val cal = Calendar.getInstance().apply {
                timeInMillis = now
                add(Calendar.DAY_OF_YEAR, offset)
                set(Calendar.HOUR_OF_DAY, hour)
                set(Calendar.MINUTE, minute)
                set(Calendar.SECOND, 0)
                set(Calendar.MILLISECOND, 0)
            }
            if (cal.timeInMillis <= now) continue          // slot already gone
            val monZero = (cal.get(Calendar.DAY_OF_WEEK) + 5) % 7   // Sun=1..Sat=7 -> Mon=0..Sun=6
            if (oneOff || monZero in daySet) return cal.timeInMillis
        }
        return null
    }

    companion object {
        fun fromJson(o: JSONObject): Alarm {
            val daysArr = o.optJSONArray("days") ?: JSONArray()
            val days = ArrayList<Int>(daysArr.length())
            for (i in 0 until daysArr.length()) {
                val d = daysArr.optInt(i, -1)
                if (d in 0..6) days.add(d)
            }
            return Alarm(
                id = o.optString("id").ifBlank { newId() },
                hour = o.optInt("hour", 0).coerceIn(0, 23),
                minute = o.optInt("minute", 0).coerceIn(0, 59),
                label = o.optString("label").ifBlank { "Alarm" },
                days = days.distinct().sorted(),
                enabled = if (o.has("enabled")) o.optBoolean("enabled", true) else true,
            )
        }

        fun listFromJson(arr: JSONArray): List<Alarm> {
            val out = ArrayList<Alarm>(arr.length())
            for (i in 0 until arr.length()) {
                arr.optJSONObject(i)?.let { out.add(fromJson(it)) }
            }
            return out
        }

        fun listToJson(alarms: List<Alarm>, now: Long = System.currentTimeMillis()): JSONArray {
            val arr = JSONArray()
            alarms.forEach { arr.put(it.toJson(now)) }
            return arr
        }

        /** Short, collision-resistant id used as the AlarmManager request-code seed. */
        fun newId(): String =
            java.util.UUID.randomUUID().toString().replace("-", "").take(10)
                .lowercase(Locale.ROOT)
    }
}
