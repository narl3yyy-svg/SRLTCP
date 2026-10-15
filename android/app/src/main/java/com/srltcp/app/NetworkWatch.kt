package com.srltcp.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import org.json.JSONObject

/**
 * What Android itself knows about the Wi-Fi: waiting for its sign-in page
 * (a "captive portal": planes, hotels, cafés), or connected without working
 * internet. The app sends nothing to find out; Android checks networks anyway.
 */
object NetworkWatch {
    fun state(context: Context): JSONObject {
        val out = JSONObject()
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return out
        var captive = false
        var wifi = false
        var wifiInternet = false
        @Suppress("DEPRECATION")
        for (net in cm.allNetworks) {
            val caps = cm.getNetworkCapabilities(net) ?: continue
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
            wifi = true
            if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_CAPTIVE_PORTAL)) captive = true
            if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) wifiInternet = true
        }
        out.put("wifi", wifi)
        out.put("captive", captive)
        out.put("wifi_internet", wifiInternet)
        return out
    }

    /** Calls [onChange] (on a binder thread) whenever a Wi-Fi network changes. */
    fun watch(context: Context, onChange: () -> Unit): ConnectivityManager.NetworkCallback? {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = onChange()
            override fun onLost(network: Network) = onChange()
        }
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            .build()
        return try {
            cm.registerNetworkCallback(request, callback)
            callback
        } catch (_: Exception) {
            null
        }
    }

    fun unwatch(context: Context, callback: ConnectivityManager.NetworkCallback?) {
        callback ?: return
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return
        try {
            cm.unregisterNetworkCallback(callback)
        } catch (_: Exception) {
        }
    }
}
