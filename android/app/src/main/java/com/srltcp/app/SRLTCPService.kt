package com.srltcp.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python

/**
 * Keeps the Python SRLTCP node alive while the app is backgrounded.
 * The server is started by MainActivity; this service only shows a notification.
 */
class SRLTCPService : Service() {
    override fun onCreate() {
        super.onCreate()
        ensureChannel()
        val text = try {
            val py = Python.getInstance()
            if (py.getModule("srltcp.app").callAttr("is_android_server_ready").toBoolean()) {
                val port = py.getModule("srltcp.app").callAttr("get_android_web_port").toInt()
                "SRLTCP running on port $port"
            } else {
                "SRLTCP running"
            }
        } catch (_: Exception) {
            "SRLTCP running"
        }
        try {
            startForeground(NOTIFICATION_ID, buildNotification(text))
        } catch (e: Exception) {
            Log.w(TAG, "startForeground failed", e)
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            "SRLTCP",
            NotificationManager.IMPORTANCE_LOW
        )
        channel.description = "SRLTCP peer node"
        mgr.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val launch = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SRLTCP")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher_legacy)
            .setContentIntent(launch)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "SRLTCPService"
        private const val CHANNEL_ID = "srltcp_node"
        private const val NOTIFICATION_ID = 7825
    }
}