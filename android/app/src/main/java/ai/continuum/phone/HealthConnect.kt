package ai.continuum.phone

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.ActiveCaloriesBurnedRecord
import androidx.health.connect.client.records.BodyFatRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.LeanBodyMassRecord
import androidx.health.connect.client.records.OxygenSaturationRecord
import androidx.health.connect.client.records.RespiratoryRateRecord
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId

/**
 * Reading exercise sessions out of Health Connect.
 *
 * This is the counterpart to [Sensors] for on-device *health* data, and it's a
 * different shape from everything there: HC isn't a hardware probe you read in a
 * line, it's a permissioned datastore behind a coroutine API. So the reads here
 * are `suspend`, and every one is defensive — HC may be absent (it's part of the
 * OS only on Android 14+; a separate, possibly-missing app on 12/13), we may not
 * hold the read permission (granted in HC's own UI, never by a runtime prompt),
 * and a query can simply fail. In every one of those cases we return nothing
 * rather than throw, so a bare cupboard reads as "no sessions", not a crash.
 *
 * A session only *exists* here if some app wrote it — for Jake that means
 * DownDog syncing yoga into Health Connect. If nothing feeds HC, [sessions]
 * legitimately returns an empty list.
 *
 * Server-side counterpart: continuum/app/server.py (`_phone_workout`) ingests
 * each reported `workout` into the WorkoutStore.
 */
object HealthConnect {

    // What we ask to read: the sessions themselves, plus the active calories
    // burned during each, so a workout lands with an energy figure and not just
    // a bare interval. Both are android.permission.health.* grants.
    val PERMISSIONS = setOf(
        HealthPermission.getReadPermission(ExerciseSessionRecord::class),
        HealthPermission.getReadPermission(ActiveCaloriesBurnedRecord::class),
        // Recovery metrics for the Health tab — a wrist wearable (Fitbit Air,
        // Garmin, Samsung) writes these to Health Connect; we read them out.
        HealthPermission.getReadPermission(SleepSessionRecord::class),
        HealthPermission.getReadPermission(RestingHeartRateRecord::class),
        HealthPermission.getReadPermission(HeartRateVariabilityRmssdRecord::class),
        HealthPermission.getReadPermission(OxygenSaturationRecord::class),
        HealthPermission.getReadPermission(RespiratoryRateRecord::class),
        // Weight + body composition for the Body tab — a smart scale (Renpho,
        // Withings, …) writes these to Health Connect; we read them out. Body fat
        // and lean mass only exist if the scale measures them; a plain scale
        // writes weight alone and that still lands.
        HealthPermission.getReadPermission(WeightRecord::class),
        HealthPermission.getReadPermission(BodyFatRecord::class),
        HealthPermission.getReadPermission(LeanBodyMassRecord::class),
    )

    /** Whether Health Connect is present and usable on this device. On Android
     *  14+ it's baked into the OS; on 12/13 it's a separate app that may be
     *  absent or need updating, in which case we can't read and say so. */
    fun available(ctx: Context): Boolean =
        HealthConnectClient.getSdkStatus(ctx) == HealthConnectClient.SDK_AVAILABLE

    private fun client(ctx: Context): HealthConnectClient? =
        if (available(ctx)) HealthConnectClient.getOrCreate(ctx) else null

    /** Do we hold both read permissions? The grant lives in the HC app, so this
     *  is a suspend call — the caller checks it off the main thread. */
    suspend fun granted(ctx: Context): Boolean {
        val c = client(ctx) ?: return false
        return runCatching {
            c.permissionController.getGrantedPermissions().containsAll(PERMISSIONS)
        }.getOrDefault(false)
    }

