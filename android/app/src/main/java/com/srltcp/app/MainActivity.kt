package com.srltcp.app

import android.annotation.SuppressLint
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
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebViewClientCompat
import com.chaquo.python.Python

class MainActivity : AppCompatActivity() {
    private var webView: WebView? = null
    private var statusView: TextView? = null
    private val handler = Handler(Looper.getMainLooper())
    private var loadAttempt = 0
    private val fallbackPorts = intArrayOf(9876, 9877, 9878)

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            Log.e(TAG, "Uncaught on ${thread.name}", error)
            showFatal("Crash: ${error.javaClass.simpleName}: ${error.message}")
        }

        super.onCreate(savedInstanceState)
        try {
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

            Thread {
                try {
                    val py = Python.getInstance()
                    py.getModule("srltcp.app").callAttr("start_android_server")
                    var waited = 0
                    while (waited < 45000) {
                        try {
                            val port = py.getModule("srltcp.app").callAttr("get_android_web_port").toInt()
                            if (port in 1024..65535) {
                                handler.post { statusView?.text = "Loading UI on port $port…" }
                                break
                            }
                        } catch (_: Exception) { }
                        Thread.sleep(300)
                        waited += 300
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Server start failed", e)
                    showFatal("Failed to start server:\n${e.message}")
                }
            }.start()

            scheduleLoad(3000)
        } catch (e: Exception) {
            Log.e(TAG, "onCreate failed", e)
            showFatal("App failed to start:\n${e.message}")
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
        if (loadAttempt > ports.size + 5) {
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
            Python.getInstance().getModule("srltcp.app").callAttr("get_android_web_port").toInt()
        } catch (_: Exception) {
            9876
        }
    }

    private fun showFatal(message: String) {
        handler.post {
            webView?.destroy()
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

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        webView?.destroy()
        webView = null
        super.onDestroy()
    }

    companion object {
        private const val TAG = "SRLTCP"
    }
}