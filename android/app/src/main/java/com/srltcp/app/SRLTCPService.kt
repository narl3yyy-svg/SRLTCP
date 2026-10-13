package com.srltcp.app

import android.app.Service
import android.content.Intent
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
            startForeground(
                SRLTCPNotifier.NOTIFICATION_NODE,
                SRLTCPNotifier.buildNodeNotification(this, "Running — messages and calls will arrive")
            )
            running = true
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

    override fun onDestroy() {
        running = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "SRLTCPService"
        const val ACTION_QUIT = "com.srltcp.app.QUIT"
        const val ACTION_DECLINE_CALL = "com.srltcp.app.DECLINE_CALL"

        /** True while the service is up in the foreground; lets MainActivity retry a failed start. */
        @Volatile
        var running = false
    }
}
