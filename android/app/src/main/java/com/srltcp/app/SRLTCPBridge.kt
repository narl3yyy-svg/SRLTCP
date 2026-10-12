package com.srltcp.app

import android.webkit.JavascriptInterface

/**
 * Methods exposed to the local web UI as `window.SRLTCPAndroid`. Sensitive
 * calls are ignored unless the WebView shows our own https://127.0.0.1 page.
 */
class SRLTCPBridge(private val activity: MainActivity) {
    @JavascriptInterface
    fun showNotification(title: String, body: String, tag: String) {
        if (!activity.isTrustedPage) return
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
        if (!activity.isTrustedPage) return
        NfcPairing.card = uri.ifBlank { null }
    }

    @JavascriptInterface
    fun isNfcAvailable(): Boolean = activity.isNfcAvailable()

    /** Start automatic tap-to-pair; returns "ok", "off" or "missing". */
    @JavascriptInterface
    fun startNfcScan(): String = if (activity.isTrustedPage) activity.startNfcReader() else ""

    @JavascriptInterface
    fun openNfcSettings() {
        activity.runOnUiThread { activity.openNfcSettings() }
    }

    @JavascriptInterface
    fun stopNfcScan() {
        activity.runOnUiThread { activity.stopNfcReader() }
    }

    @JavascriptInterface
    fun scanQrCode() {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread { activity.launchQrScanner() }
    }

    /** Route call audio like a phone call; speaker=false uses the earpiece/headset. */
    @JavascriptInterface
    fun setCallAudio(active: Boolean, speaker: Boolean) {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread { activity.setCallAudio(active, speaker) }
    }

    /** Open the system "Save as" picker for a received/sent file. */
    @JavascriptInterface
    fun saveTransferFile(transferId: String): String =
        if (activity.isTrustedPage) activity.saveTransferFile(transferId) else ""

    @JavascriptInterface
    fun shareText(title: String, text: String) {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread { activity.shareText(title, text) }
    }
}
