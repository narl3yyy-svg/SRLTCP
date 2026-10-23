package com.srltcp.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

object SRLTCPNotifier {
    const val CHANNEL_NODE = "srltcp_node"
    const val CHANNEL_ALERTS = "srltcp_alerts"
    const val CHANNEL_CALLS = "srltcp_calls"
    const val CHANNEL_ONGOING = "srltcp_ongoing_call"
    const val NOTIFICATION_NODE = 7825
    const val NOTIFICATION_CALL = 9500
    const val EXTRA_INCOMING_CALL = "com.srltcp.app.INCOMING_CALL"
    const val EXTRA_ANSWER_CALL = "com.srltcp.app.ANSWER_CALL"
    private const val ALERT_ID_BASE = 9000
    private const val RING_MS = 45_000L  // same as the web UI's ring timeout
    private val RING_VIBRATION = longArrayOf(0, 1000, 1000)

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
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Messages, calls and contact requests"
        }
        // Incoming calls ring like a phone call: the phone's own ringtone,
        // repeating, following its ring/vibrate/silent mode.
        val calls = NotificationChannel(
            CHANNEL_CALLS,
            "Incoming calls",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Rings with your phone's ringtone when a contact calls"
            setSound(
                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build()
            )
            enableVibration(true)
            vibrationPattern = RING_VIBRATION
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        }
        // A call under way (while you're in another app): silent, in the
        // notifications, with the call timer and Hang up.
        val ongoing = NotificationChannel(
            CHANNEL_ONGOING,
            "Call in progress",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Shows a call that's going on, to return to it or hang up"
            setShowBadge(false)
            setSound(null, null)
            lockscreenVisibility = Notification.VISIBILITY_PRIVATE
        }
        mgr.createNotificationChannel(ongoing)
        mgr.createNotificationChannel(node)
        mgr.createNotificationChannel(alerts)
        mgr.createNotificationChannel(calls)
    }

    /** Ring for an incoming call until answered, declined, cancelled or 45 s. */
    fun ringIncomingCall(context: Context, caller: String, video: Boolean) {
        ensureChannels(context)
        val open = PendingIntent.getActivity(
            context,
            NOTIFICATION_CALL,
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or
                    Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_INCOMING_CALL, true)
            },
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        // "Answer" opens the app and answers right away (tapping the
        // notification itself only opens the call screen).
        val answer = PendingIntent.getActivity(
            context,
            NOTIFICATION_CALL + 2,
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or
                    Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_INCOMING_CALL, true)
                putExtra(EXTRA_ANSWER_CALL, true)
            },
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val decline = PendingIntent.getService(
            context,
            NOTIFICATION_CALL + 1,
            Intent(context, SRLTCPService::class.java).setAction(SRLTCPService.ACTION_DECLINE_CALL),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        // Private notifications: the call channel shows on the lock screen in
        // full, so the caller's name is left out of the notification itself.
        val private = PrivacyPrefs.privateNotifications(context)
        val shown = if (private) "SRLTCP" else caller
        val builder = NotificationCompat.Builder(context, CHANNEL_CALLS)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(shown.ifBlank { "SRLTCP" })
            .setContentText(if (video) "Incoming video call" else "Incoming voice call")
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setOngoing(true)
            .setAutoCancel(true)
            .setContentIntent(open)
            .setTimeoutAfter(RING_MS)
            // Android 7 and older have no channels: sound set here.
            .setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE), AudioManager.STREAM_RING)
            .setVibrate(RING_VIBRATION)
            .addAction(0, "Decline", decline)
            .addAction(0, "Answer", answer)
        // Shows the call screen (with the caller's name) over the lock screen;
        // with private notifications it rings as a notification only.
        if (!private) builder.setFullScreenIntent(open, true)
        val notification = builder.build()
        notification.flags = notification.flags or Notification.FLAG_INSISTENT  // keep ringing
        try {
            NotificationManagerCompat.from(context).notify(NOTIFICATION_CALL, notification)
        } catch (e: SecurityException) {
            // Notifications turned off for the app: the in-app screen still shows the call.
        }
    }

    /** The call under way: name, voice/video, timer; tap returns, Hang up ends it. */
    fun buildOngoingCall(context: Context, name: String, video: Boolean, startedAt: Long): Notification {
        ensureChannels(context)
        val open = PendingIntent.getActivity(
            context,
            NOTIFICATION_CALL + 3,
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val hangUp = PendingIntent.getService(
            context,
            NOTIFICATION_CALL + 4,
            Intent(context, SRLTCPService::class.java).setAction(SRLTCPService.ACTION_HANGUP_CALL),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val kind = if (video) "Video call" else "Voice call"
        val private = PrivacyPrefs.privateNotifications(context)
        return NotificationCompat.Builder(context, CHANNEL_ONGOING)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(if (private) "SRLTCP" else name.ifBlank { "SRLTCP" })
            .setContentText("$kind · tap to return")
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setUsesChronometer(true)
            .setWhen(startedAt)
            .setShowWhen(true)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setContentIntent(open)
            .addAction(0, "Hang up", hangUp)
            .build()
    }

    fun stopRinging(context: Context) {
        NotificationManagerCompat.from(context).cancel(NOTIFICATION_CALL)
    }

    fun buildNodeNotification(context: Context, text: String): android.app.Notification {
        val launch = PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val quit = PendingIntent.getService(
            context,
            1,
            Intent(context, SRLTCPService::class.java).setAction(SRLTCPService.ACTION_QUIT),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        // Swiping the notification away stops SRLTCP completely (same as Quit).
        // Android 14+ lets the user dismiss a foreground-service notification
        // when it isn't marked ongoing; older versions keep it pinned, so the
        // Quit button stays for them.
        val swipeable = Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
        val hint = if (swipeable) "Swipe away or tap Quit to stop" else "Tap Quit to stop"
        return NotificationCompat.Builder(context, CHANNEL_NODE)
            .setContentTitle("SRLTCP")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText("$text\n$hint"))
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(launch)
            .setDeleteIntent(quit)
            .addAction(0, "Quit", quit)
            .setOngoing(!swipeable)
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
        val builder = NotificationCompat.Builder(context, CHANNEL_ALERTS)
            .setContentTitle(title.ifBlank { "SRLTCP" })
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(launch)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
        if (PrivacyPrefs.privateNotifications(context)) {
            // Locked phone: "SRLTCP — New message" only; unlocked: the full text.
            builder.setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .setPublicVersion(
                    NotificationCompat.Builder(context, CHANNEL_ALERTS)
                        .setSmallIcon(R.drawable.ic_notification)
                        .setContentTitle("SRLTCP")
                        .setContentText("New message")
                        .build()
                )
        }
        NotificationManagerCompat.from(context).notify(id, builder.build())
    }
}