package ai.continuum.phone

import android.content.Context
import android.os.Build
import org.json.JSONArray

/**
 * The two things this app has to remember: where Continuum lives, and what to
 * call this phone.
 *
 * Note what is *not* stored here: any credential. The link runs over Tailscale
 * and Continuum authenticates by tailnet identity, so the phone never holds a
 * token that could leak with the device.
 */
object Prefs {

    private const val FILE = "continuum"
    private const val KEY_HOST = "host"
    private const val KEY_NAME = "name"

    // Step-counter bookkeeping — see [stepsToday]. The hardware counter is
    // since-boot; these turn it into a per-day figure that survives reboots.
    private const val KEY_STEP_DAY = "step_day"      // the date the counters below describe
    private const val KEY_STEP_BASE = "step_base"    // raw value that marks 0 steps today
    private const val KEY_STEP_CARRY = "step_carry"  // steps banked before a mid-day reboot
    private const val KEY_STEP_LAST = "step_last"    // last raw reading, to detect a reboot

    // Health Connect scan watermark — the lower bound for the next exercise-session
    // read. See [hcSince]; the reader keeps an overlap so this can advance without
    // an exactly-once guarantee it couldn't honour across a dropped socket.
    private const val KEY_HC_SINCE = "hc_since"

    // Last routine snapshot the desktop pushed for the home-screen widget (a JSON
    // string). The widget renders from this, so it survives a reboot and shows the
    // last-known routine even while the phone is briefly offline. See RoutineWidget.
    private const val KEY_WIDGET = "widget_state"

    // The alarm set, as a JSON array string. The phone is the source of truth for
    // alarms (it rings them via AlarmManager even while the desktop is asleep);
    // the desktop just syncs this list down. See [AlarmScheduler].
    private const val KEY_ALARMS = "alarms"

    /** The desktop's tailnet address — the `tailscale serve` endpoint, not the
     *  loopback port uvicorn actually binds. The MagicDNS name rather than the
     *  100.x address on purpose: TLS is what makes this wss:// instead of a
     *  plaintext socket, and the certificate is issued for the name. */
    const val DEFAULT_HOST = "https://desktop-hc8d6c6.skate-wyvern.ts.net:8443"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun host(ctx: Context): String =
        prefs(ctx).getString(KEY_HOST, DEFAULT_HOST)?.trim().orEmpty().ifBlank { DEFAULT_HOST }

    fun setHost(ctx: Context, value: String) {
        prefs(ctx).edit().putString(KEY_HOST, value.trim()).apply()
    }

    fun deviceName(ctx: Context): String =
        prefs(ctx).getString(KEY_NAME, null)?.trim()?.ifBlank { null } ?: Build.MODEL ?: "phone"

    fun setDeviceName(ctx: Context, value: String) {
        prefs(ctx).edit().putString(KEY_NAME, value.trim()).apply()
    }

    /** True once the user has been through setup, so first launch can send them
     *  there instead of dropping them on a connection error. */
    fun configured(ctx: Context): Boolean = prefs(ctx).contains(KEY_HOST)