    /**
     * Exercise sessions ending in `[sinceMillis, now]`, each normalised into the
     * shape the desktop's WorkoutStore upserts. Active calories over the session
     * window are aggregated in where the data exists; a session with no calorie
     * records still lands, at 0. Returns an empty list — never throws — when HC
     * is absent, unpermissioned, the window is empty, or a read fails.
     */
    suspend fun sessions(ctx: Context, sinceMillis: Long): List<JSONObject> {
        val c = client(ctx) ?: return emptyList()
        if (!granted(ctx)) return emptyList()
        val since = Instant.ofEpochMilli(sinceMillis)
        val now = Instant.now()
        if (!since.isBefore(now)) return emptyList()

        val records = runCatching {
            c.readRecords(
                ReadRecordsRequest(
                    recordType = ExerciseSessionRecord::class,
                    timeRangeFilter = TimeRangeFilter.between(since, now),
                )
            ).records
        }.getOrNull() ?: return emptyList()

        val out = ArrayList<JSONObject>(records.size)
        for (r in records) {
            // Active calories are a separate record type, so aggregate them over
            // this session's interval. Best-effort: no calorie data → 0, never fatal.
            val kcal = runCatching {
                c.aggregate(
                    AggregateRequest(
                        metrics = setOf(ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL),
                        timeRangeFilter = TimeRangeFilter.between(r.startTime, r.endTime),
                    )
                )[ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL]?.inKilocalories ?: 0.0
            }.getOrDefault(0.0)

            out.add(JSONObject().apply {
                // A stable, namespaced id so re-scans upsert rather than duplicate,
                // and so an HC row can never collide with a Speediance trainingId.
                put("training_id", "hc:${r.metadata.id}")
                put("title", titleFor(r))
                put("start_ts", r.startTime.toEpochMilli() / 1000.0)
                put("end_ts", r.endTime.toEpochMilli() / 1000.0)
                put("duration_s", r.endTime.epochSecond - r.startTime.epochSecond)
                put("calories", kcal)
                put("exercise_type", r.exerciseType)
                put("source", r.metadata.dataOrigin.packageName.ifBlank { "health_connect" })
            })
        }
        return out
    }

    /**
     * Daily recovery aggregates over `[sinceMillis, now]` — one JSONObject per
     * local calendar day that has data, already in the shape the desktop's
     * `HealthStore.upsert_day` ingests. Sleep is attributed to the WAKE day (the
     * day a session ends), matching how every wearable reports a night; the spot
     * readings (resting HR, HRV, SpO2, respiratory rate) are averaged per day.
     *
     * Every read is best-effort and independent: a missing permission or record
     * type contributes nothing rather than throwing, so a device that only
     * tracks sleep still reports sleep. Returns an empty list — never throws —
     * when HC is absent, unpermissioned or the window is empty.
     *
     * Server-side counterpart: continuum/app/server.py (`_phone_health`) upserts
     * each day into the HealthStore.
     */
    suspend fun vitals(ctx: Context, sinceMillis: Long): List<JSONObject> {
        val c = client(ctx) ?: return emptyList()
        if (!granted(ctx)) return emptyList()
        val since = Instant.ofEpochMilli(sinceMillis)
        val now = Instant.now()
        if (!since.isBefore(now)) return emptyList()
        val range = TimeRangeFilter.between(since, now)

        // day (YYYY-MM-DD) → its accumulating row. Linked so it stays oldest→newest.
        val days = LinkedHashMap<String, JSONObject>()
        fun dayFor(t: Instant) = t.atZone(ZoneId.systemDefault()).toLocalDate().toString()
        fun row(day: String) = days.getOrPut(day) {
            JSONObject().put("day", day).put("source", "health_connect")
        }
        // Running [sum, count] per (day, field) so several samples average cleanly.
        val sums = HashMap<String, DoubleArray>()
        fun addAvg(day: String, field: String, value: Double) {
            val a = sums.getOrPut("$day|$field") { DoubleArray(2) }
            a[0] += value; a[1] += 1
        }

        // ---- sleep: one session → its wake (end) day, summed by stage ----
        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = SleepSessionRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { s ->
            var deep = 0L; var rem = 0L; var light = 0L; var awake = 0L; var asleep = 0L
            for (st in s.stages) {
                val mins = (st.endTime.epochSecond - st.startTime.epochSecond) / 60
                when (st.stage) {
                    SleepSessionRecord.STAGE_TYPE_DEEP -> { deep += mins; asleep += mins }
                    SleepSessionRecord.STAGE_TYPE_REM -> { rem += mins; asleep += mins }
                    SleepSessionRecord.STAGE_TYPE_LIGHT,
                    SleepSessionRecord.STAGE_TYPE_SLEEPING -> { light += mins; asleep += mins }
                    SleepSessionRecord.STAGE_TYPE_AWAKE,
                    SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED,
                    SleepSessionRecord.STAGE_TYPE_OUT_OF_BED -> awake += mins
                }
            }
            val inBed = (s.endTime.epochSecond - s.startTime.epochSecond) / 60
            // No stage breakdown (some trackers only mark the interval) → the whole
            // session counts as asleep, so a bare night still lands with a duration.
            if (asleep == 0L && awake == 0L) asleep = inBed
            val r = row(dayFor(s.endTime))
            r.put("asleep_min", asleep)
            r.put("in_bed_min", inBed)
            if (deep > 0) r.put("deep_min", deep)
            if (rem > 0) r.put("rem_min", rem)
            if (light > 0) r.put("light_min", light)
            if (awake > 0) r.put("awake_min", awake)
        }

        // ---- spot readings: averaged per day ----
        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = RestingHeartRateRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { addAvg(dayFor(it.time), "resting_hr", it.beatsPerMinute.toDouble()) }
        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = HeartRateVariabilityRmssdRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { addAvg(dayFor(it.time), "hrv_ms", it.heartRateVariabilityMillis) }
        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = OxygenSaturationRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { addAvg(dayFor(it.time), "spo2", it.percentage.value) }
        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = RespiratoryRateRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { addAvg(dayFor(it.time), "respiratory_rate", it.rate) }

        // Fold the averaged spot readings onto their day rows.
        for ((key, acc) in sums) {
            if (acc[1] <= 0) continue
            val parts = key.split("|", limit = 2)
            val avg = acc[0] / acc[1]
            row(parts[0]).put(parts[1], if (parts[1] == "resting_hr") Math.round(avg).toDouble() else avg)
        }
        return days.values.toList()
    }

