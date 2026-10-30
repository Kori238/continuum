package ai.continuum.phone

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.math.pow

/**
 * The link — one long-lived websocket to Continuum, held open by a foreground
 * service because Android will otherwise kill it inside a few minutes.
 *
 * Two design choices worth keeping:
 *
 * 1. **The phone drives the cadence.** Nothing here is polled by the desktop.
 *    We report when a value changes (battery, screen, connectivity all arrive
 *    as system broadcasts, free of charge) and only location runs on a timer,
 *    because it's the one reading that costs real power. A desktop polling
 *    into a dozing phone would be far more expensive for strictly worse data.
 *
 * 2. **Reconnect is assumed, not exceptional.** Doze, network handoff, and
 *    process death mean this socket dies many times a day. Backoff is capped
 *    low (a minute) because the failure is almost always transient, and every
 *    reconnect re-sends a full snapshot so the desktop is never left holding
 *    a value from before the gap.
 *
 * 3. **A dead link must be noticed by something other than the link.** The
 *    OkHttp callbacks are the primary reconnect trigger, but they are a single
 *    thread of control and it can be lost — see `onClosing` below, where a
 *    server-initiated close used to park the socket forever. When it is lost
 *    nothing downstream can tell: `send()` does not throw on a dead socket, it
 *    returns false. So there are now two independent detectors that do not
 *    share a failure mode: a refused send, and silence from the server for
 *    longer than three ping intervals (the server pongs every ping, so inbound
 *    traffic is guaranteed while the link is up). Both land on `retry()`, and
 *    the screen/connectivity broadcasts get to run the check too, because they
 *    fire constantly and keep working when a coroutine timer is frozen by Doze.
 */
class LinkService : Service() {

    companion object {
        private const val TAG = "ContinuumLink"
        private const val CHANNEL_ID = "continuum_link"
        private const val NOTIF_ID = 1
        const val ACTION_STOP = "ai.continuum.phone.STOP"

        /** Location cadence. Fifteen minutes is a deliberate compromise: fine
         *  enough to notice leaving the house, coarse enough to be invisible
         *  on the battery graph. */
        private val LOCATION_INTERVAL_MS = TimeUnit.MINUTES.toMillis(15)
        private val PING_INTERVAL_MS = TimeUnit.MINUTES.toMillis(4)

        /** Silence this long means the link is dead whatever `socket` says. The
         *  server answers every ping with a pong, so while the link is up we
         *  hear from it at the ping cadence; three intervals leaves room for one
         *  ping lost to a doze window without crying wolf. */
        private val STALE_AFTER_MS = PING_INTERVAL_MS * 3
    }

    private val scope = CoroutineScope(SupervisorJob())
    private var socket: WebSocket? = null
    private var attempt = 0
    private var running = true
    private var loops: Job? = null

    /** Bumped every time we give up on a socket. Each listener captures the
     *  value it was created with and returns early once it no longer matches, so
     *  a late callback from a socket we have already abandoned — the `cancel()`
     *  in `retry()` provokes exactly that — cannot schedule a second reconnect
     *  on top of the one already in flight. Same rule the hub applies at the
     *  other end ("a detach from a socket we no longer hold is ignored"). */
    @Volatile private var generation = 0

    /** When the server last sent us anything. 0 until the first frame, which is
     *  why every staleness test checks for that first: a link that has never
     *  spoken is not the same as one that has gone quiet. */
    @Volatile private var lastInbound = 0L

    private val client = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(true)
        .build()

