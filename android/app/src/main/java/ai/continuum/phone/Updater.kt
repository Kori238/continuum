package ai.continuum.phone

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.widget.Toast
import androidx.core.content.FileProvider
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Pulling a new build from the desktop.
 *
 * The app is sideloaded — there is no Play Store to push updates — so instead
 * the desktop serves whatever APK build.sh last produced at /app/download, with
 * its version at /api/app/version. This compares that against what's installed
 * and, only on Jake's confirmation, downloads it and hands it to the system
 * package installer, which shows its own confirm dialog on top.
 *
 * Nothing here installs silently. The version check is a cheap GET that runs on
 * foreground; the download and the install happen only after Jake taps through
 * the prompt. That is deliberate — fetching and installing a binary is exactly
 * the kind of thing that should never happen speculatively.
 *
 * No coroutines and no new dependencies: OkHttp (already here for the socket)
 * does the calls asynchronously via enqueue, and results hop back to the UI
 * thread through the Activity.
 */
object Updater {

    private val http = OkHttpClient.Builder()
        .callTimeout(120, TimeUnit.SECONDS)
        .build()

    private data class Remote(val code: Long, val name: String, val size: Long)

    /**
     * Ask the desktop what build it has and, if it's newer than the installed
     * one, prompt to install. Safe to call on every foreground: it only surfaces
     * a dialog when the server genuinely has a higher versionCode.
     *
     * @param manual a button press deserves an answer either way ("up to date",
     *   "couldn't check"); a background check stays silent unless there's news.
     */
    fun check(activity: Activity, manual: Boolean) {
        val base = Prefs.httpUrl(activity)
        if (base.isBlank()) {
            if (manual) toast(activity, "No server configured")
            return
        }
        val url = base.trimEnd('/') + "/api/app/version"
        http.newCall(Request.Builder().url(url).get().build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (manual) ui(activity) { toast(activity, "Couldn't reach Continuum to check") }
            }

            override fun onResponse(call: Call, response: Response) {
                val remote = response.use { parse(it) }
                ui(activity) { onVersion(activity, remote, manual) }
            }
        })
    }

    private fun parse(response: Response): Remote? {
        if (!response.isSuccessful) return null
        val body = response.body?.string() ?: return null
        return runCatching {
            val o = JSONObject(body)
            if (!o.optBoolean("available", false)) null
            else Remote(
                o.optLong("versionCode", 0),
                o.optString("versionName", "?"),
                o.optLong("size", 0),
            )
        }.getOrNull()
    }

    private fun onVersion(activity: Activity, remote: Remote?, manual: Boolean) {
        if (remote == null) {
            if (manual) toast(activity, "No build available on the desktop")
            return
        }
        val installed = installedCode(activity)
        if (remote.code <= installed) {
            if (manual) toast(activity, "Up to date (v${remote.name})")
            return
        }
        AlertDialog.Builder(activity)
            .setTitle("Update Continuum?")
            .setMessage(
                "A newer build (v${remote.name}) is on the desktop${sizeHint(remote.size)}.\n\n" +
                    "It'll download and open the installer, which will ask you to confirm."
            )
            .setPositiveButton("Update") { _, _ -> startUpdate(activity, remote) }
            .setNegativeButton("Later", null)
            .show()
    }

    private fun startUpdate(activity: Activity, remote: Remote) {
        // Android won't let an app install packages until the user has granted it
        // "install unknown apps". Send them there rather than failing opaquely;
        // they can tap Update again once it's on.
        if (!activity.packageManager.canRequestPackageInstalls()) {
            toast(activity, "Allow Continuum to install apps, then tap Update again")
            runCatching {
                activity.startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:${activity.packageName}"),
                    )
                )
            }
            return
        }
        toast(activity, "Downloading update…")
        val url = Prefs.httpUrl(activity).trimEnd('/') + "/app/download"
        http.newCall(Request.Builder().url(url).get().build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                ui(activity) { toast(activity, "Download failed") }
            }

            override fun onResponse(call: Call, response: Response) {
                val apk = runCatching { saveApk(activity, response) }.getOrNull()
                ui(activity) {
                    if (apk == null) toast(activity, "Download failed")
                    else launchInstaller(activity, apk)
                }
            }
        })
    }

    private fun saveApk(activity: Activity, response: Response): File? {
        if (!response.isSuccessful) return null
        val body = response.body ?: return null
        val dir = File(activity.cacheDir, "updates").apply { mkdirs() }
        val file = File(dir, "continuum-phone.apk")
        body.byteStream().use { input ->
            file.outputStream().use { output -> input.copyTo(output) }
        }
        return file
    }

    private fun launchInstaller(activity: Activity, apk: File) {
        // A file:// URI throws FileUriExposedException on modern Android; the
        // installer has to be handed a content:// URI it's granted to read.
        val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.fileprovider", apk)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        runCatching { activity.startActivity(intent) }
            .onFailure { toast(activity, "Couldn't open the installer") }
    }

    private fun installedCode(ctx: Context): Long = runCatching {
        ctx.packageManager.getPackageInfo(ctx.packageName, 0).longVersionCode
    }.getOrDefault(0L)

    private fun sizeHint(size: Long): String =
        if (size > 0) " · ${size / (1024 * 1024)} MB" else ""

    private fun ui(activity: Activity, block: () -> Unit) {
        if (!activity.isFinishing) activity.runOnUiThread { block() }
    }

    private fun toast(ctx: Context, text: String) =
        Toast.makeText(ctx, text, Toast.LENGTH_SHORT).show()
}
