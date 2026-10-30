package ai.continuum.phone

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Reading the phone.
 *
 * Every reading here is cheap and synchronous except location, which is the
 * one that costs real battery — so it is the one thing the service asks for on
 * a slow cadence rather than on every wake.
 */
object Sensors {

    fun battery(ctx: Context): JSONObject {
        val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        // The sticky ACTION_BATTERY_CHANGED broadcast carries the bits the
        // BatteryManager properties don't expose (plug state, temperature).
        val sticky: Intent? = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val plug = sticky?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
        val tempTenths = sticky?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1) ?: -1
        val status = sticky?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1

        return JSONObject().apply {
            put("level", level)
            put("charging", plug != 0 || status == BatteryManager.BATTERY_STATUS_CHARGING)
            put("plug", when (plug) {
                BatteryManager.BATTERY_PLUGGED_AC -> "ac"
                BatteryManager.BATTERY_PLUGGED_USB -> "usb"
                BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
                else -> "none"
            })
            if (tempTenths > 0) put("temp_c", tempTenths / 10.0)
            val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
            put("power_save", pm.isPowerSaveMode)
        }
    }

    fun screen(ctx: Context): JSONObject {
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
        return JSONObject().put("on", pm.isInteractive)
    }

    @SuppressLint("MissingPermission")
    fun connectivity(ctx: Context): JSONObject {
        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        val out = JSONObject()
        when {
            caps == null -> out.put("type", "offline")
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> {
                out.put("type", "wifi")
                // SSID needs location permission on modern Android; without it
                // we simply omit the field rather than reporting <unknown ssid>.
                // connectionInfo is deprecated with no equivalent that works
                // outside a NetworkCallback, and the callback route would mean
                // holding a registration all day for one cosmetic string.
                @Suppress("DEPRECATION")
                try {
                    val wm = ctx.applicationContext
                        .getSystemService(Context.WIFI_SERVICE) as WifiManager
                    val ssid = wm.connectionInfo.ssid?.trim('"')
                    if (!ssid.isNullOrBlank() && ssid != "<unknown ssid>") out.put("ssid", ssid)
                } catch (_: Throwable) { /* optional detail, never fatal */ }
            }
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> out.put("type", "cellular")
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> out.put("type", "ethernet")
            else -> out.put("type", "other")
        }
        if (caps != null) {
            out.put("metered", !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED))
        }
        return out
    }

    /**
     * A location fix, or null if we lack permission or the fix times out.
     * Balanced priority on purpose: street-level is enough to answer "is he
     * home / at the gym / out", and precise GPS would cost far more power than
     * that question is worth.
     */
    @SuppressLint("MissingPermission")
    fun location(ctx: Context, onResult: (JSONObject?) -> Unit) {
        if (!Perms.hasLocation(ctx)) {
            onResult(null)
            return
        }
        try {
            val client = LocationServices.getFusedLocationProviderClient(ctx)
            val cts = CancellationTokenSource()
            client.getCurrentLocation(Priority.PRIORITY_BALANCED_POWER_ACCURACY, cts.token)
                .addOnSuccessListener { loc ->
                    if (loc == null) {
                        onResult(null)
                    } else {
                        onResult(JSONObject().apply {
                            put("lat", loc.latitude)
                            put("lon", loc.longitude)
                            put("accuracy_m", loc.accuracy)
                            if (loc.hasSpeed()) put("speed_ms", loc.speed)
                        })
                    }
                }
                .addOnFailureListener { onResult(null) }
        } catch (_: Throwable) {
            onResult(null)
        }
    }

    /**
     * Today's step count, or null if we can't read it (permission not granted,
     * or the phone has no step-counter chip).
     *
     * TYPE_STEP_COUNTER is the right sensor for an all-day app: a dedicated
     * low-power counter that's always running, so reading it costs almost
     * nothing — unlike TYPE_STEP_DETECTOR, which would need us to stay
     * subscribed and wake on every stride. Its one awkwardness is that it
     * counts *since boot*, not *today*; [Prefs.stepsToday] turns the running
     * total into today's figure, keeping a per-day baseline and surviving the
     * reboot that resets the chip to zero.
     *
     * Like [location] it's asynchronous — a sensor delivers values by callback,
     * not on demand. We register a one-shot listener, take the first value, and
     * unregister; a timeout unregisters anyway so a phone that never delivers
     * an event can't leave the sensor subscribed.
     */
    @SuppressLint("MissingPermission")
    fun steps(ctx: Context, onResult: (JSONObject?) -> Unit) {
        if (!Perms.hasActivityRecognition(ctx)) {
            onResult(null)
            return
        }
        val sm = ctx.getSystemService(Context.SENSOR_SERVICE) as? SensorManager
        val sensor = sm?.getDefaultSensor(Sensor.TYPE_STEP_COUNTER)
        if (sm == null || sensor == null) {
            onResult(null)
            return
        }
        val handler = Handler(Looper.getMainLooper())
        val done = AtomicBoolean(false)
        val listener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                if (!done.compareAndSet(false, true)) return
                sm.unregisterListener(this)
                handler.removeCallbacksAndMessages(null)
                val raw = event.values.firstOrNull()?.toLong()
                if (raw == null) {
                    onResult(null)
                    return
                }
                onResult(JSONObject().apply {
                    put("count", Prefs.stepsToday(ctx, raw))
                    put("source", "phone")
                })
            }

            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
        }
        // The chip is already counting, so the current value normally lands the
        // moment we register; the 5s timeout is only a safety net against a
        // device that withholds the initial reading until the next step.
        if (!sm.registerListener(listener, sensor, SensorManager.SENSOR_DELAY_FASTEST, handler)) {
            sm.unregisterListener(listener)
            onResult(null)
            return
        }
        handler.postDelayed({
            if (done.compareAndSet(false, true)) {
                sm.unregisterListener(listener)
                onResult(null)
            }
        }, 5_000)
    }
}
