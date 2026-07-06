package com.srltcp.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.webkit.WebViewClientCompat
import com.chaquo.python.Python

class MainActivity : AppCompatActivity() {
    private var webView: WebView? = null
    private var statusView: TextView? = null
    private val handler = Handler(Looper.getMainLooper())
    private var loadAttempt = 0
    private var pageLoaded = false
    private var uiRestored = false
    private var serverThread: Thread? = null
    private var serviceStarted = false
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var isClosing = false

    var isActivityResumed = false
        private set

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val callback = filePathCallback
        filePathCallback = null
        if (callback == null) return@registerForActivityResult
        val uris = WebChromeClient.FileChooserParams.parseResult(
            result.resultCode,
            result.data
        )
        callback.onReceiveValue(uris)
    }

    private val requestNotificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        maybeStartForegroundService()
    }

    private val requestStoragePermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    private val requestAllFilesAccess = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { _ -> }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            Log.e(TAG, "Uncaught on ${thread.name}", error)
            showFatal("Crash: ${error.javaClass.simpleName}: ${error.message}")
        }

        super.onCreate(savedInstanceState)
        SRLTCPNotifier.ensureChannels(this)

        SRLTCPApplication.pythonError?.let {
            showFatal("Python failed to start:\n$it")
            return
        }

        if (!Python.isStarted()) {
            showFatal("Python runtime not initialized.\nRestart the app.")
            return
        }

        val root = FrameLayout(this)
        statusView = TextView(this).apply {
            text = "Starting SRLTCP…"
            setTextColor(0xFF8B93A8.toInt())
            gravity = Gravity.CENTER
            textSize = 14f
            setPadding(48, 48, 48, 48)
        }
        root.addView(statusView)

        webView = WebView(this).also { wv ->
            wv.visibility = android.view.View.GONE
            wv.settings.javaScriptEnabled = true
            wv.settings.domStorageEnabled = true
            wv.settings.allowFileAccess = true
            wv.settings.allowContentAccess = true
            wv.settings.databaseEnabled = true
            wv.settings.useWideViewPort = true
            wv.settings.loadWithOverviewMode = true
            wv.settings.mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
            WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
            wv.addJavascriptInterface(SRLTCPBridge(this@MainActivity), "SRLTCPAndroid")
            wv.webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(
                    webView: WebView,
                    filePathCallback: ValueCallback<Array<Uri>>,
                    fileChooserParams: FileChooserParams
                ): Boolean {
                    this@MainActivity.filePathCallback?.onReceiveValue(null)
                    this@MainActivity.filePathCallback = filePathCallback
                    return try {
                        fileChooserLauncher.launch(fileChooserParams.createIntent())
                        true
                    } catch (e: Exception) {
                        Log.e(TAG, "File chooser failed", e)
                        this@MainActivity.filePathCallback = null
                        false
                    }
                }
            }
            wv.webViewClient = object : WebViewClientCompat() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    return false
                }

                override fun onPageFinished(view: WebView, url: String) {
                    if (isClosing || isFinishing || url == "about:blank") return
                    pageLoaded = true
                    loadAttempt = 0
                    handler.removeCallbacks(loadRetryRunnable)
                    view.evaluateJavascript(
                        "document.documentElement.classList.add('android-app');" +
                            "document.getElementById('stat-cpu')?.classList.add('hidden');" +
                            "if(window.applyMobileLayout)window.applyMobileLayout('android');",
                        null
                    )
                    statusView?.visibility = android.view.View.GONE
                    view.visibility = android.view.View.VISIBLE
                    maybeStartForegroundService()
                }

                override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                    if (isClosing || isFinishing) {
                        handler.cancel()
                        return
                    }
                    val host = android.net.Uri.parse(error.url).host
                    if (host == "127.0.0.1" || host == "localhost") {
                        handler.proceed()
                    } else {
                        handler.cancel()
                    }
                }

                @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
                override fun onReceivedError(
                    view: WebView,
                    errorCode: Int,
                    description: String?,
                    failingUrl: String?
                ) {
                    if (isClosing || isFinishing) {
                        view.stopLoading()
                        return
                    }
                    super.onReceivedError(view, errorCode, description, failingUrl)
                }
            }
            root.addView(wv)
        }
        setContentView(root)

        if (savedInstanceState != null) {
            webView?.restoreState(savedInstanceState)
            val url = webView?.url
            if (!url.isNullOrBlank() && url != "about:blank") {
                uiRestored = true
                pageLoaded = true
                webView?.visibility = android.view.View.VISIBLE
                statusView?.visibility = android.view.View.GONE
            }
        }

        requestNotificationIfNeeded()
        requestStorageAccessThenStart()
    }

    fun postAlertNotification(title: String, body: String, tag: String) {
        if (isClosing || isFinishing) return
        if (isActivityResumed) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        SRLTCPNotifier.postAlert(this, title, body, tag)
    }

    private fun requestStorageAccessThenStart() {
        beginServerStartup()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                requestAllFilesAccess.launch(
                    Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).apply {
                        data = Uri.parse("package:$packageName")
                    }
                )
            }
            return
        }

        val needed = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.READ_EXTERNAL_STORAGE
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.READ_EXTERNAL_STORAGE)
        }
        if (ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.WRITE_EXTERNAL_STORAGE
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        }
        if (needed.isNotEmpty()) {
            requestStoragePermissions.launch(needed.toTypedArray())
        }
    }

    private fun beginServerStartup() {
        startPythonServer()
        if (uiRestored && isServerReady()) {
            maybeStartForegroundService()
            return
        }
        waitForServerThenLoad()
    }

    private fun startPythonServer() {
        if (serverThread?.isAlive == true) {
            try {
                val py = Python.getInstance()
                if (!py.getModule("srltcp.app").callAttr("is_android_server_ready").toBoolean()) {
                    serverThread = null
                } else {
                    return
                }
            } catch (_: Exception) {
                serverThread = null
            }
        }
        serverThread = Thread {
            try {
                val py = Python.getInstance()
                py.getModule("srltcp.utils.platform")
                    .callAttr("set_android_data_dir", applicationContext.filesDir.absolutePath)
                val appMod = py.getModule("srltcp.app")
                if (!appMod.callAttr("is_android_server_ready").toBoolean()) {
                    appMod.callAttr("start_android_server")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server thread failed", e)
                handler.post { showFatal("Server start failed:\n${e.message}") }
            }
        }.apply { name = "srltcp-server"; isDaemon = false; start() }
    }

    private fun waitForServerThenLoad() {
        Thread {
            try {
                val py = Python.getInstance()
                var waited = 0
                while (waited < 90000) {
                    val ready = py.getModule("srltcp.app")
                        .callAttr("is_android_server_ready")
                        .toBoolean()
                    if (ready) {
                        val port = py.getModule("srltcp.app")
                            .callAttr("get_android_web_port")
                            .toInt()
                        handler.post {
                            statusView?.text = "Loading UI on port $port…"
                            scheduleLoad(400)
                        }
                        return@Thread
                    }
                    Thread.sleep(250)
                    waited += 250
                }
                handler.post {
                    showFatal(
                        "SRLTCP server did not start within 90 seconds.\n" +
                            "Check logcat for SRLTCP tag."
                    )
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server wait failed", e)
                handler.post { showFatal("Failed to reach server:\n${e.message}") }
            }
        }.apply { name = "srltcp-wait"; start() }
    }

    private fun requestNotificationIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            requestNotificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun maybeStartForegroundService() {
        if (serviceStarted) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        serviceStarted = true
        try {
            val serviceIntent = Intent(this, SRLTCPService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ContextCompat.startForegroundService(this, serviceIntent)
            } else {
                startService(serviceIntent)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Foreground service not started (UI still works)", e)
            serviceStarted = false
        }
    }

    private val loadUiRunnable = Runnable { loadWebUi() }
    private val loadRetryRunnable = Runnable {
        if (isClosing || pageLoaded) return@Runnable
        val wv = webView ?: return@Runnable
        if (wv.visibility == android.view.View.VISIBLE) return@Runnable
        Log.w(TAG, "Web UI not ready yet, retrying…")
        scheduleLoad(2000)
    }

    private fun scheduleLoad(delayMs: Long) {
        if (isClosing) return
        handler.removeCallbacks(loadUiRunnable)
        handler.removeCallbacks(loadRetryRunnable)
        handler.postDelayed(loadUiRunnable, delayMs)
    }

    private fun loadWebUi() {
        if (isClosing) return
        val wv = webView ?: return
        if (!isServerReady()) {
            waitForServerThenLoad()
            return
        }
        loadAttempt++
        if (loadAttempt > 12) {
            showFatal("Cannot reach SRLTCP web UI on port ${resolvePort()}.")
            return
        }
        pageLoaded = false
        handler.removeCallbacks(loadRetryRunnable)
        val port = resolvePort()
        val url = "https://127.0.0.1:$port/"
        Log.i(TAG, "Loading $url (attempt $loadAttempt)")
        statusView?.text = "Connecting to $url …"
        wv.loadUrl(url)
        handler.postDelayed(loadRetryRunnable, 5000)
    }

    private fun resolvePort(): Int {
        return try {
            val py = Python.getInstance()
            if (py.getModule("srltcp.app").callAttr("is_android_server_ready").toBoolean()) {
                py.getModule("srltcp.app").callAttr("get_android_web_port").toInt()
            } else {
                9876
            }
        } catch (_: Exception) {
            9876
        }
    }

    private fun showFatal(message: String) {
        handler.post {
            webView?.let {
                try {
                    (it.parent as? FrameLayout)?.removeView(it)
                } catch (_: Exception) { /* ignore */ }
            }
            webView = null
            val tv = TextView(this).apply {
                text = "SRLTCP\n\n$message"
                setTextColor(0xFFEEEEEE.toInt())
                setBackgroundColor(0xFF0C0E14.toInt())
                setPadding(48, 48, 48, 48)
                textSize = 16f
            }
            setContentView(tv)
        }
    }

    override fun onPause() {
        isActivityResumed = false
        super.onPause()
        webView?.onPause()
        if (isFinishing) {
            prepareForClose()
        }
    }

    override fun onStop() {
        if (isFinishing) {
            prepareForClose()
        } else {
            maybeStartForegroundService()
        }
        super.onStop()
    }

    override fun onResume() {
        super.onResume()
        isActivityResumed = true
        if (isClosing) return
        webView?.onResume()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R &&
            Environment.isExternalStorageManager() &&
            serverThread?.isAlive != true
        ) {
            startPythonServer()
            waitForServerThenLoad()
            return
        }
        if (webView?.url.isNullOrBlank() &&
            statusView?.visibility == android.view.View.VISIBLE &&
            isServerReady()
        ) {
            scheduleLoad(300)
        }
    }

    private fun isServerReady(): Boolean {
        return try {
            Python.getInstance()
                .getModule("srltcp.app")
                .callAttr("is_android_server_ready")
                .toBoolean()
        } catch (_: Exception) {
            false
        }
    }

    private fun prepareForClose() {
        if (isClosing) return
        isClosing = true
        pageLoaded = false
        handler.removeCallbacks(loadRetryRunnable)
        handler.removeCallbacksAndMessages(null)
        webView?.let { wv ->
            try {
                wv.stopLoading()
                wv.loadUrl("about:blank")
                wv.visibility = android.view.View.GONE
            } catch (_: Exception) { /* ignore */ }
        }
        statusView?.visibility = android.view.View.GONE
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView?.saveState(outState)
    }

    override fun onDestroy() {
        if (isFinishing) {
            prepareForClose()
        }
        handler.removeCallbacksAndMessages(null)
        if (isFinishing) {
            try {
                webView?.destroy()
            } catch (_: Exception) { /* ignore */ }
            webView = null
            try {
                stopService(Intent(this, SRLTCPService::class.java))
            } catch (_: Exception) { /* ignore */ }
        }
        super.onDestroy()
    }

    companion object {
        private const val TAG = "SRLTCP"
    }
}
