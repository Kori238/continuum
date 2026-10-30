package ai.continuum.phone

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * The accessibility service — Continuum's eyes and coarse hands on the phone.
 *
 * Two capabilities, split by risk on purpose:
 *
 *  - **Observe (read-only).** It hears every window change, so it knows the
 *    foreground app — pushed to Continuum as a `foreground` report so the
 *    activity timeline can show real per-app phone segments, the way the desktop
 *    reports its focused window instead of a screen-on blur. On request it also
 *    reads the on-screen text tree (`ui_observe`). It never touches a control.
 *
 *  - **Global actions.** Back, home, recents, the notification shade, quick
 *    settings, a screenshot, lock, the power dialog — the system-level buttons,
 *    performed via [performGlobalAction]. These act *as* Jake, so the command
 *    that drives them is marked sensitive server-side and must be asked for.
 *
 * What is deliberately absent: tapping, swiping or typing into specific
 * elements (the aspirational `ui_act`). That is the most invasive thing this API
 * grants — acting inside an arbitrary app as the user — and Jake has left it
 * off. The service *cannot* do it: the config declares `canPerformGestures=false`
 * and the code to dispatch a gesture does not exist here. Absence, not a flag.
 *
 * Lifecycle mirrors [NotificationRelay]: Android binds and unbinds this at will,
 * in a different object from [LinkService], so the two meet through a static sink
 * (foreground events out) and a static instance handle (commands in) rather than
 * binding to each other. Whichever exists when work arrives does it; if the link
 * is down a foreground change is simply dropped, never queued.
 */
class A11yService : AccessibilityService() {

    companion object {
        // Bounds on a single ui_observe read — an accessibility tree can be huge,
        // and this text rides the socket and then a model's context window.
        private const val MAX_NODES = 250
        private const val MAX_CONTROLS = 60
        private const val MAX_TEXT = 6000

        // The shade/quick-settings overlay reports as systemui; recording it as a
        // "foreground app" would just fragment the real app's segment every time
        // Jake glances at a notification. Skip it and the underlying app's segment
        // stays whole.
        private const val SYSTEM_UI = "com.android.systemui"

        @Volatile private var instance: A11yService? = null
        @Volatile private var sink: ((JSONObject) -> Unit)? = null
        @Volatile private var lastPackage: String = ""

        /** LinkService registers here to receive foreground-app changes. */
        fun attach(f: (JSONObject) -> Unit) { sink = f }
        fun detach() { sink = null }

        /** Whether the service is actually bound and running right now. */
        fun isEnabled(): Boolean = instance != null

        /** The foreground app right now, or null if unknown / service off — used
         *  to seed a fresh connection's snapshot so the desktop isn't blind to the
         *  app that was already in front before the socket came up. */
        fun foreground(ctx: Context): JSONObject? {
            val pkg = lastPackage
            if (pkg.isBlank()) return null
            return JSONObject().put("package", pkg).put("app", label(ctx, pkg))
        }

        /** The wire vocabulary of global actions, and their AccessibilityService
         *  constants. Minimal and non-destructive — no element interaction. */
        val ACTION_NAMES = listOf(
            "back", "home", "recents", "notifications", "quick_settings",
            "dismiss_shade", "screenshot", "lock", "power_dialog",
        )

        // Fully qualified: these are static constants inherited by the *class*
        // from AccessibilityService, but a companion object doesn't inherit the
        // enclosing class's supertype, so unqualified names wouldn't resolve.
        private fun actionCode(name: String): Int? = when (name.lowercase(Locale.ROOT)) {
            "back" -> AccessibilityService.GLOBAL_ACTION_BACK
            "home" -> AccessibilityService.GLOBAL_ACTION_HOME
            "recents" -> AccessibilityService.GLOBAL_ACTION_RECENTS
            "notifications" -> AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS
            "quick_settings" -> AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS
            "dismiss_shade" -> AccessibilityService.GLOBAL_ACTION_DISMISS_NOTIFICATION_SHADE
            "screenshot" -> AccessibilityService.GLOBAL_ACTION_TAKE_SCREENSHOT
            "lock" -> AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN
            "power_dialog" -> AccessibilityService.GLOBAL_ACTION_POWER_DIALOG
            else -> null
        }

        /** Perform a system global action. Returns ok/err JSON for the wire. */
        fun performGlobal(name: String): JSONObject {
            val svc = instance
                ?: return err("accessibility service is not enabled on the phone")
            val code = actionCode(name)
                ?: return err("unknown global action '$name' " +
                    "(have: ${ACTION_NAMES.joinToString(", ")})")
            val ok = runCatching { svc.performGlobalAction(code) }.getOrDefault(false)
            return JSONObject().put("ok", ok).put("action", name).apply {
                if (!ok) put("error", "the system refused the '$name' action")
            }
        }

        /** Read the on-screen accessibility tree as bounded text. Read-only. */
        fun observe(): JSONObject {
            val svc = instance
                ?: return err("accessibility service is not enabled on the phone")
            val root = svc.rootInActiveWindow
                ?: return err("nothing readable on screen right now")
            return try {
                val texts = ArrayList<String>()
                val controls = JSONArray()
                collect(root, texts, controls, intArrayOf(0))
                val pkg = root.packageName?.toString() ?: lastPackage
                JSONObject().apply {
                    put("ok", true)
                    put("package", pkg)
                    put("app", label(svc, pkg))
                    put("text", texts.joinToString("\n").take(MAX_TEXT))
                    put("controls", controls)
                }
            } catch (t: Throwable) {
                err("could not read the screen: ${t.message}")
            } finally {
                @Suppress("DEPRECATION") root.recycle()
            }
        }

        /** Depth-first gather of visible text and clickable labels, hard-capped.
         *  We don't recycle child nodes: recycle() is a no-op / deprecated from
         *  API 33 and the read is bounded and infrequent, so the leak window is
         *  negligible against the code it would add. */
        private fun collect(node: AccessibilityNodeInfo?, texts: MutableList<String>,
                            controls: JSONArray, count: IntArray) {
            if (node == null || count[0] >= MAX_NODES) return
            count[0]++
            val t = node.text?.toString()?.trim().orEmpty()
            val cd = node.contentDescription?.toString()?.trim().orEmpty()
            val label = if (t.isNotBlank()) t else cd
            if (label.isNotBlank()) {
                texts.add(label)
                if (node.isClickable && controls.length() < MAX_CONTROLS) controls.put(label)
            }
            for (i in 0 until node.childCount) collect(node.getChild(i), texts, controls, count)
        }

        private fun label(ctx: Context, pkg: String): String = runCatching {
            val pm = ctx.packageManager
            pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
        }.getOrDefault(pkg)

        private fun err(reason: String) = JSONObject().put("ok", false).put("error", reason)
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // The only event type the config subscribes to, but guard anyway.
        if (event?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val pkg = event.packageName?.toString().orEmpty()
        // Skip our own windows and the transient system shade, and dedupe: a
        // dialog inside the same app shouldn't re-report the app.
        if (pkg.isBlank() || pkg == packageName || pkg == SYSTEM_UI || pkg == lastPackage) return
        lastPackage = pkg
        val out = sink ?: return
        out(JSONObject().put("package", pkg).put("app", label(this, pkg)))
    }

    override fun onInterrupt() { /* no in-flight work to abandon between events */ }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }
}
