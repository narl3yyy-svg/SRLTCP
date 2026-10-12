package com.srltcp.app

import android.nfc.cardemulation.HostApduService
import android.os.Bundle
import java.io.ByteArrayOutputStream

/** Card side of NFC pairing — only answers while the Pair screen is open. */
class PairingApduService : HostApduService() {
    private var selected = false
    private val incoming = ByteArrayOutputStream()
    private var outgoing: List<ByteArray> = emptyList()

    override fun processCommandApdu(apdu: ByteArray, extras: Bundle?): ByteArray {
        if (apdu.size < 4) return NfcPairing.SW_BAD
        val cla = apdu[0].toInt() and 0xFF
        val ins = apdu[1].toInt() and 0xFF
        val p1 = apdu[2].toInt() and 0xFF
        val p2 = apdu[3].toInt() and 0xFF

        if (cla == 0x00 && ins == 0xA4) {
            val card = NfcPairing.card ?: return NfcPairing.SW_NOT_READY
            selected = true
            incoming.reset()
            outgoing = NfcPairing.chunks(card.toByteArray(Charsets.UTF_8))
            return NfcPairing.SW_OK
        }
        if (!selected || cla != 0x80) return NfcPairing.SW_BAD

        return when (ins) {
            0x10 -> {
                if (apdu.size < 5) return NfcPairing.SW_BAD
                val len = apdu[4].toInt() and 0xFF
                if (apdu.size < 5 + len || incoming.size() > 8192) return NfcPairing.SW_BAD
                incoming.write(apdu, 5, len)
                if (p2 == 1) {
                    val uri = incoming.toString("UTF-8")
                    Thread {
                        val result = NfcPairing.importCard(uri)
                        NfcPairing.listener?.invoke(result)
                    }.start()
                }
                NfcPairing.SW_OK
            }
            0x20 -> {
                if (p1 >= outgoing.size) return NfcPairing.SW_BAD
                val sw = if (p1 == outgoing.size - 1) NfcPairing.SW_OK else NfcPairing.SW_MORE
                outgoing[p1] + sw
            }
            else -> NfcPairing.SW_BAD
        }
    }

    override fun onDeactivated(reason: Int) {
        selected = false
        incoming.reset()
    }
}
