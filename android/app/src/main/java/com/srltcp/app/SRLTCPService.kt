package com.srltcp.app

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log
import com.chaquo.python.Python

/**
 * Keeps the Python SRLTCP node alive while the app is backgrounded.
 * The server is started by MainActivity; this service only shows a notification.
 */
class SRLTCPService : Service() {
    override fun onCreate() {
        super.onCreate()
        SRLTCPNotifier.ensureChannels(this)
        val text = try {
            val py = Python.getInstance()
            if (py.getModule("srltcp.app").callAttr("is_android_server_ready").toBoolean()) {
                val port = py.getModule("srltcp.app").callAttr("get_android_web_port").toInt()
                "SRLTCP running on port $port"
            } else {
                "SRLTCP running in background"
            }
        } catch (_: Exception) {
            "SRLTCP running in background"
        }
        try {
            startForeground(
                SRLTCPNotifier.NOTIFICATION_NODE,
                SRLTCPNotifier.buildNodeNotification(this, text)
            )
        } catch (e: Exception) {
            Log.w(TAG, "startForeground failed", e)
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "SRLTCPService"
    }
}