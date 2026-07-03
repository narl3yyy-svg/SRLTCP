package com.srltcp.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
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
import android.net.http.SslError
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.webkit.WebViewClientCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class MainActivity : AppCompatActivity() {
    private var webView: WebView? = null
    private var statusView: TextView? = null
    private val handler = Handler(Looper.getMainLooper())
    private var loadAttempt = 0
    private val fallbackPorts = intArrayOf(9876, 9877, 9878)
    private var serviceStarted = false

    private val requestNotificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        startSrltcpService()
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            Log.e(TAG, "Uncaught on ${thread.name}", error)
            showFatal("Crash: ${error.javaClass.simpleName}: ${error.message}")
        }

        super.onCreate(savedInstanceState)
        try {
            SRLTCPApplication.pythonError?.let {
                showFatal("Python failed to start:\n$it")
                return
            }
            ensurePythonStarted()
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

            ensureServiceThenWaitForServer()
        } catch (e: Exception) {
            Log.e(TAG, "onCreate failed", e)
            showFatal("App failed to start:\n${e.message}")
        }
    }

    private fun ensurePythonStarted() {
        if (Python.isStarted()) return
        try {
            Python.start(AndroidPlatform(applicationContext))
            Log.i(TAG, "Python started from MainActivity fallback")
        } catch (e: Exception) {
            Log.e(TAG, "Python fallback start failed", e)
            throw e
        }
    }

    private fun ensureServiceThenWaitForServer() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(
                    this,
                    Manifest.permission.POST_NOTIFICATIONS
                ) == PackageManager.PERMISSION_GRANTED
            ) {
                startSrltcpService()
            } else {
                requestNotificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        } else {
            startSrltcpService()
        }
        waitForServerInBackground()
    }

    private fun startSrltcpService() {
        if (serviceStarted) return
        serviceStarted = true
        try {
            val serviceIntent = Intent(this, SRLTCPService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ContextCompat.startForegroundService(this, serviceIntent)
            } else {
                startService(serviceIntent)
            }
        } catch (e: Exception) {
            Log.e(TAG, "startForegroundService failed", e)
            startServerDirectly()
        }
    }

    private fun startServerDirectly() {
        Thread {
            try {
                val py = Python.getInstance()
                val filesDir = applicationContext.filesDir.absolutePath
                py.getModule("srltcp.utils.platform")
                    .callAttr("set_android_data_dir", filesDir)
                if (!py.getModule("srltcp.app").callAttr("is_android_server_ready").toBoolean()) {
                    py.getModule("srltcp.app").callAttr("start_android_server")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Direct server start failed", e)
                handler.post { showFatal("Server start failed:\n${e.message}") }
            }
        }.apply { name = "srltcp-direct"; start() }
    }

    private fun waitForServerInBackground() {
        Thread {
            try {
                val py = Python.getInstance()
                var waited = 0
                while (waited < 60000) {
                    val ready = py.getModule("srltcp.app")
                        .callAttr("is_android_server_ready")
                        .toBoolean()
                    if (ready) {
                        val port = py.getModule("srltcp.app")
                            .callAttr("get_android_web_port")
                            .toInt()
                        handler.post { statusView?.text = "Loading UI on port $port…" }
                        handler.post { scheduleLoad(500) }
                        return@Thread
                    }
                    Thread.sleep(300)
                    waited += 300
                }
                handler.post {
                    showFatal("SRLTCP server did not start within 60 seconds.")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server wait failed", e)
                handler.post { showFatal("Failed to reach server:\n${e.message}") }
            }
        }.apply { name = "srltcp-wait"; start() }
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
            stopService(Intent(this, SRLTCPService::class.java))
        }
        super.onDestroy()
    }

    companion object {
        private const val TAG = "SRLTCP"
    }
}