    /** Battery, screen and connectivity all announce themselves. Subscribing is
     *  strictly cheaper than asking, and it means a change is reported the
     *  moment it happens rather than up to a cadence later. */
    private val changes = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            // Check the link BEFORE reporting into it. These broadcasts are the
            // cheapest liveness ticks the app has — they fire on every unlock and
            // every network change, they are delivered by the system rather than
            // by a coroutine timer Doze can freeze, and they are precisely the
            // moments a reconnect is both wanted and free. Reporting first would
            // just push the reading into a dead socket and lose it.
            ensureConnected()
            when (intent.action) {
                Intent.ACTION_BATTERY_CHANGED,
                Intent.ACTION_POWER_CONNECTED,
                Intent.ACTION_POWER_DISCONNECTED -> report("battery", Sensors.battery(this@LinkService))
                Intent.ACTION_SCREEN_ON, Intent.ACTION_SCREEN_OFF ->
                    report("screen", Sensors.screen(this@LinkService))
                else -> report("connectivity", Sensors.connectivity(this@LinkService))
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // The type must be repeated here, not just in the manifest: Android 14
        // throws MissingForegroundServiceTypeException on the untyped overload.
        ServiceCompat.startForeground(
            this, NOTIF_ID, buildNotification("connecting…"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
        )
        // NOT_EXPORTED is mandatory from Android 14 — an unflagged registration
        // of a non-system broadcast is a hard crash, not a warning.
        ContextCompat.registerReceiver(
            this, changes,
            IntentFilter().apply {
                addAction(Intent.ACTION_POWER_CONNECTED)
                addAction(Intent.ACTION_POWER_DISCONNECTED)
                addAction(Intent.ACTION_SCREEN_ON)
                addAction(Intent.ACTION_SCREEN_OFF)
                addAction(ConnectivityManagerCompat.ACTION)
            },
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        NotificationRelay.attach { payload -> send(Protocol.notification(payload)) }
        // Foreground-app changes from the accessibility service become `foreground`
        // reports, so the desktop's activity timeline gets real per-app phone
        // segments. Dropped silently when the socket is down — never queued.
        A11yService.attach { payload -> report("foreground", payload) }
        connect()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            running = false
            stopSelf()
            return START_NOT_STICKY
        }
        // START_STICKY: if Android reclaims us under pressure we want to come
        // back on our own rather than wait for the user to open the app.
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        NotificationRelay.detach()
        A11yService.detach()
        runCatching { unregisterReceiver(changes) }
        socket?.close(1000, "service stopping")
        scope.cancel()
        super.onDestroy()
    }

    // ---- the socket --------------------------------------------------------

    private fun connect() {
        if (!running) return
        val url = Prefs.wsUrl(this)
        if (url.isBlank()) {
            note("no server configured")
            return
        }
        Log.i(TAG, "connecting to $url")
        val req = Request.Builder().url(url).build()
        // Bump first, then drop anything we still hold: the bump makes the old
        // socket's callbacks stale, so the cancel below cannot come back round as
        // a reconnect. Without this a connect() racing the broadcast receiver's
        // ensureConnected() would leave an orphan socket open — harmless at this
        // end, but the hub then holds two phone sockets and has to detach one.
        val gen = ++generation
        socket?.cancel()
        lastInbound = 0L
        socket = client.newWebSocket(req, object : WebSocketListener() {

            /** True once we have abandoned this socket. See `generation`. */
            private fun stale() = gen != generation

            override fun onOpen(ws: WebSocket, response: Response) {
                if (stale()) { ws.cancel(); return }
                attempt = 0
                lastInbound = System.currentTimeMillis()
                Log.i(TAG, "connected")
                ws.send(Protocol.hello(
                    name = Prefs.deviceName(this@LinkService),
                    model = "${Build.MANUFACTURER} ${Build.MODEL}",
                    os = "Android ${Build.VERSION.RELEASE}",
                ))
                note("connected")
                // The socket is passed in rather than read back off the field:
                // `socket = client.newWebSocket(...)` assigns AFTER the call
                // returns, and onOpen runs on OkHttp's reader thread, so the
                // field can still be null here. The old loop tested `socket !=
                // null` as its condition and would have exited immediately and
                // silently, leaving the connection with no pings at all.
                startLoops(ws, gen)
                snapshot()
            }

            override fun onMessage(ws: WebSocket, text: String) {
                if (stale()) return
                lastInbound = System.currentTimeMillis()
                handle(text)
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                if (stale()) return
                Log.w(TAG, "socket failed: ${t.message}")
                retry()
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                // THE BUG THIS OVERRIDE EXISTS FOR. OkHttp delivers onClosed only
                // after *we* complete the close handshake: RealWebSocket calls
                // onClosing on the peer's close frame and reaches onClosed only
                // if close() has already been enqueued on our side. The default
                // onClosing is a no-op, so before this override a close initiated
                // by the server parked the socket here permanently — onClosed
                // never fired, retry() was never called, `socket` stayed non-null
                // so nothing else looked, and the app sat "connected" reporting
                // into a socket that was gone. Answering the close is what turns
                // a server close back into a reconnect.
                Log.i(TAG, "server closing: $code $reason")
                runCatching { ws.close(1000, null) }
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                if (stale()) return
                Log.i(TAG, "socket closed: $code $reason")
                if (running) retry()
            }
        })
    }

    private fun retry() {
        // Bump first: cancel() below makes OkHttp deliver onFailure for this
        // socket, and without the bump that callback would come back through
        // here and schedule a second, competing reconnect.
        generation++
        socket?.cancel()
        socket = null
        loops?.cancel()
        if (!running) return
        note("reconnecting…")
        // Exponential, but capped at a minute — these failures are transient
        // (a tunnel, a doze window, wifi handoff) and a long backoff just means
        // the desktop's picture goes stale for no reason.
        val backoff = min(60_000.0, 2.0.pow(attempt.toDouble()) * 1000).toLong()
        attempt++
        scope.launch {
            delay(backoff)
            connect()
        }
    }

    /** Re-open the link if it is not up, and decide whether "up" is a lie.
     *
     *  Two separate states get us here. A null socket is the easy one. The hard
     *  one is a socket that is non-null and silent, which is what a lost
     *  callback leaves behind — and no amount of inspecting the socket object
     *  reveals it, so the test is on the traffic instead: the server pongs every
     *  ping, so silence past [STALE_AFTER_MS] means the link is dead however
     *  healthy the handle looks. Cheap enough to call from a broadcast. */
    private fun ensureConnected() {
        if (!running) return
        if (socket == null) {
            // Not a failure, so it does not inherit a failure's backoff: this is
            // usually the screen coming on during a long retry delay.
            attempt = 0
            connect()
            return
        }
        val quiet = System.currentTimeMillis() - lastInbound
        if (lastInbound > 0L && quiet > STALE_AFTER_MS) {
            Log.w(TAG, "no frame from the server in ${quiet / 1000}s — link is dead")
            retry()
        }
    }

    /** False when the frame did not go out, which is also a link-is-dead signal.
     *
     *  OkHttp's send() does NOT throw on a dead socket — it returns false (the
     *  socket is closed or closing, or the outgoing buffer is over its cap). The
     *  `runCatching { ws.send(text) }.onFailure { … }` this replaces could
     *  therefore never fire: it was dead code wrapping a call that has no
     *  exceptional path, and every report went into the void with nothing
     *  logged and nothing retried. */
    private fun send(text: String): Boolean {
        val ws = socket ?: return false
        if (ws.send(text)) return true
        Log.w(TAG, "send refused — socket closed or backed up; reconnecting")
        retry()
        return false
    }

    private fun report(kind: String, payload: JSONObject) = send(Protocol.report(kind, payload))

    /** Everything we know, sent at connect. A reconnect must not leave the
     *  desktop holding readings from before the gap. */
    private fun snapshot() {
        report("battery", Sensors.battery(this))
        report("screen", Sensors.screen(this))
        report("connectivity", Sensors.connectivity(this))
        // If the accessibility service already knows the app in front, seed it so
        // a reconnect doesn't leave the timeline blind to it until the next switch.
        A11yService.foreground(this)?.let { report("foreground", it) }
        Sensors.steps(this) { s -> if (s != null) report("steps", s) }
        Sensors.location(this) { loc -> if (loc != null) report("location", loc) }
        reportWorkouts()
        reportHealth()
        reportWeight()
    }

    /** Health Connect exercise sessions → `workout` reports (e.g. DownDog yoga).
     *  Unlike the step counter this is a permissioned datastore read, so it's
     *  heavier than a hardware probe and rides the quarter-hour location cadence
     *  rather than the ping. Sessions are infrequent and the desktop upserts by
     *  id, so re-scanning a couple of days each time is free — which lets us
     *  skip a fragile exactly-once watermark and just keep a small overlap. */
    private fun reportWorkouts() {
        scope.launch {
            val since = Prefs.hcSince(this@LinkService)
            val sessions = HealthConnect.sessions(this@LinkService, since)
            if (sessions.isEmpty()) return@launch
            var newest = since
            for (s in sessions) {
                report("workout", s)
                newest = maxOf(newest, (s.optDouble("end_ts", 0.0) * 1000).toLong())
            }
            // Advance the watermark but keep a two-day overlap, so a session sent
            // into a socket that dies mid-flush is re-tried on the next scan.
            val advanced = newest - TimeUnit.DAYS.toMillis(2)
            if (advanced > since) Prefs.setHcSince(this@LinkService, advanced)
        }
    }

    /** Health Connect recovery aggregates → `health` reports (sleep stages, HRV,
     *  resting HR, SpO2, respiratory rate — from a Fitbit/Garmin/Samsung band).
     *  Like workouts this is a permissioned datastore read, so it rides the
     *  quarter-hour cadence rather than the ping. The desktop upserts by day
     *  (COALESCE), so re-sending the last few days each scan is cheap and
     *  idempotent — no watermark to keep, unlike the exercise sessions above. */
    private fun reportHealth() {
        scope.launch {
            val since = System.currentTimeMillis() - TimeUnit.DAYS.toMillis(3)
            for (r in HealthConnect.vitals(this@LinkService, since)) report("health", r)
        }
    }

    /** Health Connect weigh-ins → `weight` reports (weight and, from a smart
     *  scale like the Renpho, body-fat % + muscle mass). Like the health and
     *  workout reads this is a permissioned datastore read, so it rides the
     *  quarter-hour cadence rather than the ping. The desktop upserts by day
     *  (COALESCE, latest weight wins), so re-sending the last few days each scan
     *  is cheap and idempotent — no watermark to keep. */
    private fun reportWeight() {
        scope.launch {
            val since = System.currentTimeMillis() - TimeUnit.DAYS.toMillis(3)
            for (r in HealthConnect.weight(this@LinkService, since)) report("weight", r)
        }
    }

    private fun startLoops(ws: WebSocket, gen: Int) {
        loops?.cancel()
        loops = scope.launch {
            var sinceLocation = 0L
            // Keyed on the generation, not on `socket != null`: the field is
            // assigned after newWebSocket() returns and this can start before
            // that, and a generation also ends the loop the instant retry()
            // abandons this socket rather than whenever the field happens to be
            // cleared.
            while (running && gen == generation) {
                delay(PING_INTERVAL_MS)
                if (gen != generation) return@launch
                // The other half of the liveness pair. `send` catches a socket
                // that refuses the frame; this catches one that swallows it and
                // never answers — a pong is due every interval, so two missed in
                // a row is not a slow network, it is a dead link.
                val quiet = System.currentTimeMillis() - lastInbound
                if (lastInbound > 0L && quiet > STALE_AFTER_MS) {
                    Log.w(TAG, "no frame from the server in ${quiet / 1000}s — link is dead")
                    retry()
                    return@launch
                }
                if (!ws.send(Protocol.ping())) {
                    Log.w(TAG, "ping refused — socket closed or backed up; reconnecting")
                    retry()
                    return@launch
                }
                // Steps ride the ping cadence: the counter is a cheap low-power
                // read, so refreshing it every few minutes keeps the desktop's
                // figure current without its own timer or any real battery cost.
                Sensors.steps(this@LinkService) { s -> if (s != null) report("steps", s) }
                sinceLocation += PING_INTERVAL_MS
                if (sinceLocation >= LOCATION_INTERVAL_MS) {
                    sinceLocation = 0
                    Sensors.location(this@LinkService) { loc ->
                        if (loc != null) report("location", loc)
                    }
                    // Same quarter-hour cadence: pick up new exercise sessions,
                    // the latest overnight recovery metrics, and any new weigh-in.
                    reportWorkouts()
                    reportHealth()
                    reportWeight()
                }
            }
        }
    }

    // ---- inbound commands --------------------------------------------------

    private fun handle(text: String) {
        val msg = runCatching { JSONObject(text) }.getOrNull() ?: return
        when (msg.optString("type")) {
            "command" -> {
                val corr = msg.optString("corr")
                val command = msg.optString("command")
                val params = msg.optJSONObject("params") ?: JSONObject()
                // Off the socket thread: resolving and launching an app touches
                // PackageManager, which is far too slow to run inline.
                scope.launch {
                    val out = runCatching { Commands.dispatch(this@LinkService, command, params) }
                        .getOrElse { JSONObject().put("ok", false).put("error", it.message ?: "failed") }
                    send(Protocol.result(corr, out))
                }
            }
            "welcome" -> Log.i(TAG, "server welcomed us: ${msg.optJSONArray("commands")}")
            "pong" -> Unit
        }
    }

    // ---- the persistent notification --------------------------------------

    private fun note(state: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NOTIF_ID, buildNotification(state))
    }

    private fun buildNotification(state: String): Notification {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Continuum link", NotificationManager.IMPORTANCE_MIN)
                .apply { setShowBadge(false) }
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, LinkService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE
        )
        // MainActivity is a bare WebView with no native chrome, so this and the
        // launcher long-press are the only ways back into setup.
        val setup = PendingIntent.getActivity(
            this, 2, Intent(this, SetupActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("Continuum")
            .setContentText(state)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setContentIntent(open)
            .addAction(0, "Setup", setup)
            .addAction(0, "Stop", stop)
            .build()
    }
}

/** The connectivity broadcast action, kept in one place — the constant is
 *  deprecated but still delivered, and there is no non-deprecated broadcast
 *  equivalent for "the active network changed". */
private object ConnectivityManagerCompat {
    @Suppress("DEPRECATION")
    const val ACTION = android.net.ConnectivityManager.CONNECTIVITY_ACTION
}
