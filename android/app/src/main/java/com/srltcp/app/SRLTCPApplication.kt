package com.srltcp.app

import android.app.Application
import android.content.Context
import android.net.wifi.WifiManager
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class SRLTCPApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        instance = this
        try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(this))
                Log.i(TAG, "Python runtime started")
            }
            val py = Python.getInstance()
            py.getModule("srltcp.utils.platform")
                .callAttr("set_android_data_dir", filesDir.absolutePath)
        } catch (e: Exception) {
            Log.e(TAG, "Python.start failed", e)
            pythonError = e.message ?: e.javaClass.simpleName
        }
    }

    private var multicastLock: WifiManager.MulticastLock? = null

    /**
     * Android filters broadcast/multicast Wi-Fi frames unless an app holds a
     * MulticastLock, which silently breaks UDP LAN discovery on many phones.
     */
    @Synchronized
    fun acquireNetworkLocks() {
        if (multicastLock?.isHeld == true) return
        try {
            val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            multicastLock = wifi?.createMulticastLock("srltcp-discovery")?.apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Multicast lock unavailable — LAN discovery may not work", e)
        }
    }

    @Synchronized
    fun releaseNetworkLocks() {
        try {
            multicastLock?.takeIf { it.isHeld }?.release()
        } catch (_: Exception) { /* ignore */ }
        multicastLock = null
    }

    companion object {
        private const val TAG = "SRLTCP"
        @JvmStatic
        var instance: SRLTCPApplication? = null

        @JvmStatic
        var pythonError: String? = null
    }
}