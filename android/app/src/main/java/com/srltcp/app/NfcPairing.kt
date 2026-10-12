package com.srltcp.app

import android.nfc.Tag
import android.nfc.tech.IsoDep
import android.util.Log
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/**
 * Tap-to-pair over NFC.
 *
 * One phone (the one showing the Pair screen) answers as a contactless card via
 * [PairingApduService]; the other phone uses reader mode and talks to it with
 * ISO 7816 APDUs. In one tap the reader writes its own contact card and reads
 * the other phone's card, so both sides end up trusting each other.
 *
 * APDUs (all with our private AID selected first):
 *   80 10 <seq> <last> Lc <data>  PUT a chunk of the reader's card
 *   80 20 <seq> 00               GET chunk <seq> of the card owner's card
 *                                 -> data + 9000 (last chunk) / 6310 (more)
 */
object NfcPairing {
    private const val TAG = "SRLTCP-NFC"

    /** F0 + "SRLTCP" + 01 — a proprietary AID (see res/xml/apduservice.xml). */
    val AID: ByteArray = byteArrayOf(
        0xF0.toByte(), 0x53, 0x52, 0x4C, 0x54, 0x43, 0x50, 0x01
    )
    const val CHUNK = 200

    val SW_OK = byteArrayOf(0x90.toByte(), 0x00)
    val SW_MORE = byteArrayOf(0x63, 0x10)
    val SW_NOT_READY = byteArrayOf(0x6A, 0x82.toByte())
    val SW_BAD = byteArrayOf(0x6D, 0x00)

    /** Our contact card while the Pair screen is open; null disables NFC answers. */
    @Volatile
    var card: String? = null

    /** Receives the result JSON (paired peer or error) for the WebView. */
    @Volatile
    var listener: ((String) -> Unit)? = null

    fun selectApdu(): ByteArray {
        val header = byteArrayOf(0x00, 0xA4.toByte(), 0x04, 0x00, AID.size.toByte())
        return header + AID + byteArrayOf(0x00)
    }

    fun chunks(data: ByteArray): List<ByteArray> {
        if (data.isEmpty()) return listOf(ByteArray(0))
        return (data.indices step CHUNK).map { data.copyOfRange(it, minOf(it + CHUNK, data.size)) }
    }

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
            ?: return JSONObject().put("error", "not an SRLTCP phone").toString()
        return try {
            iso.connect()
            iso.timeout = 5000
            val sel = iso.transceive(selectApdu())
            if (!sel.endsWith(SW_OK)) {
                return JSONObject().put(
                    "error", "other phone is not on the SRLTCP Pair screen"
                ).toString()
            }
            val parts = chunks(own.toByteArray(Charsets.UTF_8))
            parts.forEachIndexed { i, part ->
                val last = if (i == parts.size - 1) 1 else 0
                val apdu = byteArrayOf(0x80.toByte(), 0x10, i.toByte(), last.toByte(), part.size.toByte()) + part
                if (!iso.transceive(apdu).endsWith(SW_OK)) {
                    return JSONObject().put("error", "other phone rejected our card").toString()
                }
            }
            val out = ByteArrayOutputStream()
            var seq = 0
            while (seq < 64) {
                val resp = iso.transceive(byteArrayOf(0x80.toByte(), 0x20, seq.toByte(), 0x00, 0x00))
                if (resp.size < 2) break
                out.write(resp, 0, resp.size - 2)
                if (resp.endsWith(SW_OK)) break
                if (!resp.endsWith(SW_MORE)) {
                    return JSONObject().put("error", "transfer interrupted").toString()
                }
                seq++
            }
            importCard(out.toString("UTF-8"))
        } catch (e: Exception) {
            Log.w(TAG, "NFC exchange failed", e)
            JSONObject().put("error", "hold the phones together a bit longer").toString()
        } finally {
            try { iso.close() } catch (_: Exception) { }
        }
    }

    private fun ByteArray.endsWith(sw: ByteArray): Boolean =
        size >= 2 && this[size - 2] == sw[0] && this[size - 1] == sw[1]
}
