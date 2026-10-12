package com.srltcp.app

import android.webkit.JavascriptInterface

/** Methods exposed to the local web UI as `window.SRLTCPAndroid`. */
class SRLTCPBridge(private val activity: MainActivity) {
    @JavascriptInterface
    fun showNotification(title: String, body: String, tag: String) {
        activity.runOnUiThread {
            activity.postAlertNotification(title, body, tag)
        }
    }

    @JavascriptInterface
    fun isInBackground(): Boolean {
        return !activity.isActivityResumed
    }

    /** Card served over NFC while the Pair screen is open ("" = stop answering). */
    @JavascriptInterface
    fun setPairingCard(uri: String) {
        NfcPairing.card = uri.ifBlank { null }
    }

    @JavascriptInterface
    fun isNfcAvailable(): Boolean = activity.isNfcAvailable()

    @JavascriptInterface
    fun startNfcScan(): Boolean = activity.startNfcReader()

    @JavascriptInterface
    fun stopNfcScan() {
        activity.runOnUiThread { activity.stopNfcReader() }
    }

    @JavascriptInterface
    fun scanQrCode() {
        activity.runOnUiThread { activity.launchQrScanner() }
    }

    @JavascriptInterface
    fun shareText(title: String, text: String) {
        activity.runOnUiThread { activity.shareText(title, text) }
    }
}
