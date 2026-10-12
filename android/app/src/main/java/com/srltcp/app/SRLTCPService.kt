package com.srltcp.app

import android.app.Service
import android.content.Intent
import android.os.IBinder
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
        } catch (e: Exception) {
            Log.w(TAG, "startForeground failed", e)
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_QUIT) {
            Log.i(TAG, "Quit requested from notification")
            SRLTCPApplication.instance?.releaseNetworkLocks()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            // The Python node runs on a non-daemon thread; end the process cleanly.
            Process.killProcess(Process.myPid())
            return START_NOT_STICKY
        }
        SRLTCPApplication.instance?.ensurePythonServer()
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "SRLTCPService"
        const val ACTION_QUIT = "com.srltcp.app.QUIT"
    }
}
