package com.srltcp.app

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        Thread {
            try {
                val py = Python.getInstance()
                py.getModule("srltcp.app").callAttr("start_android_server")
            } catch (_: Exception) {
                // Server may already be running
            }
        }.start()

        val webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                return false
            }
        }

        // Allow self-signed localhost HTTPS cert
        Handler(Looper.getMainLooper()).postDelayed({
            webView.loadUrl("https://127.0.0.1:9876/")
        }, 2500)

        setContentView(webView)
    }
}