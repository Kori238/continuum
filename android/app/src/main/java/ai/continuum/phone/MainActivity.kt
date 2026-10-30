package ai.continuum.phone

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.Toast
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

/**
 * Continuum itself, on the phone.
 *
 * This is a WebView onto the desktop's own web UI rather than a second,
 * native interface. That is the whole point: the server already ships a
 * responsive layout and a PWA manifest, and every feature Jake adds to the
 * desktop — a new tab, a new control — appears here the next time he pulls to
 * the app with no Kotlin written and no protocol to keep in sync. A hand-built
 * native chat would start out prettier and be a version behind within a week.
 *
 * What stays native is exactly what a web page cannot do: the persistent
 * socket, sensors, reading notifications, and launching apps. Those live in
 * [LinkService]; this class is a window.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        /** Set when returning from setup, where the server may have changed. */
        const val EXTRA_RELOAD = "reload"

        /** Deep-link a specific web-UI tab, e.g. "routine" from the home-screen
         *  widget. Matched against the web app's SECTION_OF views. */
        const val EXTRA_VIEW = "view"
    }

    private lateinit var web: WebView
    private lateinit var error: LinearLayout
    private lateinit var errorText: TextView
    private var failed = false

    // The tab to land on for the launch currently being handled. Read (and
    // cleared) by load() so a cold start opens straight on it; a warm launch
    // navigates in-page instead, in onNewIntent.
    private var pendingView: String? = null

    // <input type="file"> is dead in a bare WebView: the tap does nothing unless
    // the chrome client forwards it to a real picker. Jake's "attach image" button
    // (a recipe, a label) goes through here. The WebView hands us a callback to
    // deliver the chosen Uri(s) to — or null if he cancels, which we must still
    // send back or the input jams and never fires again.
    private var fileUploadCallback: ValueCallback<Array<Uri>>? = null

    private val fileChooser = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        // parseResult turns a cancel into null and a multi-select into the full
        // array, honouring whatever accept=/multiple the web input asked for.
        val parsed = WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        val cb = fileUploadCallback
        // DIAGNOSTIC (v0.16): an OS-level Toast, independent of the WebView and
        // its JS, so we can finally SEE where the attach dies. Three outcomes:
        //   cb=LOST  → the activity was recreated under memory pressure while the
        //              gallery was in front; the callback (and its WebView input)
        //              died with the old instance, so the pick is dropped.
        //   uris=0   → the picker handed back nothing (rc tells cancel vs empty).
        //   uris=1+, cb=ok, yet still no red line in chat → onReceiveValue isn't
        //              populating the input; a deeper WebView issue.
        Toast.makeText(
            this,
            "picker back: rc=${result.resultCode} uris=${parsed?.size ?: -1} " +
                "cb=${if (cb == null) "LOST" else "ok"}",
            Toast.LENGTH_LONG,
        ).show()
        if (cb == null) return@registerForActivityResult
        fileUploadCallback = null
        cb.onReceiveValue(parsed)
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)

        // First run: no point loading a URL the user has never confirmed.
        if (!Prefs.configured(this)) {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
            return
        }

        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0a1519")) // Continuum's own bg, so no white flash
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true          // the UI keeps its tab state here
                mediaPlaybackRequiresUserGesture = false
                cacheMode = WebSettings.LOAD_DEFAULT
            }
            webChromeClient = object : WebChromeClient() {
                // Route the web app's file input to a real Android picker; without
                // this override the tap is silently dropped and nothing opens.
                override fun onShowFileChooser(
                    webView: WebView,
                    filePathCallback: ValueCallback<Array<Uri>>,
                    fileChooserParams: FileChooserParams,
                ): Boolean {
                    // DIAGNOSTIC (v0.16): confirms the tap actually reached our
                    // chooser (paired with the "picker back" toast on return).
                    Toast.makeText(this@MainActivity, "picker: opening", Toast.LENGTH_SHORT).show()
                    // Only one picker at a time; drop any stale callback so the
                    // WebView isn't left waiting on a picker that's gone.
                    fileUploadCallback?.onReceiveValue(null)
                    fileUploadCallback = filePathCallback
                    return try {
                        fileChooser.launch(fileChooserParams.createIntent())
                        true
                    } catch (e: Exception) {
                        fileUploadCallback = null
                        false
                    }
                }
            }
            webViewClient = object : WebViewClient() {

                override fun shouldOverrideUrlLoading(
                    view: WebView, request: WebResourceRequest,
                ): Boolean {
                    val target = request.url
                    // Continuum's own pages stay in the shell; anything it links
                    // out to is the open web and belongs in a real browser.
                    if (target.host != null && target.host == Uri.parse(Prefs.httpUrl(this@MainActivity)).host) {
                        return false
                    }
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, target)) }
                    return true
                }

                override fun onReceivedError(
                    view: WebView, request: WebResourceRequest, err: WebResourceError,
                ) {
                    // Sub-resource failures are noise; only a dead main frame is
                    // worth replacing the screen over.
                    if (!request.isForMainFrame) return
                    showError(err.description?.toString() ?: "could not reach Continuum")
                }

                override fun onPageFinished(view: WebView, url: String) {
                    if (!failed) hideError()
                }
            }
        }

        error = buildErrorPane()

        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.parseColor("#0a1519"))
            addView(web, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            addView(error, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        }
        setContentView(root)

        // targetSdk 35 is edge-to-edge whether we ask for it or not, and a
        // WebView does not receive the cutout as CSS env() insets — so the web
        // UI's own safe-area padding can't save us. Inset the view instead, or
        // the bottom tab strip sits under the gesture bar.
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            // The keyboard has to be in here too. adjustResize alone doesn't
            // shrink an edge-to-edge window, so without this the composer sits
            // behind the keyboard exactly when you're trying to type into it.
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            insets
        }

        // Continuum's UI has its own history (tabs, conversations); back should
        // walk that before it leaves the app.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        // Opening the app is a good moment to revive a link that Doze or a
        // low-memory kill quietly ended.
        runCatching { startForegroundService(Intent(this, LinkService::class.java)) }

        // The web UI *is* the app, so a stale cached app.js is a stale app: every
        // fix Jake makes on the desktop has to actually arrive here. Under
        // LOAD_DEFAULT the WebView's HTTP cache can keep serving an old
        // /static/app.js on a cold start — the file has a stable URL and the
        // server sets no Cache-Control, so nothing forces a re-fetch — which
        // silently strands web-side fixes on the phone (this is exactly why an
        // attach fix could look shipped and still do nothing here). Clearing on
        // launch costs one shell re-download over the local link and guarantees
        // the build Jake is looking at is the build that's running.
        web.clearCache(true)

        pendingView = intent.getStringExtra(EXTRA_VIEW)
        load()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra(EXTRA_RELOAD, false)) { load(); return }
        // Deep-link into a tab on an already-running instance (singleTask, so we
        // land here rather than onCreate). If the error pane is up there's no
        // live page to script — reload onto the target; otherwise navigate in
        // place so chat/scroll state survives.
        val view = intent.getStringExtra(EXTRA_VIEW) ?: return
        if (failed) {
            pendingView = view
            load()
        } else {
            web.evaluateJavascript("window.gotoView && window.gotoView(${jsString(view)})", null)
        }
    }

    // Encode a string as a JS literal for evaluateJavascript. The value only ever
    // comes from our own widget, but quoting keeps injection impossible by design.
    private fun jsString(s: String): String =
        "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

    // The error pane latches: once a main-frame load fails it stays until a
    // successful reload, and only Retry/Setup trigger one. So a server that was
    // merely down while the phone slept (e.g. a restart) leaves the app stuck on
    // "Can't reach Continuum" even after it's back. Foregrounding is exactly
    // when to re-check — reload if the last load failed, so returning to the app
    // self-heals instead of waiting on a manual Retry. On a healthy app failed
    // is false here (load() clears it), so an ordinary resume reloads nothing.
    override fun onResume() {
        super.onResume()
        if (failed) load()
        // Foregrounding is also when to notice a newer build waiting on the
        // desktop. The check is a silent GET; it only surfaces a prompt when
        // there's genuinely a higher versionCode, and only downloads/installs if
        // Jake confirms — so it's safe to fire on every resume.
        Updater.check(this, manual = false)
    }

    private fun load() {
        val url = Prefs.httpUrl(this)
        if (url.isBlank()) {
            showError("No server configured.")
            return
        }
        failed = false
        hideError()
        // A pending deep-link becomes a #view hash on the loaded URL so the web
        // app lands on that tab as it boots. Consume it — later plain reloads
        // (Retry, resume self-heal) shouldn't keep forcing the same tab.
        val view = pendingView
        pendingView = null
        web.loadUrl(if (view != null) url.trimEnd('/') + "/#" + view else url)
    }

    // ---- the failure screen ------------------------------------------------

    /**
     * Shown instead of Chrome's "webpage not available", which on a tailnet
     * failure reads as though the app is broken. The two things actually worth
     * checking — is the desktop awake, is Tailscale up — are worth naming.
     */
    private fun buildErrorPane(): LinearLayout {
        val pad = (24 * resources.displayMetrics.density).toInt()
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(pad, pad, pad, pad)
            setBackgroundColor(Color.parseColor("#0a1519"))
            visibility = View.GONE

            addView(TextView(context).apply {
                text = "Can't reach Continuum"
                textSize = 20f
                setTextColor(Color.parseColor("#dceced"))
                gravity = Gravity.CENTER
            })
            errorText = TextView(context).apply {
                setTextColor(Color.parseColor("#7d9599"))
                gravity = Gravity.CENTER
                setPadding(0, 16, 0, 24)
            }
            addView(errorText)
            addView(Button(context).apply {
                text = "Retry"
                layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
                setOnClickListener { load() }
            })
            addView(Button(context).apply {
                text = "Setup"
                layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
                setOnClickListener {
                    startActivity(Intent(this@MainActivity, SetupActivity::class.java))
                }
            })
        }
    }

    private fun showError(detail: String) {
        failed = true
        errorText.text = "$detail\n\n${Prefs.httpUrl(this)}\n\n" +
            "Check the desktop is awake and Tailscale is connected on both ends."
        error.visibility = View.VISIBLE
    }

    private fun hideError() {
        error.visibility = View.GONE
    }
}
