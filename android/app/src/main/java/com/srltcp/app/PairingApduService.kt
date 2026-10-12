package com.srltcp.app

import android.nfc.cardemulation.HostApduService
import android.os.Bundle

/** Card side of NFC pairing — only answers while the Pair screen is open. */
class PairingApduService : HostApduService() {
    private val emulator = PairingProtocol.CardEmulator(
        cardProvider = { if (NfcPairing.active) NfcPairing.card else null },
        onCardReceived = { uri ->
            Thread {
                val result = NfcPairing.importCard(uri)
                NfcPairing.listener?.invoke(result)
            }.start()
        },
    )

    override fun processCommandApdu(apdu: ByteArray, extras: Bundle?): ByteArray =
        emulator.process(apdu)

    override fun onDeactivated(reason: Int) {
        emulator.reset()
    }
}
