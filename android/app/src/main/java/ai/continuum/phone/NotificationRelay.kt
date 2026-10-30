package ai.continuum.phone

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject

/**
 * Notifications, forwarded to Continuum.
 *
 * Android owns this service's lifecycle — it binds and unbinds it at will, and
 * it is a *different* process-level object from LinkService. So the two find
 * each other through a static sink rather than binding: whichever exists when a
 * notification lands does the right thing, and if the link is down the
 * notification is simply dropped rather than queued. That is deliberate. A
 * backlog of hours-old notifications replayed on reconnect would be worse than
 * silence — Continuum would act on things the user has long since dealt with.
 */
class NotificationRelay : NotificationListenerService() {

    companion object {
        @Volatile
        private var sink: ((JSONObject) -> Unit)? = null

        fun attach(f: (JSONObject) -> Unit) { sink = f }
        fun detach() { sink = null }

        /** Noise that says nothing about the user's life. Media controls and
         *  our own status line would otherwise dominate the feed. */
        private val IGNORED_PACKAGES = setOf(
            "android",
            "com.android.systemui",
            "ai.continuum.phone",
        )
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val out = sink ?: return
        if (sbn.packageName in IGNORED_PACKAGES) return

        // Ongoing = persistent status (playing music, navigation, a download).
        // It isn't an event, and it re-posts constantly as it updates.
        if (sbn.notification.flags and Notification.FLAG_ONGOING_EVENT != 0) return
        if (sbn.notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        val extras = sbn.notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val body = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
            ?: extras.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        if (title.isBlank() && body.isBlank()) return

        out(JSONObject().apply {
            put("app", appLabel(sbn.packageName))
            put("package", sbn.packageName)
            put("title", title)
            put("body", body)
        })
    }

    private fun appLabel(pkg: String): String = runCatching {
        val pm = packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    }.getOrDefault(pkg)
}
