package ai.continuum.phone

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * Acting on the phone.
 *
 * The hard constraint here is that Android 10+ forbids starting an activity
 * from the background. A plain `startActivity` from this service is silently
 * swallowed — no exception, no window, and the caller is told it worked. That
 * is the worst possible failure for an agent, so `openApp` checks whether the
 * launch can actually be honoured and reports the truth when it can't.
 */
object Commands {

    private const val CHANNEL_ID = "continuum_relay"

    fun dispatch(ctx: Context, command: String, params: JSONObject): JSONObject = when (command) {
        "open_app" -> openApp(ctx, params.optString("name"))
        "notify" -> notify(ctx, params.optString("title", "Continuum"), params.optString("message"))
        "list_apps" -> listApps(ctx)
        // Both route to the accessibility service, which reports honestly when
        // it isn't enabled rather than pretending the action happened.
        "ui_observe" -> A11yService.observe()
        "global_action" -> A11yService.performGlobal(params.optString("action"))
        "routine_widget" -> routineWidget(ctx, params)
        "set_alarm" -> setAlarm(ctx, params)
        "list_alarms" -> listAlarms(ctx)
        "cancel_alarm" -> cancelAlarm(ctx, params)
        "wake_lan" -> wakeLan(ctx, params)
        else -> JSONObject().put("ok", false).put("error", "unsupported command: $command")
    }

    // ---- open_app ----------------------------------------------------------

