package ai.continuum.phone

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.VibratorManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlin.math.ceil

/**
 * The ringing itself — a foreground service so the sound survives whether or
 * not the full-screen [AlarmActivity] actually comes up (on an unlocked screen
 * Android may show the alarm as a heads-up notification instead of launching
 * the activity; the service is ringing regardless).
 *
 * Jake's volume rule lives here, at fire time — which is the whole reason the
 * alarm can't be handed to the system Clock app: **play at the device's current
 * alarm volume, or 50% of max, whichever is HIGHER.** We read STREAM_ALARM when
 * the alarm fires, raise it only if it's below the 50% floor, and restore the
 * original level when the alarm is dismissed or snoozed — so a loud alarm never
 * silently, permanently changes his setting.
 */
class AlarmService : Service() {

    companion object {
        const val ACTION_RING = "ai.continuum.phone.RING"
        const val ACTION_SNOOZE = "ai.continuum.phone.SNOOZE"
        const val ACTION_DISMISS = "ai.continuum.phone.DISMISS"
        private const val CHANNEL_ID = "continuum_alarm"
        private const val NOTIF_ID = 42
        private const val TAG = "ContinuumAlarm"
        /** Ring for at most five minutes, then give up so a missed alarm can't
         *  drain the battery or ring into the afternoon. */
        private const val AUTO_STOP_MS = 5 * 60_000L
    }

    private var player: MediaPlayer? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var savedAlarmVolume: Int? = null
    private var ringing = false
    private var currentId: String = ""
    private var currentLabel: String = "Alarm"
    private val handler = Handler(Looper.getMainLooper())
    private val autoStop = Runnable { stopRinging() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SNOOZE -> {
                runCatching { AlarmScheduler.snooze(this, currentId) }
                stopRinging()
            }
            ACTION_DISMISS -> stopRinging()
            else -> {
                currentId = intent?.getStringExtra(AlarmScheduler.EXTRA_ID) ?: currentId
                currentLabel = intent?.getStringExtra(AlarmScheduler.EXTRA_LABEL) ?: currentLabel
                startRinging()
            }
        }
        return START_NOT_STICKY
    }

    private fun startRinging() {
        // A second RING for an already-ringing service just refreshes the UI.
        val notif = buildNotification()
        ServiceCompat.startForeground(
            this, NOTIF_ID, notif,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK else 0,
        )
        if (ringing) return
        ringing = true

        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "continuum:alarm").apply {
            setReferenceCounted(false)
            runCatching { acquire(AUTO_STOP_MS + 10_000L) }
        }

        applyVolumeRule()
        startSound()
        startVibrate()
        handler.postDelayed(autoStop, AUTO_STOP_MS)
    }

    /** Jake's rule: max(current alarm volume, 50% of max), applied only upward,
     *  and remembered so [stopRinging] can put it back. */
    private fun applyVolumeRule() {
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val max = am.getStreamMaxVolume(AudioManager.STREAM_ALARM)
        val cur = am.getStreamVolume(AudioManager.STREAM_ALARM)
        val floor = ceil(max * 0.5).toInt().coerceIn(0, max)
        val target = maxOf(cur, floor)
        if (target != cur) {
            savedAlarmVolume = cur
            runCatching { am.setStreamVolume(AudioManager.STREAM_ALARM, target, 0) }
                .onFailure { Log.w(TAG, "could not raise alarm volume: ${it.message}") }
        }
    }

    private fun startSound() {
        val uri: Uri = RingtoneManager.getActualDefaultRingtoneUri(this, RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        runCatching {
            player = MediaPlayer().apply {
                setDataSource(this@AlarmService, uri)
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build())
                isLooping = true
                prepare()
                start()
            }
        }.onFailure { Log.w(TAG, "alarm sound failed: ${it.message}") }
    }

    private fun startVibrate() {
        val vm = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager ?: return
        val vib = vm.defaultVibrator
        val pattern = longArrayOf(0, 800, 600)   // wait, buzz, gap — repeats
        runCatching { vib.vibrate(VibrationEffect.createWaveform(pattern, 0)) }
    }

    private fun stopRinging() {
        handler.removeCallbacks(autoStop)
        ringing = false
        runCatching { player?.stop() }
        runCatching { player?.release() }
        player = null
        val vm = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
        runCatching { vm?.defaultVibrator?.cancel() }
        // Put the alarm volume back exactly as we found it.
        savedAlarmVolume?.let { prior ->
            val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
            runCatching { am.setStreamVolume(AudioManager.STREAM_ALARM, prior, 0) }
        }
        savedAlarmVolume = null
        runCatching { wakeLock?.release() }
        wakeLock = null
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        if (ringing) stopRinging()
        super.onDestroy()
    }

    // ---- the full-screen alarm notification --------------------------------

    private fun buildNotification(): android.app.Notification {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Alarms", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Continuum wake-up alarms"
                setBypassDnd(true)
                setSound(null, null)      // the service plays the sound, not the channel
                enableVibration(false)
            })

        val full = PendingIntent.getActivity(
            this, currentId.hashCode(),
            Intent(this, AlarmActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                putExtra(AlarmScheduler.EXTRA_ID, currentId)
                putExtra(AlarmScheduler.EXTRA_LABEL, currentLabel)
            },
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        val snooze = PendingIntent.getService(
            this, 1, Intent(this, AlarmService::class.java).setAction(ACTION_SNOOZE),
            PendingIntent.FLAG_IMMUTABLE)
        val dismiss = PendingIntent.getService(
            this, 2, Intent(this, AlarmService::class.java).setAction(ACTION_DISMISS),
            PendingIntent.FLAG_IMMUTABLE)

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
            .setContentTitle(currentLabel.ifBlank { "Alarm" })
            .setContentText("Alarm")
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setOngoing(true)
            .setAutoCancel(false)
            .setContentIntent(full)
            .setFullScreenIntent(full, true)
            .addAction(0, "Snooze ${AlarmScheduler.SNOOZE_MINUTES}m", snooze)
            .addAction(0, "Dismiss", dismiss)
            .build()
    }
}
