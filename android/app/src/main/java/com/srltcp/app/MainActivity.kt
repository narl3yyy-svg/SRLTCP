package com.srltcp.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
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
    private val fallbackPorts = intArrayOf(9876, 9877, 9878)
    private var serverThread: Thread? = null
    private var serviceStarted = false

    private val requestNotificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        maybeStartForegroundService()
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            Log.e(TAG, "Uncaught on ${thread.name}", error)
            showFatal("Crash: ${error.javaClass.simpleName}: ${error.message}")
        }

        super.onCreate(savedInstanceState)

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
            wv.settings.allowFileAccess = false
            wv.settings.databaseEnabled = true
            wv.settings.mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
            WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
            wv.webViewClient = object : WebViewClientCompat() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    return false
                }

                override fun onPageFinished(view: WebView, url: String) {
                    statusView?.visibility = android.view.View.GONE
                    view.visibility = android.view.View.VISIBLE
                    maybeStartForegroundService()
                }

                override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                    val host = error.url.host
                    if (host == "127.0.0.1" || host == "localhost") {
                        handler.proceed()
                    } else {
                        handler.cancel()
                    }
                }

                override fun onReceivedError(
                    view: WebView,
                    request: WebResourceRequest,
                    error: WebResourceError
                ) {
                    if (request.isForMainFrame) {
                        Log.w(TAG, "WebView error: ${error.description}")
                        scheduleLoad(2000)
                    }
                }
            }
            root.addView(wv)
        }
        setContentView(root)

        startPythonServer()
        waitForServerThenLoad()
        requestNotificationIfNeeded()
    }

    private fun startPythonServer() {
        if (serverThread?.isAlive == true) return
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

    private fun scheduleLoad(delayMs: Long) {
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed({ loadWebUi() }, delayMs)
    }

    private fun loadWebUi() {
        val wv = webView ?: return
        val port = resolvePort()
        val ports = if (loadAttempt == 0) {
            intArrayOf(port) + fallbackPorts.filter { it != port }.toIntArray()
        } else {
            fallbackPorts
        }
        val idx = loadAttempt.coerceAtMost(ports.size - 1)
        loadAttempt++
        if (loadAttempt > ports.size + 8) {
            showFatal("Cannot reach SRLTCP web UI.\nTried ports: ${ports.joinToString()}")
            return
        }
        val url = "https://127.0.0.1:${ports[idx]}/"
        Log.i(TAG, "Loading $url (attempt $loadAttempt)")
        statusView?.text = "Connecting to $url …"
        wv.loadUrl(url)
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
        super.onPause()
        webView?.onPause()
    }

    override fun onResume() {
        super.onResume()
        webView?.onResume()
        if (webView?.url.isNullOrBlank() && statusView?.visibility == android.view.View.VISIBLE) {
            scheduleLoad(300)
        }
    }

    override fun onDestroy() {
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