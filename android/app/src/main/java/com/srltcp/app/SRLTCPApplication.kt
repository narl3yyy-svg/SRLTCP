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
                Log.i(TAG, "Python runtime started")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Python.start failed", e)
            pythonError = e.message ?: e.javaClass.simpleName
        }
    }

    companion object {
        private const val TAG = "SRLTCP"
        @JvmStatic
        var pythonError: String? = null
    }
}