    /**
     * Turn the hardware step counter's since-boot total into today's step count.
     *
     * The counter only ever increases, except it resets to zero on reboot. So
     * we remember the raw value that marked the start of today ([KEY_STEP_BASE])
     * and subtract it. Two things complicate that:
     *
     *  - **The day rolls over.** When the local date changes we rebase to the
     *    current reading, so today restarts at zero.
     *  - **The phone reboots mid-day.** The raw value drops below the last one
     *    we saw. We bank the steps counted so far today into [KEY_STEP_CARRY]
     *    and rebase to zero, so the day's total survives the restart — losing
     *    only the handful of steps taken during the boot itself, which no
     *    since-boot counter could recover anyway.
     *
     * Returns today's total. Persists the updated bookkeeping every call.
     */
    fun stepsToday(ctx: Context, raw: Long): Long {
        val p = prefs(ctx)
        val today = java.time.LocalDate.now().toString()
        val day = p.getString(KEY_STEP_DAY, null)
        var base = p.getLong(KEY_STEP_BASE, raw)
        var carry = p.getLong(KEY_STEP_CARRY, 0L)
        val last = p.getLong(KEY_STEP_LAST, raw)

        if (day != today) {
            // New day (or first ever reading): today starts here, nothing banked.
            base = raw
            carry = 0L
        } else if (raw < last) {
            // Reboot since the last sample — the counter restarted at zero.
            carry += (last - base).coerceAtLeast(0L)
            base = 0L
        }

        val total = (carry + (raw - base)).coerceAtLeast(0L)
        p.edit()
            .putString(KEY_STEP_DAY, today)
            .putLong(KEY_STEP_BASE, base)
            .putLong(KEY_STEP_CARRY, carry)
            .putLong(KEY_STEP_LAST, raw)
            .apply()
        return total
    }

    /**
     * Lower bound (epoch millis) for the next Health Connect exercise-session
     * scan. The first ever scan backfills a month of history; after that the
     * reader advances this forward, keeping a couple of days of overlap so a
     * session lost to a dying socket is picked up on the next scan (the desktop
     * upserts by id, so the re-reads cost nothing).
     */
    fun hcSince(ctx: Context): Long {
        val stored = prefs(ctx).getLong(KEY_HC_SINCE, 0L)
        if (stored > 0L) return stored
        return System.currentTimeMillis() - 30L * 24 * 60 * 60 * 1000
    }

    fun setHcSince(ctx: Context, ms: Long) {
        prefs(ctx).edit().putLong(KEY_HC_SINCE, ms).apply()
    }

    /** The last routine snapshot pushed for the widget, as a JSON string (empty
     *  until the desktop has pushed one). See [RoutineWidget]. */
    fun widgetState(ctx: Context): String =
        prefs(ctx).getString(KEY_WIDGET, "").orEmpty()

    fun setWidgetState(ctx: Context, json: String) {
        prefs(ctx).edit().putString(KEY_WIDGET, json).apply()
    }

    /** The stored alarm set (empty array until the desktop syncs one down). */
    fun alarms(ctx: Context): JSONArray =
        runCatching { JSONArray(prefs(ctx).getString(KEY_ALARMS, "[]")) }.getOrDefault(JSONArray())

    fun setAlarms(ctx: Context, arr: JSONArray) {
        prefs(ctx).edit().putString(KEY_ALARMS, arr.toString()).apply()
    }

    /** The Continuum web UI — the same server the socket talks to, over plain
     *  HTTP(S) rather than the websocket scheme. */
    fun httpUrl(ctx: Context): String {
        val raw = host(ctx)
        if (raw.isBlank()) return ""
        val secure = raw.startsWith("https://") || raw.startsWith("wss://")
        val stripped = raw
            .removePrefix("https://").removePrefix("http://")
            .removePrefix("wss://").removePrefix("ws://")
            .removeSuffix("/phone/ws")
            .trimEnd('/')
        return if (secure) "https://$stripped/" else "http://$stripped/"
    }

    /** Accepts what a human would actually type — "100.64.99.96:8848",
     *  "http://host:8848", or a full ws:// URL — and produces the endpoint. */
    fun wsUrl(ctx: Context): String {
        val raw = host(ctx)
        if (raw.isBlank()) return ""
        if (raw.startsWith("ws://") || raw.startsWith("wss://")) {
            return if (raw.endsWith("/phone/ws")) raw else raw.trimEnd('/') + "/phone/ws"
        }
        val stripped = raw.removePrefix("https://").removePrefix("http://").trimEnd('/')
        val scheme = if (raw.startsWith("https://")) "wss" else "ws"
        return "$scheme://$stripped/phone/ws"
    }
}
