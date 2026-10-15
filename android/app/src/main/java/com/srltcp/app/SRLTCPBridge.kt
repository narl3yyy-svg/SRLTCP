package com.srltcp.app

import android.webkit.JavascriptInterface
import org.json.JSONObject

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

    /** A call is ringing or running (true) or over (false): lock-screen display, ringing. */
    @JavascriptInterface
    fun setCallScreen(on: Boolean) {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread { activity.setCallScreen(on) }
    }

    /** Stop the ringtone (call answered or declined in the app). */
    @JavascriptInterface
    fun stopRinging() {
        activity.runOnUiThread { SRLTCPNotifier.stopRinging(activity) }
    }

    /** Open the system "Save as" picker for a received/sent file. */
    @JavascriptInterface
    fun saveTransferFile(transferId: String): String =
        if (activity.isTrustedPage) activity.saveTransferFile(transferId) else ""

    /** "Save as" for the encrypted backup just made (Settings → Backup). */
    @JavascriptInterface
    fun saveBackup(filename: String): String =
        if (activity.isTrustedPage) activity.saveBackupFile(filename) else ""

    /** App lock: "on", "off" or "unavailable" (no screen lock set up). */
    @JavascriptInterface
    fun appLockState(): String =
        if (activity.isTrustedPage) activity.appLock?.state() ?: "unavailable" else ""

    /** Turn app lock on (asks to unlock once, to be sure you can) or off;
     *  the result arrives in window.onAppLockChanged. */
    @JavascriptInterface
    fun setAppLock(on: Boolean) {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread {
            val lock = activity.appLock ?: return@runOnUiThread
            lock.setEnabled(on) { state ->
                activity.deliverToWeb("onAppLockChanged", JSONObject.quote(state))
            }
        }
    }

    @JavascriptInterface
    fun privateNotifications(): Boolean =
        activity.isTrustedPage && PrivacyPrefs.privateNotifications(activity)

    /** Lock screen shows "New message" without the sender or the text. */
    @JavascriptInterface
    fun setPrivateNotifications(on: Boolean) {
        if (!activity.isTrustedPage) return
        PrivacyPrefs.setPrivateNotifications(activity, on)
    }

    /** Start the app again (after restoring a backup). */
    @JavascriptInterface
    fun restartApp() {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread { activity.restartApp() }
    }

    /** {"wifi", "captive" (waiting for its sign-in page), "wifi_internet"}. */
    @JavascriptInterface
    fun networkState(): String =
        if (activity.isTrustedPage) NetworkWatch.state(activity).toString() else "{}"

    @JavascriptInterface
    fun openWifiSettings() {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread { activity.openWifiSettings() }
    }

    @JavascriptInterface
    fun shareText(title: String, text: String) {
        if (!activity.isTrustedPage) return
        activity.runOnUiThread { activity.shareText(title, text) }
    }
}
