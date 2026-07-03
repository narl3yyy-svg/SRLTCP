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
 * Keeps the Python SRLTCP node alive while the WebView UI is open.
 */
class SRLTCPService : Service() {
    override fun onCreate() {
        super.onCreate()
        ensureChannel()
        startForeground(NOTIFICATION_ID, buildNotification("Starting SRLTCP…"))
        Thread {
            try {
                val py = Python.getInstance()
                val filesDir = applicationContext.filesDir.absolutePath
                py.getModule("srltcp.utils.platform")
                    .callAttr("set_android_data_dir", filesDir)
                py.getModule("srltcp.app").callAttr("start_android_server")
                var waited = 0
                while (waited < 60000) {
                    val ready = py.getModule("srltcp.app")
                        .callAttr("is_android_server_ready")
                        .toBoolean()
                    if (ready) {
                        val port = py.getModule("srltcp.app")
                            .callAttr("get_android_web_port")
                            .toInt()
                        updateNotification("SRLTCP running on port $port")
                        Log.i(TAG, "Server ready on port $port")
                        return@Thread
                    }
                    Thread.sleep(300)
                    waited += 300
                }
                updateNotification("SRLTCP server timeout")
                Log.e(TAG, "Server did not become ready within 60s")
            } catch (e: Exception) {
                Log.e(TAG, "Server start failed", e)
                updateNotification("SRLTCP failed to start")
            }
        }.start()
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

    private fun updateNotification(text: String) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.notify(NOTIFICATION_ID, buildNotification(text))
    }

    companion object {
        private const val TAG = "SRLTCPService"
        private const val CHANNEL_ID = "srltcp_node"
        private const val NOTIFICATION_ID = 7825
    }
}