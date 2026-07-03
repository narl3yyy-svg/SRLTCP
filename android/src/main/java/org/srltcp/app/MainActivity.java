package org.srltcp.app;

import android.app.Activity;
import android.content.Intent;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

import org.kivy.android.PythonService;

/**
 * Launcher activity: starts the Python foreground service and loads the local HTTPS UI.
 */
public class MainActivity extends Activity {
    private static final String TAG = "SRLTCP";
    private static final int[] PORTS = {9876, 9877, 9878};

    private WebView webView;
    private TextView statusView;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private int loadAttempt = 0;
    private boolean serviceStarted = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Thread.setDefaultUncaughtExceptionHandler((thread, error) ->
            Log.e(TAG, "Uncaught on " + thread.getName(), error));

        FrameLayout root = new FrameLayout(this);

        statusView = new TextView(this);
        statusView.setText("Starting SRLTCP…");
        statusView.setTextColor(0xFF8B93A8);
        statusView.setGravity(Gravity.CENTER);
        statusView.setTextSize(14f);
        statusView.setPadding(48, 48, 48, 48);
        root.addView(statusView);

        webView = new WebView(this);
        webView.setVisibility(View.GONE);
        configureWebView();
        root.addView(webView, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT));

        setContentView(root);
        startPythonService();
        scheduleLoad(800);
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                statusView.setVisibility(View.GONE);
                webView.setVisibility(View.VISIBLE);
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                String host = error.getUrl();
                if (host != null && (host.contains("127.0.0.1") || host.contains("localhost"))) {
                    handler.proceed();
                } else {
                    handler.cancel();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    Log.w(TAG, "WebView error: " + error.getDescription());
                    scheduleLoad(2000);
                }
            }
        });
    }

    private void startPythonService() {
        if (serviceStarted) return;
        serviceStarted = true;
        try {
            Intent intent = new Intent(this, PythonService.class);
            intent.putExtra("pythonService", "SRLTCP");
            intent.putExtra("serviceEntrypoint", "service/srltcp_service.py");
            intent.putExtra("pythonName", "srltcp");
            intent.putExtra("serviceTitle", "SRLTCP");
            intent.putExtra("serviceDescription", "Secure peer node running");
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent);
            } else {
                startService(intent);
            }
        } catch (Exception e) {
            Log.e(TAG, "Failed to start Python service", e);
            statusView.setText("Failed to start SRLTCP service:\n" + e.getMessage());
        }
    }

    private void scheduleLoad(long delayMs) {
        handler.removeCallbacksAndMessages(null);
        handler.postDelayed(this::loadWebUi, delayMs);
    }

    private void loadWebUi() {
        int port = PORTS[Math.min(loadAttempt, PORTS.length - 1)];
        loadAttempt++;
        if (loadAttempt > PORTS.length * 6) {
            statusView.setText("Cannot reach SRLTCP web UI.\nTried ports: 9876, 9877, 9878");
            statusView.setVisibility(View.VISIBLE);
            webView.setVisibility(View.GONE);
            return;
        }
        String url = "https://127.0.0.1:" + port + "/";
        Log.i(TAG, "Loading " + url + " (attempt " + loadAttempt + ")");
        statusView.setText("Connecting to " + url + " …");
        statusView.setVisibility(View.VISIBLE);
        webView.loadUrl(url);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (webView != null) webView.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) webView.onResume();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (isFinishing() && webView != null) {
            try {
                webView.destroy();
            } catch (Exception ignored) {
                // ignore
            }
        }
        super.onDestroy();
    }
}