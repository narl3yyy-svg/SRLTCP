package com.srltcp.app

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.Process
import android.util.Log

/**
 * Keeps the Python SRLTCP node alive in the background (foreground service) so
 * messages and calls keep arriving after the UI is closed. Also starts the
 * node itself, so it survives Android restarting the service.
 */
class SRLTCPService : Service() {
    override fun onCreate() {
        super.onCreate()
        SRLTCPNotifier.ensureChannels(this)
        SRLTCPApplication.instance?.ensurePythonServer()
        try {
            // (Only "data sync" here: the microphone/camera types are added for
            // a call, while the app is open, as Android requires.)
            ServiceCompat.startForeground(
                this, SRLTCPNotifier.NOTIFICATION_NODE,
                SRLTCPNotifier.buildNodeNotification(this, "Running — messages and calls will arrive"),
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                } else 0
            )
            running = true
            instance = this
            if (call.active) applyCall()
        } catch (e: Exception) {
            Log.w(TAG, "startForeground failed", e)
            running = false
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_QUIT) {
            // Quit button, or the notification swiped away (Android 14+).
            Log.i(TAG, "Quit requested from notification")
            SRLTCPApplication.instance?.releaseNetworkLocks()
            running = false
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            // Close the screen first so Android doesn't bring the app back up,
            // then end the process: the Python node runs on a non-daemon thread.
            val activity = MainActivity.current?.get()
            if (activity != null && !activity.isFinishing) {
                activity.runOnUiThread { activity.finishAndRemoveTask() }
                Handler(Looper.getMainLooper()).postDelayed({ Process.killProcess(Process.myPid()) }, 300)
            } else {
                Process.killProcess(Process.myPid())
            }
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_HANGUP_CALL) {
            // "Hang up" on the call notification: the call lives in the page.
            MainActivity.current?.get()?.deliverToWeb("onAndroidHangUp", "")
            return START_STICKY
        }
        if (intent?.action == ACTION_DECLINE_CALL) {
            SRLTCPNotifier.stopRinging(this)
            // Tell the caller (from Python, off the main thread).
            Thread {
                try {
                    com.chaquo.python.Python.getInstance().getModule("srltcp.app")
                        .callAttr("android_decline_call")
                } catch (e: Exception) {
                    Log.w(TAG, "Declining the call failed", e)
                }
            }.start()
            return START_STICKY
        }
        SRLTCPApplication.instance?.ensurePythonServer()
        return START_STICKY
    }

    /**
     * During a call this service's notification is the call (name, timer,
     * Hang up), and it tells Android the microphone (and camera, for video)
     * stay in use: otherwise leaving the app would silence you.
     */
    private fun applyCall() {
        val c = call
        try {
            if (c.active) {
                var types = 0
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    types = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                    if (c.video) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
                }
                ServiceCompat.startForeground(
                    this, SRLTCPNotifier.NOTIFICATION_NODE,
                    SRLTCPNotifier.buildOngoingCall(this, c.name, c.video, c.startedAt), types
                )
            } else {
                ServiceCompat.startForeground(
                    this, SRLTCPNotifier.NOTIFICATION_NODE,
                    SRLTCPNotifier.buildNodeNotification(this, "Running — messages and calls will arrive"),
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                    } else 0
                )
            }
        } catch (e: Exception) {
            // (Camera/microphone not allowed in the background on this phone:
            // the call still works while the app is open.)
            Log.w(TAG, "Call foreground mode not applied", e)
        }
    }

    override fun onDestroy() {
        instance = null
        running = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "SRLTCPService"
        const val ACTION_QUIT = "com.srltcp.app.QUIT"
        const val ACTION_DECLINE_CALL = "com.srltcp.app.DECLINE_CALL"
        const val ACTION_HANGUP_CALL = "com.srltcp.app.HANGUP_CALL"

        data class CallState(
            val active: Boolean = false,
            val video: Boolean = false,
            val name: String = "",
            val startedAt: Long = 0L,
        )

        @Volatile
        private var call = CallState()

        @Volatile
        private var instance: SRLTCPService? = null

        /** A call started, changed (voice/video) or ended. Called while the app is open. */
        fun setCall(context: Context, active: Boolean, video: Boolean, name: String) {
            val old = call
            if (!active && !old.active) return
            // (Applied again on every update, not only on changes: the first
            // try can come before the microphone permission is granted.)
            call = CallState(
                active, video, name,
                if (active && old.active) old.startedAt else System.currentTimeMillis(),
            )
            val svc = instance
            if (svc != null) {
                svc.applyCall()
            } else if (active) {
                try {
                    ContextCompat.startForegroundService(context, Intent(context, SRLTCPService::class.java))
                } catch (e: Exception) {
                    Log.w(TAG, "Foreground service not started for the call", e)
                }
            }
        }

        /** True while the service is up in the foreground; lets MainActivity retry a failed start. */
        @Volatile
        var running = false
    }
}
