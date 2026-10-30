package ai.continuum.phone

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.PowerManager
import android.provider.Settings
import androidx.core.content.ContextCompat

/**
 * What we're actually allowed to do.
 *
 * Every capability in this app is separately grantable and several can only be
 * granted by hand in Settings — so the app has to be able to say precisely
 * which of them are live. Guessing produces the failure mode where Continuum
 * confidently reports a battery level it never received.
 */
object Perms {

    fun hasLocation(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    fun hasBackgroundLocation(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_BACKGROUND_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    fun canPostNotifications(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    /** Reading the hardware step counter — a runtime permission since Android 10. */
    fun hasActivityRecognition(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACTIVITY_RECOGNITION) ==
            PackageManager.PERMISSION_GRANTED

    /** Notification *reading* — granted only in Settings > Notification access. */
    fun canReadNotifications(ctx: Context): Boolean =
        Settings.Secure.getString(ctx.contentResolver, "enabled_notification_listeners")
            ?.contains(ctx.packageName) == true

    /** The accessibility service — foreground-app awareness, screen reads, and
     *  global actions. Granted only in Settings > Accessibility; no runtime
     *  prompt. Read from the enabled-services list rather than trusting the
     *  service's own static handle, which lags a toggle by a bind cycle. */
    fun canUseAccessibility(ctx: Context): Boolean =
        Settings.Secure.getString(ctx.contentResolver, "enabled_accessibility_services")
            ?.contains(ctx.packageName) == true

    /** Whether a background activity start will actually be honoured. */
    fun canStartActivities(ctx: Context): Boolean = Settings.canDrawOverlays(ctx)

    fun isBatteryExempt(ctx: Context): Boolean {
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.isIgnoringBatteryOptimizations(ctx.packageName)
    }

    /** Human-readable list of what's missing, for the setup screen. */
    fun missing(ctx: Context): List<String> = buildList {
        if (!hasLocation(ctx)) add("Location")
        else if (!hasBackgroundLocation(ctx)) add("Background location (set to \"Allow all the time\")")
        if (!canPostNotifications(ctx)) add("Post notifications")
        if (!hasActivityRecognition(ctx)) add("Physical activity (step count)")
        if (!canReadNotifications(ctx)) add("Notification access (read)")
        if (!canUseAccessibility(ctx)) add("Accessibility (foreground app, screen reads, global actions)")
        if (!canStartActivities(ctx)) add("Display over other apps (needed to open apps)")
        if (!isBatteryExempt(ctx)) add("Unrestricted battery (the link dies without it)")
    }
}