    private fun openApp(ctx: Context, query: String): JSONObject {
        if (query.isBlank()) return fail("no app name given")

        val match = resolve(ctx, query) ?: return fail("no installed app matches '$query'")
        val intent = ctx.packageManager.getLaunchIntentForPackage(match.first)
            ?: return fail("${match.second} has no launcher entry")

        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(intent)
            // Being able to call startActivity is not the same as the window
            // appearing: without the overlay permission Android drops it. Say
            // so rather than claiming a launch we can't verify.
            val verified = Perms.canStartActivities(ctx)
            JSONObject().apply {
                put("ok", true)
                put("opened", match.second)
                put("package", match.first)
                if (!verified) put(
                    "warning",
                    "launch requested, but 'Display over other apps' is off so Android may " +
                        "have suppressed it while the screen is locked or another app is in front"
                )
            }
        } catch (e: Throwable) {
            fail("could not launch ${match.second}: ${e.message}")
        }
    }

    /** Resolve a human's word for an app to a package. Exact package first,
     *  then exact label, then prefix, then contains — so "maps" doesn't lose
     *  to some app whose description merely mentions maps. */
    private fun resolve(ctx: Context, query: String): Pair<String, String>? {
        val q = query.trim().lowercase(Locale.ROOT)
        val apps = launchable(ctx)

        apps.firstOrNull { it.first.lowercase(Locale.ROOT) == q }?.let { return it }
        apps.firstOrNull { it.second.lowercase(Locale.ROOT) == q }?.let { return it }
        apps.firstOrNull { it.second.lowercase(Locale.ROOT).startsWith(q) }?.let { return it }
        apps.firstOrNull { it.second.lowercase(Locale.ROOT).contains(q) }?.let { return it }
        apps.firstOrNull { it.first.lowercase(Locale.ROOT).contains(q) }?.let { return it }
        return null
    }

    private fun launchable(ctx: Context): List<Pair<String, String>> {
        val pm = ctx.packageManager
        val main = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        return pm.queryIntentActivities(main, 0).mapNotNull { ri ->
            val pkg = ri.activityInfo?.packageName ?: return@mapNotNull null
            val label = ri.loadLabel(pm)?.toString() ?: pkg
            pkg to label
        }.distinctBy { it.first }
    }

    private fun listApps(ctx: Context): JSONObject {
        val arr = JSONArray()
        launchable(ctx).sortedBy { it.second.lowercase(Locale.ROOT) }.forEach {
            arr.put(JSONObject().put("package", it.first).put("label", it.second))
        }
        return JSONObject().put("ok", true).put("count", arr.length()).put("apps", arr)
    }

    // ---- notify ------------------------------------------------------------

    private fun notify(ctx: Context, title: String, message: String): JSONObject {
        if (message.isBlank()) return fail("no message given")
        if (!Perms.canPostNotifications(ctx)) return fail("notification permission not granted")

        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Continuum", NotificationManager.IMPORTANCE_DEFAULT)
        )
        val n = NotificationCompat.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setAutoCancel(true)
            .build()
        nm.notify(message.hashCode(), n)
        return JSONObject().put("ok", true).put("delivered", true)
    }

    // ---- routine_widget ----------------------------------------------------

    /** Desktop pushes today's routine snapshot (next item + progress); we stash
     *  it and repaint the home-screen widget. The widget itself is a dumb view
     *  over this last-known state, so a phone that's briefly offline keeps
     *  showing the last thing it heard rather than going blank. */
    private fun routineWidget(ctx: Context, params: JSONObject): JSONObject {
        Prefs.setWidgetState(ctx, params.toString())
        RoutineWidget.refresh(ctx)
        return JSONObject().put("ok", true).put("updated", true)
    }

    // ---- alarms ------------------------------------------------------------

    /** Add or replace an alarm; the phone owns scheduling and ringing. Returns
     *  the stored alarm (with its computed next fire) so the desktop UI can echo
     *  it back without a second round-trip. */
    private fun setAlarm(ctx: Context, params: JSONObject): JSONObject = try {
        val saved = AlarmScheduler.upsert(ctx, Alarm.fromJson(params))
        JSONObject().put("ok", true).put("alarm", saved.toJson())
    } catch (e: Throwable) {
        fail("could not set alarm: ${e.message}")
    }

    private fun listAlarms(ctx: Context): JSONObject {
        val arr = AlarmScheduler.listJson(ctx)
        return JSONObject().put("ok", true).put("count", arr.length()).put("alarms", arr)
    }

    private fun cancelAlarm(ctx: Context, params: JSONObject): JSONObject {
        val id = params.optString("id")
        if (id.isBlank()) return fail("no alarm id given")
        return JSONObject().put("ok", true).put("removed", AlarmScheduler.remove(ctx, id))
    }

    // ---- wake_lan ----------------------------------------------------------
    //
    // Why the phone, of all machines, is the one that wakes the desk: a magic
    // packet is an Ethernet broadcast, and a broadcast reaches only the link it
    // was sent on. The hub is a VPS in Germany; Tailscale is a layer-3 tunnel
    // with no broadcast domain at all. Neither can do this no matter how well
    // they can reach the desk while it is awake. The phone is the only always-on
    // device that is ever physically on Jake's LAN, so it is the only candidate.
    //
    // That also fixes the honest limit: this works on home Wi-Fi and fails on
    // mobile data. It reports which, rather than failing blank, because "you are
    // not at home" and "the packet went nowhere" want different reactions.

    private fun wakeLan(ctx: Context, params: JSONObject): JSONObject {
        val mac = parseMac(params.optString("mac")) ?: return fail(
            "need the desk's wired NIC MAC, as 12 hex digits")

        val targets = broadcastTargets(ctx)
        if (targets.isEmpty()) return fail(
            "the phone is not on Wi-Fi — a magic packet only travels over the local " +
                "network, so this has to be sent from home")

        // 6 bytes of 0xFF, then the MAC sixteen times over. The NIC's wake filter
        // scans the frame for this shape; it is not addressed or parsed, which is
        // why it can be delivered to a machine with no IP stack running.
        val packet = ByteArray(6 + 16 * 6)
        for (i in 0 until 6) packet[i] = 0xFF.toByte()
        for (rep in 0 until 16) System.arraycopy(mac, 0, packet, 6 + rep * 6, 6)

        val sent = mutableListOf<String>()
        var lastError: String? = null
        runCatching {
            java.net.DatagramSocket().use { sock ->
                sock.broadcast = true
                for (addr in targets) {
                    // 9 is the conventional discard port WoL settled on; 7 is the
                    // older echo port some NICs still listen on. Both are cheap,
                    // and which one a given card honours is a firmware detail.
                    for (port in intArrayOf(9, 7)) {
                        runCatching {
                            sock.send(java.net.DatagramPacket(
                                packet, packet.size, java.net.InetAddress.getByName(addr), port))
                            sent += "$addr:$port"
                        }.onFailure { lastError = it.message ?: it.javaClass.simpleName }
                    }
                }
            }
        }.onFailure { return fail("could not open a broadcast socket: ${it.message}") }

        // A send that reached nothing is a failure even though no exception
        // survived to say so — every per-target attempt swallows its own.
        if (sent.isEmpty()) return fail("no magic packet left the phone (${lastError ?: "unknown"})")
        return JSONObject().put("ok", true).put("sent", JSONArray(sent))
            .put("message", "magic packet sent to ${sent.size} address(es)")
    }

    /** "D8-43-AE-67-2E-23", "d8:43:ae:67:2e:23" and bare hex all mean one MAC. */
    private fun parseMac(raw: String): ByteArray? {
        val hex = raw.filter { it.isDigit() || it in 'a'..'f' || it in 'A'..'F' }
        if (hex.length != 12) return null
        return ByteArray(6) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }

    /**
     * Where to aim, derived from the phone's own IPv4 address and prefix length.
     *
     * 255.255.255.255 alone is not enough: Android routes the limited broadcast
     * out whichever interface it likes, and several OEM builds simply drop it.
     * The directed subnet broadcast (192.168.68.255 for a /24) is what reliably
     * crosses the Wi-Fi link, so both are tried and the directed one goes first.
     *
     * Returning an empty list means "not on a LAN at all", which is a different
     * answer from "sent and the desk stayed asleep" — see the caller.
     */
    private fun broadcastTargets(ctx: Context): List<String> {
        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE)
            as? android.net.ConnectivityManager ?: return emptyList()
        val net = cm.activeNetwork ?: return emptyList()
        val caps = cm.getNetworkCapabilities(net) ?: return emptyList()
        // Mobile data has no path to the desk's link, so refuse rather than fire
        // a packet into a carrier network and report success.
        if (!caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) &&
            !caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_ETHERNET)) return emptyList()

        val out = mutableListOf<String>()
        val props = cm.getLinkProperties(net)
        for (link in props?.linkAddresses.orEmpty()) {
            val ip = link.address as? java.net.Inet4Address ?: continue
            val bytes = ip.address
            // Set every host bit: the directed broadcast for this prefix.
            val hostBits = 32 - link.prefixLength
            if (hostBits !in 1..30) continue
            var v = ((bytes[0].toInt() and 0xFF) shl 24) or ((bytes[1].toInt() and 0xFF) shl 16) or
                ((bytes[2].toInt() and 0xFF) shl 8) or (bytes[3].toInt() and 0xFF)
            v = v or ((1 shl hostBits) - 1)
            out += "${(v ushr 24) and 0xFF}.${(v ushr 16) and 0xFF}." +
                "${(v ushr 8) and 0xFF}.${v and 0xFF}"
        }
        out += "255.255.255.255"
        return out
    }

    private fun fail(reason: String) = JSONObject().put("ok", false).put("error", reason)
}
