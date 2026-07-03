package com.srltcp.app

import android.app.Application
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class SRLTCPApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(this))
            }
            Thread {
                try {
                    Python.getInstance().getModule("srltcp.app")
                } catch (e: Exception) {
                    Log.e(TAG, "Python module preload failed", e)
                }
            }.start()
        } catch (e: Exception) {
            Log.e(TAG, "Python.start failed", e)
        }
    }

    companion object {
        private const val TAG = "SRLTCP"
    }
}