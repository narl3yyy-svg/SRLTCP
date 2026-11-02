package com.srltcp.app

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Process
import android.util.Log

/**
 * The built-in Tor (tor-android's TorService) runs in its own process
 * (":tor", see AndroidManifest.xml): Tor can be started only once per process,
 * so a Tor that stopped or failed is replaced by ending that process; the
 * next start is a fresh Tor. Called from Python (srltcp/core/messaging/tor.py).
 */
object TorProcess {
    private const val TAG = "SRLTCP"
    const val SUFFIX = ":tor"

    @JvmStatic
    fun reset(ctx: Context) {
        try {
            ctx.stopService(Intent().setClassName(ctx.packageName, "org.torproject.jni.TorService"))
        } catch (e: Exception) {
            Log.w(TAG, "Stopping the built-in Tor failed", e)
        }
        val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager ?: return
        for (p in am.runningAppProcesses.orEmpty()) {
            if (p.processName == ctx.packageName + SUFFIX && p.pid != Process.myPid()) {
                Log.i(TAG, "Ending the built-in Tor's process for a fresh start")
                Process.killProcess(p.pid)
            }
        }
    }

    /** True in the ":tor" process (where the app itself doesn't start). */
    @JvmStatic
    fun isTorProcess(ctx: Context): Boolean {
        val name = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            android.app.Application.getProcessName()
        } else {
            try {
                java.io.File("/proc/self/cmdline").readText().trimEnd('\u0000')
            } catch (e: Exception) {
                ""
            }
        }
        return name.endsWith(SUFFIX)
    }
}
