package com.srltcp.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.net.http.SslError
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private val handler = Handler(Looper.getMainLooper())
    private var loadAttempt = 0
    private val fallbackPorts = intArrayOf(9876, 9877, 9878)

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.allowFileAccess = false
        webView.webViewClient = object : WebViewClient() {
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

        setContentView(webView)

        Thread {
            try {
                Python.getInstance().getModule("srltcp.app").callAttr("start_android_server")
            } catch (_: Exception) {
                showError("Failed to start Python server")
            }
        }.start()

        scheduleLoad(3000)
    }

    private fun scheduleLoad(delayMs: Long) {
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed({ loadWebUi() }, delayMs)
    }

    private fun loadWebUi() {
        val port = resolvePort()
        val ports = if (loadAttempt == 0) {
            intArrayOf(port) + fallbackPorts.filter { it != port }.toIntArray()
        } else {
            fallbackPorts
        }
        val idx = loadAttempt.coerceAtMost(ports.size - 1)
        loadAttempt++
        if (loadAttempt > ports.size + 2) {
            showError("Cannot reach SRLTCP web UI. Try reinstalling the app.")
            return
        }
        webView.loadUrl("https://127.0.0.1:${ports[idx]}/")
    }

    private fun resolvePort(): Int {
        return try {
            val py = Python.getInstance()
            py.getModule("srltcp.app").callAttr("get_android_web_port").toInt()
        } catch (_: Exception) {
            9876
        }
    }

    private fun showError(message: String) {
        handler.post {
            webView.loadData(
                "<html><body style='font-family:sans-serif;padding:24px;background:#0c0e14;color:#eee'>" +
                    "<h2>SRLTCP</h2><p>$message</p></body></html>",
                "text/html",
                "UTF-8"
            )
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        webView.destroy()
        super.onDestroy()
    }
}