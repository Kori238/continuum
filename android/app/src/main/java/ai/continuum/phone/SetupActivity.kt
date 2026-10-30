package ai.continuum.phone

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/**
 * Setup and permissions — everything the web UI can't do for itself.
 *
 * This used to be the whole app. Now [MainActivity] shows Continuum proper and
 * this is the back room: where the link points, and which of the six separately
 * grantable capabilities are actually live. Three of them can only be turned on
 * by hand in Settings, so the app has to state plainly which are missing —
 * running with half of them off would feed Continuum a confidently wrong
 * picture of the phone.
 *
 * Built in code rather than XML: one column of text and buttons, where a layout
 * file would be more indirection than content.
 */
class SetupActivity : AppCompatActivity() {

    private lateinit var gaps: TextView
    private lateinit var endpoint: TextView
    private lateinit var version: TextView
    private lateinit var host: EditText
    private lateinit var name: EditText
    private lateinit var health: TextView

    private val askRuntime = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { refresh() }

    // Health Connect's grant flow is its own contract, not a runtime permission —
    // the set of android.permission.health.* strings goes in, the granted set
    // comes back, and we just re-read status.
    private val askHealth = registerForActivityResult(
        PermissionController.createRequestPermissionResultContract()
    ) { refresh() }

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)

        val pad = (16 * resources.displayMetrics.density).toInt()
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        column.addView(heading("Continuum setup"))
        column.addView(body("This phone reports its state to your desktop and accepts " +
            "commands from it. Nothing leaves your tailnet."))

        column.addView(label("Server"))
        host = field(Prefs.host(this), "https://host:8443")
        column.addView(host)

        column.addView(label("Device name"))
        name = field(Prefs.deviceName(this), "phone")
        column.addView(name)

        column.addView(button("Save and connect") {
            Prefs.setHost(this, host.text.toString())
            Prefs.setDeviceName(this, name.text.toString())
            // Restart rather than start: the service caches nothing, but the
            // live socket stays pointed at the old host until torn down.
            stopService(Intent(this, LinkService::class.java))
            startForegroundService(Intent(this, LinkService::class.java))
            toast("Connecting…")
            refresh()
        })

        column.addView(button("Open Continuum") {
            startActivity(Intent(this, MainActivity::class.java).apply {
                putExtra(MainActivity.EXTRA_RELOAD, true)
            })
            finish()
        })

        column.addView(button("Stop link") {
            startService(Intent(this, LinkService::class.java).setAction(LinkService.ACTION_STOP))
            toast("Link stopped")
        })

        column.addView(button("Check for updates") {
            Updater.check(this, manual = true)
        })

        endpoint = body("")
        column.addView(endpoint)

        // What's actually installed on this phone — read from PackageManager, so
        // it's the running APK, not a build-time constant that could drift from
        // it. Lets Jake glance at the version instead of catching an update to
        // prove which build landed.
        version = body("")
        column.addView(version)

        column.addView(heading("Permissions"))
        gaps = body("")
        column.addView(gaps)

        column.addView(button("Grant location, notifications + activity") {
            askRuntime.launch(arrayOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
                Manifest.permission.POST_NOTIFICATIONS,
                Manifest.permission.ACTIVITY_RECOGNITION,
            ))
        })
        column.addView(body("Android only offers \"Allow all the time\" for location on a " +
            "second visit, from the app's own settings page — grant it once above, then use " +
            "App info below."))

        column.addView(button("Grant Health Connect (workouts)") {
            if (HealthConnect.available(this)) {
                askHealth.launch(HealthConnect.PERMISSIONS)
            } else {
                // 12/13: HC is a separate app. Send them to install/open it.
                openHealthConnect()
            }
        })
        health = body("")
        column.addView(health)

        column.addView(button("Notification access (read)") {
            open(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
        })
        column.addView(button("Accessibility (foreground app + controls)") {
            open(Settings.ACTION_ACCESSIBILITY_SETTINGS)
        })
        column.addView(button("Display over other apps") {
            open(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, packageUri())
        })
        column.addView(button("Battery: set to Unrestricted") {
            open(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
        })
        column.addView(button("App info (background location)") {
            open(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, packageUri())
        })

        val scroll = ScrollView(this).apply { addView(column) }
        setContentView(scroll)
        // targetSdk 35 draws under the system bars by default; without this the
        // first heading sits behind the status bar.
        ViewCompat.setOnApplyWindowInsetsListener(scroll) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(0, bars.top, 0, bars.bottom)
            insets
        }
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        endpoint.text = "Endpoint: ${Prefs.wsUrl(this)}"
        version.text = installedVersion()
        val missing = Perms.missing(this)
        gaps.text = if (missing.isEmpty()) "All granted."
        else "Still needed:\n" + missing.joinToString("\n") { "  • $it" }
        refreshHealth()
    }

    /** Health Connect readiness. Kept out of [Perms.missing] because the grant
     *  check is a suspend call into the HC app, so it resolves asynchronously and
     *  updates its own line rather than blocking the rest of the refresh. */
    private fun refreshHealth() {
        if (!HealthConnect.available(this)) {
            health.text = "Health Connect: not available on this device"
            return
        }
        health.text = "Health Connect: checking…"
        lifecycleScope.launch {
            health.text = if (HealthConnect.granted(this@SetupActivity))
                "Health Connect: workout read granted"
            else "Health Connect: not granted — tap the button above"
        }
    }

    private fun openHealthConnect() {
        // The Health Connect onboarding/settings screen, or its Play listing if
        // the app isn't installed at all (older Android without HC in the OS).
        runCatching {
            startActivity(Intent(HealthConnectClient.ACTION_HEALTH_CONNECT_SETTINGS))
        }.onFailure { toast("Health Connect isn't installed on this phone") }
    }

    /** The installed build, straight from PackageManager (never throws for our
     *  own package). Mirrors the "v0.3 (code 3)" shape the update prompt uses. */
    private fun installedVersion(): String = runCatching {
        val pi = packageManager.getPackageInfo(packageName, 0)
        val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P)
            pi.longVersionCode else @Suppress("DEPRECATION") pi.versionCode.toLong()
        "Installed: v${pi.versionName} (code $code)"
    }.getOrDefault("Installed: unknown")

    // ---- plumbing ----------------------------------------------------------

    private fun open(action: String, data: Uri? = null) {
        val intent = Intent(action).apply { if (data != null) this.data = data }
        runCatching { startActivity(intent) }
            .onFailure { toast("Couldn't open that settings page") }
    }

    private fun packageUri() = Uri.fromParts("package", packageName, null)

    private fun toast(text: String) = Toast.makeText(this, text, Toast.LENGTH_SHORT).show()

    private fun heading(text: String) = TextView(this).apply {
        this.text = text
        textSize = 22f
        setPadding(0, 32, 0, 8)
    }

    private fun label(text: String) = TextView(this).apply {
        this.text = text
        setPadding(0, 16, 0, 4)
    }

    private fun body(text: String) = TextView(this).apply {
        this.text = text
        setPadding(0, 4, 0, 8)
    }

    @SuppressLint("SetTextI18n")
    private fun field(value: String, hint: String) = EditText(this).apply {
        setText(value)
        this.hint = hint
        inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        setSingleLine()
        layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
    }

    private fun button(text: String, onClick: () -> Unit) = Button(this).apply {
        this.text = text
        layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
        setOnClickListener { onClick() }
    }
}
