package com.srltcp.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingProtocolTest {
    private fun card(name: String, size: Int) =
        "srltcp://pair#" + name + "x".repeat(size)

    @Test
    fun exchangeSwapsBothCards() {
        val readerCard = card("reader", 380)   // two chunks
        val phoneCard = card("phone", 650)     // four chunks
        var received: String? = null
        val emulator = PairingProtocol.CardEmulator({ phoneCard }, { received = it })

        val result = PairingProtocol.exchange(readerCard) { emulator.process(it) }

        assertEquals(PairingProtocol.Result.Card(phoneCard), result)
        assertEquals(readerCard, received)
    }

    @Test
    fun notReadyWhenPairScreenClosed() {
        val emulator = PairingProtocol.CardEmulator({ null }, { })
        val result = PairingProtocol.exchange(card("r", 10)) { emulator.process(it) }
        assertTrue(result is PairingProtocol.Result.NotReady)
    }

    @Test
    fun rejectsForeignAidAndCommandsBeforeSelect() {
        val emulator = PairingProtocol.CardEmulator({ "card" }, { })
        val otherAid = byteArrayOf(0x00, 0xA4.toByte(), 0x04, 0x00, 0x02, 0x01, 0x02, 0x00)
        assertTrue(emulator.process(otherAid).contentEquals(PairingProtocol.SW_NOT_READY))
        val get = byteArrayOf(0x80.toByte(), 0x20, 0x00, 0x00, 0x00)
        assertTrue(emulator.process(get).contentEquals(PairingProtocol.SW_BAD))
    }

    @Test
    fun singleChunkCard() {
        val emulator = PairingProtocol.CardEmulator({ "srltcp://pair#short" }, { })
        val result = PairingProtocol.exchange("srltcp://pair#mine") { emulator.process(it) }
        assertEquals(PairingProtocol.Result.Card("srltcp://pair#short"), result)
    }
}
