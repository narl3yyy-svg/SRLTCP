package com.srltcp.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.net.http.SslError
import android.widget.FrameLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebViewClientCompat
import com.chaquo.python.Python

class MainActivity : AppCompatActivity() {
    private var webView: WebView? = null
    private val handler = Handler(Looper.getMainLooper())
    private var loadAttempt = 0
    private val fallbackPorts = intArrayOf(9876, 9877, 9878)

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            if (!Python.isStarted()) {
                showFatal("Python runtime not initialized")
                return
            }

            val root = FrameLayout(this)
            webView = WebView(this).also { wv ->
                wv.settings.javaScriptEnabled = true
                wv.settings.domStorageEnabled = true
                wv.settings.allowFileAccess = false
                wv.settings.databaseEnabled = true
                WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
                wv.webViewClient = object : WebViewClientCompat() {
                    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                        return false
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
                            scheduleLoad(1500)
                        }
                    }
                }
                root.addView(wv)
            }
            setContentView(root)

            Thread {
                try {
                    Python.getInstance().getModule("srltcp.app").callAttr("start_android_server")
                } catch (e: Exception) {
                    Log.e(TAG, "Server start failed", e)
                    showFatal("Failed to start server: ${e.message}")
                }
            }.start()

            scheduleLoad(4000)
        } catch (e: Exception) {
            Log.e(TAG, "onCreate failed", e)
            showFatal("App failed to start: ${e.message}")
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
        if (loadAttempt > ports.size + 3) {
            showFatal("Cannot reach SRLTCP web UI on ports ${ports.joinToString()}")
            return
        }
        wv.loadUrl("https://127.0.0.1:${ports[idx]}/")
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