    /**
     * Daily weight + body composition over `[sinceMillis, now]` — one JSONObject
     * per local calendar day with a reading, already in the shape the desktop's
     * `BodyStore.set_measurement` ingests (`weight_kg`, and from a smart scale
     * `body_fat_pct` + `muscle_mass_kg`). A day's LATEST sample wins per field: a
     * weigh-in is a discrete event, not something to average, which also matches
     * how the desktop store replaces the day (latest weight wins).
     *
     * Every read is best-effort and independent: a missing permission or record
     * type contributes nothing rather than throwing, so a plain scale that only
     * writes weight still reports weight. Returns an empty list — never throws —
     * when HC is absent, unpermissioned or the window is empty.
     *
     * Server-side counterpart: continuum/app/server.py (`_phone_weight`) upserts
     * each day into the BodyStore.
     */
    suspend fun weight(ctx: Context, sinceMillis: Long): List<JSONObject> {
        val c = client(ctx) ?: return emptyList()
        if (!granted(ctx)) return emptyList()
        val since = Instant.ofEpochMilli(sinceMillis)
        val now = Instant.now()
        if (!since.isBefore(now)) return emptyList()
        val range = TimeRangeFilter.between(since, now)

        // day (YYYY-MM-DD) → its accumulating row. Linked so it stays oldest→newest.
        val days = LinkedHashMap<String, JSONObject>()
        fun dayFor(t: Instant) = t.atZone(ZoneId.systemDefault()).toLocalDate().toString()
        fun row(day: String) = days.getOrPut(day) {
            JSONObject().put("day", day).put("source", "health_connect")
        }
        // Keep the newest sample per (day, field) — later weigh-ins overwrite.
        val seen = HashMap<String, Long>()
        fun latest(day: String, field: String, t: Instant, value: Double) {
            val key = "$day|$field"
            val ms = t.toEpochMilli()
            if (ms >= (seen[key] ?: Long.MIN_VALUE)) {
                seen[key] = ms
                row(day).put(field, value)
            }
        }

        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = WeightRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { latest(dayFor(it.time), "weight_kg", it.time, it.weight.inKilograms) }
        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = BodyFatRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { latest(dayFor(it.time), "body_fat_pct", it.time, it.percentage.value) }
        runCatching {
            c.readRecords(ReadRecordsRequest(
                recordType = LeanBodyMassRecord::class, timeRangeFilter = range)).records
        }.getOrNull()?.forEach { latest(dayFor(it.time), "muscle_mass_kg", it.time, it.mass.inKilograms) }

        return days.values.toList()
    }

    /** The app's own title for the session (DownDog sets one) if present, else a
     *  readable name for the exercise type, else a plain "Workout". */
    private fun titleFor(r: ExerciseSessionRecord): String {
        val t = r.title
        if (!t.isNullOrBlank()) return t
        return EXERCISE_NAMES[r.exerciseType] ?: "Workout"
    }

    // A small map for the types most likely to show up here. Anything unmapped
    // falls back to "Workout"; the numeric type still rides along in the report.
    private val EXERCISE_NAMES = mapOf(
        ExerciseSessionRecord.EXERCISE_TYPE_YOGA to "Yoga",
        ExerciseSessionRecord.EXERCISE_TYPE_PILATES to "Pilates",
        ExerciseSessionRecord.EXERCISE_TYPE_STRETCHING to "Stretching",
        ExerciseSessionRecord.EXERCISE_TYPE_STRENGTH_TRAINING to "Strength training",
        ExerciseSessionRecord.EXERCISE_TYPE_RUNNING to "Running",
        ExerciseSessionRecord.EXERCISE_TYPE_WALKING to "Walking",
        ExerciseSessionRecord.EXERCISE_TYPE_HIKING to "Hiking",
        ExerciseSessionRecord.EXERCISE_TYPE_BIKING to "Cycling",
    )
}
