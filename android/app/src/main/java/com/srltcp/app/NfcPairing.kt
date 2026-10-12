package com.srltcp.app

import android.nfc.Tag
import android.nfc.tech.IsoDep
import android.util.Log
import com.chaquo.python.Python
import org.json.JSONObject

/**
 * Tap-to-pair over NFC: one phone answers as a contactless card via
 * [PairingApduService], the other reads it with ISO-DEP. In one tap the reader
 * writes its own contact card and reads the other phone's, so both sides end up
 * trusting each other. Protocol details: [PairingProtocol].
 */
object NfcPairing {
    private const val TAG = "SRLTCP-NFC"

    /** Our contact card while the Pair screen is open; null disables NFC answers. */
    @Volatile
    var card: String? = null

    /** True only while MainActivity is resumed; the card is not served otherwise. */
    @Volatile
    var active = false

    /** Receives the result JSON (paired peer or error) for the WebView. */
    @Volatile
    var listener: ((String) -> Unit)? = null

    /** Hand a received card to the Python node; returns result JSON for the UI. */
    fun importCard(uri: String): String {
        return try {
            Python.getInstance().getModule("srltcp.app")
                .callAttr("android_import_pairing_card", uri)
                .toString()
        } catch (e: Exception) {
            Log.e(TAG, "Import failed", e)
            JSONObject().put("error", e.message ?: "import failed").toString()
        }
    }

    /** Reader side: exchange cards with a phone running [PairingApduService]. */
    fun exchangeAsReader(tag: Tag): String {
        val own = card ?: return JSONObject().put("error", "open the Pair screen first").toString()
        val iso = IsoDep.get(tag)
            ?: return JSONObject().put("error", "not an SRLTCP phone").put("retry", true).toString()
        return try {
            iso.connect()
            iso.timeout = 5000
            when (val r = PairingProtocol.exchange(own) { apdu -> iso.transceive(apdu) }) {
                is PairingProtocol.Result.Card -> importCard(r.uri)
                is PairingProtocol.Result.NotReady -> JSONObject()
                    .put("error", "other phone is not on the SRLTCP Pair screen")
                    .put("retry", true).toString()
                is PairingProtocol.Result.Failed -> JSONObject().put("error", r.reason).toString()
            }
        } catch (e: Exception) {
            Log.w(TAG, "NFC exchange failed", e)
            JSONObject().put("error", "hold the phones together a bit longer")
                .put("retry", true).toString()
        } finally {
            try { iso.close() } catch (_: Exception) { }
        }
    }
}
