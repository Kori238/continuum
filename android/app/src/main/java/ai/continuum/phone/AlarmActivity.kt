package ai.continuum.phone

import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The full-screen alarm face, launched over the lock screen by [AlarmService]'s
 * full-screen intent. It owns no sound or scheduling — those live in the
 * service, so swiping this away never silences the alarm. All it does is show
 * the time and label and offer Snooze / Dismiss, which it forwards to the
 * service.
 *
 * Built in code rather than a layout XML to keep the alarm engine self-contained
 * (no new res/ files to keep in step).
 */
class AlarmActivity : Activity() {

    private var labelView: TextView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Show over the lock screen and wake the display — this is the one screen
        // that has to appear whatever state the phone is in.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
            (getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager)
                ?.requestDismissKeyguard(this, null)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                    WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        setContentView(buildUi())
        applyLabel(intent)
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        applyLabel(intent)
    }

    private fun applyLabel(intent: Intent?) {
        val label = intent?.getStringExtra(AlarmScheduler.EXTRA_LABEL)?.ifBlank { "Alarm" } ?: "Alarm"
        labelView?.text = label
    }

    private fun buildUi(): LinearLayout {
        val dp = resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#0a1519"))
            setPadding(px(32), px(48), px(32), px(48))
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        }

        val clock = TextView(this).apply {
            text = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date())
            setTextColor(Color.WHITE)
            textSize = 72f
            gravity = Gravity.CENTER
        }
        labelView = TextView(this).apply {
            setTextColor(Color.parseColor("#7fd4c0"))
            textSize = 22f
            gravity = Gravity.CENTER
            setPadding(0, px(8), 0, px(48))
        }
        root.addView(clock)
        root.addView(labelView)

        root.addView(Button(this).apply {
            text = "Dismiss"
            textSize = 20f
            layoutParams = LinearLayout.LayoutParams(px(220), px(64))
            setOnClickListener { send(AlarmService.ACTION_DISMISS) }
        })
        root.addView(Button(this).apply {
            text = "Snooze ${AlarmScheduler.SNOOZE_MINUTES} min"
            textSize = 18f
            layoutParams = LinearLayout.LayoutParams(px(220), px(56)).apply { topMargin = px(16) }
            setOnClickListener { send(AlarmService.ACTION_SNOOZE) }
        })
        return root
    }

    private fun send(action: String) {
        runCatching {
            startService(Intent(this, AlarmService::class.java).setAction(action))
        }
        finish()
    }

    // The back button must not dismiss a wake-up alarm by accident.
    @Suppress("MissingSuperCall", "OVERRIDE_DEPRECATION")
    override fun onBackPressed() { /* ignored — use Dismiss/Snooze */ }
}
