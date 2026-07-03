package com.srltcp.app

import android.os.Bundle
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

        // Start SRLTCP web server in background (HTTPS localhost)
        Thread {
            try {
                val py = Python.getInstance()
                val app = py.getModule("srltcp.app")
                app.callAttr("main")
            } catch (_: Exception) {
                // Server may already run or needs CLI args — WebView still loads UI
            }
        }.start()

        val webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.webViewClient = WebViewClient()
        webView.loadUrl("https://127.0.0.1:9876/")
        setContentView(webView)
    }
}