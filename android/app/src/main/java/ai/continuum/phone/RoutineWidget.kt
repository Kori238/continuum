package ai.continuum.phone

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import org.json.JSONObject

/**
 * The home-screen routine widget — an at-a-glance "what's next, how far in".
 *
 * It holds no logic of its own: the desktop pushes today's routine snapshot down
 * the link ([Commands.routineWidget] → [Prefs.setWidgetState]) and this repaints
 * from that last-known state. So it stays correct without the widget ever asking
 * for anything, keeps showing the last snapshot while the phone is briefly
 * offline (rather than blanking), and survives a reboot because the state is in
 * SharedPreferences, not memory.
 *
 * updatePeriodMillis is 0 in the provider XML — Android's own 30-minute minimum
 * would be both too slow and pointless here, since every refresh that matters is
 * driven by a push, not a timer.
 */
class RoutineWidget : AppWidgetProvider() {

    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) {
        // Fired when a widget is first placed and on the (disabled) period. Paint
        // whatever we last heard so a freshly-added widget isn't blank until the
        // next push.
        val views = build(ctx)
        for (id in ids) mgr.updateAppWidget(id, views)
    }

    companion object {

        /** Repaint every placed instance from the current stored state. Called by
         *  [Commands] when a fresh snapshot arrives. A no-op if none are placed. */
        fun refresh(ctx: Context) {
            val mgr = AppWidgetManager.getInstance(ctx)
            val ids = mgr.getAppWidgetIds(ComponentName(ctx, RoutineWidget::class.java))
            if (ids.isEmpty()) return
            val views = build(ctx)
            for (id in ids) mgr.updateAppWidget(id, views)
        }

        // The five rows the layout provides, paired (when-label, title). We show
        // one per upcoming item and hide the rest.
        private val WHEN_IDS = intArrayOf(
            R.id.widget_when_0, R.id.widget_when_1, R.id.widget_when_2,
            R.id.widget_when_3, R.id.widget_when_4,
        )
        private val TITLE_IDS = intArrayOf(
            R.id.widget_next_0, R.id.widget_next_1, R.id.widget_next_2,
            R.id.widget_next_3, R.id.widget_next_4,
        )
        private val ROW_IDS = intArrayOf(
            R.id.widget_row_0, R.id.widget_row_1, R.id.widget_row_2,
            R.id.widget_row_3, R.id.widget_row_4,
        )

        /** Paint row `i` with a when-label + title and show it. */
        private fun showRow(v: RemoteViews, i: Int, whenStr: String, title: String) {
            v.setTextViewText(WHEN_IDS[i], whenStr)
            v.setTextViewText(TITLE_IDS[i], title)
            v.setViewVisibility(ROW_IDS[i], android.view.View.VISIBLE)
        }

        /** A single-line message in row 0, the rest hidden. */
        private fun showMessage(v: RemoteViews, msg: String) {
            showRow(v, 0, "", msg)
            for (i in 1 until ROW_IDS.size) v.setViewVisibility(ROW_IDS[i], android.view.View.GONE)
        }

        private fun build(ctx: Context): RemoteViews {
            val v = RemoteViews(ctx.packageName, R.layout.widget_routine)
            val state = runCatching {
                Prefs.widgetState(ctx).takeIf { it.isNotBlank() }?.let { JSONObject(it) }
            }.getOrNull()

            if (state == null) {
                showMessage(v, "Open Continuum to sync")
                v.setTextViewText(R.id.widget_progress, "")
                v.setProgressBar(R.id.widget_bar, 1, 0, false)
            } else {
                val done = state.optInt("done", 0)
                val total = state.optInt("total", 0)
                // Prefer the up-to-5 `items` list; fall back to the single `next`
                // for a snapshot pushed by an older server that only sent one.
                val items = state.optJSONArray("items")
                    ?: state.optJSONObject("next")?.let { org.json.JSONArray().put(it) }
                val count = items?.length() ?: 0
                if (count > 0) {
                    for (i in 0 until ROW_IDS.size) {
                        if (i < count) {
                            val it = items!!.getJSONObject(i)
                            val whenStr = it.optString("when", "").ifBlank { "next" }
                            showRow(v, i, whenStr, it.optString("title", ""))
                        } else {
                            v.setViewVisibility(ROW_IDS[i], android.view.View.GONE)
                        }
                    }
                } else {
                    showMessage(v, if (total > 0) "All done 🎉" else "Nothing scheduled")
                }
                v.setTextViewText(R.id.widget_progress, "$done/$total today")
                // Guard the zero-item case: a max of 0 makes the bar ill-defined.
                v.setProgressBar(R.id.widget_bar, if (total > 0) total else 1, done, false)
            }

            // Tapping anywhere opens the app straight on the routine tab (the
            // EXTRA_VIEW deep-link). FLAG_IMMUTABLE is mandatory from Android 12;
            // FLAG_UPDATE_CURRENT keeps the extra fresh on the cached intent.
            // NEW_TASK is required launching an Activity from this non-Activity
            // context; the singleTask MainActivity then delivers this to
            // onNewIntent when it's already running.
            val open = PendingIntent.getActivity(
                ctx, 0,
                Intent(ctx, MainActivity::class.java)
                    .putExtra(MainActivity.EXTRA_VIEW, "routine")
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
            v.setOnClickPendingIntent(R.id.widget_root, open)
            return v
        }
    }
}
