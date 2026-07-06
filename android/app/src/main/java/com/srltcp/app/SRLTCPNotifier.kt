package com.srltcp.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

object SRLTCPNotifier {
    const val CHANNEL_NODE = "srltcp_node"
    const val CHANNEL_ALERTS = "srltcp_alerts"
    const val NOTIFICATION_NODE = 7825
    private const val ALERT_ID_BASE = 9000

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = context.getSystemService(NotificationManager::class.java)
        val node = NotificationChannel(
            CHANNEL_NODE,
            "SRLTCP node",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Keeps SRLTCP running in the background"
            setShowBadge(false)
        }
        val alerts = NotificationChannel(
            CHANNEL_ALERTS,
            "SRLTCP alerts",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "Messages, transfers, and peer events"
        }
        mgr.createNotificationChannel(node)
        mgr.createNotificationChannel(alerts)
    }

    fun buildNodeNotification(context: Context, text: String): android.app.Notification {
        val launch = PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(context, CHANNEL_NODE)
            .setContentTitle("SRLTCP")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher_legacy)
            .setContentIntent(launch)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    fun postAlert(context: Context, title: String, body: String, tag: String) {
        val id = ALERT_ID_BASE + (tag.ifBlank { "$title-$body" }.hashCode() and 0x7fff)
        val launch = PendingIntent.getActivity(
            context,
            id,
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            },
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_ALERTS)
            .setContentTitle(title.ifBlank { "SRLTCP" })
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setSmallIcon(R.drawable.ic_launcher_legacy)
            .setContentIntent(launch)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .build()
        NotificationManagerCompat.from(context).notify(id, notification)
    }
}