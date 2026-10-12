package com.srltcp.app

import java.io.ByteArrayOutputStream

/**
 * NFC pairing APDU protocol, free of Android dependencies so it can be unit tested.
 *
 *   00 A4 04 00 Lc <AID> 00        SELECT our AID            -> 9000 / 6A82 (not ready)
 *   80 10 <seq> <last> Lc <data>   PUT chunk of reader's card -> 9000
 *   80 20 <seq> 00 00              GET chunk <seq> of card    -> data + 9000 (last) / 6310 (more)
 */
object PairingProtocol {
    /** F0 + "SRLTCP" + 01 — a proprietary AID (must match res/xml/apduservice.xml). */
    val AID: ByteArray = byteArrayOf(0xF0.toByte(), 0x53, 0x52, 0x4C, 0x54, 0x43, 0x50, 0x01)
    const val CHUNK = 200
    private const val MAX_CARD = 8192

    val SW_OK = byteArrayOf(0x90.toByte(), 0x00)
    val SW_MORE = byteArrayOf(0x63, 0x10)
    val SW_NOT_READY = byteArrayOf(0x6A, 0x82.toByte())
    val SW_BAD = byteArrayOf(0x6D, 0x00)

    fun selectApdu(): ByteArray =
        byteArrayOf(0x00, 0xA4.toByte(), 0x04, 0x00, AID.size.toByte()) + AID + byteArrayOf(0x00)

    fun chunks(data: ByteArray): List<ByteArray> {
        if (data.isEmpty()) return listOf(ByteArray(0))
        return (data.indices step CHUNK).map { data.copyOfRange(it, minOf(it + CHUNK, data.size)) }
    }

    fun ByteArray.endsWith(sw: ByteArray): Boolean =
        size >= 2 && this[size - 2] == sw[0] && this[size - 1] == sw[1]

    sealed class Result {
        data class Card(val uri: String) : Result()
        /** Other phone not answering as a card right now (e.g. in its reader phase). */
        object NotReady : Result()
        data class Failed(val reason: String) : Result()
    }

    /** Reader side: send our card, read theirs. [transceive] talks to the other phone. */
    fun exchange(ownCard: String, transceive: (ByteArray) -> ByteArray): Result {
        if (!transceive(selectApdu()).endsWith(SW_OK)) return Result.NotReady
        val parts = chunks(ownCard.toByteArray(Charsets.UTF_8))
        if (parts.size > 255) return Result.Failed("card too large")
        parts.forEachIndexed { i, part ->
            val last = if (i == parts.size - 1) 1 else 0
            val apdu = byteArrayOf(0x80.toByte(), 0x10, i.toByte(), last.toByte(), part.size.toByte()) + part
            if (!transceive(apdu).endsWith(SW_OK)) return Result.Failed("other phone rejected our card")
        }
        val out = ByteArrayOutputStream()
        for (seq in 0 until 64) {
            val resp = transceive(byteArrayOf(0x80.toByte(), 0x20, seq.toByte(), 0x00, 0x00))
            if (resp.size < 2) return Result.Failed("transfer interrupted")
            out.write(resp, 0, resp.size - 2)
            if (resp.endsWith(SW_OK)) return Result.Card(out.toString("UTF-8"))
            if (!resp.endsWith(SW_MORE)) return Result.Failed("transfer interrupted")
        }
        return Result.Failed("card too large")
    }

    /** Card side state machine (one instance per HCE service). */
    class CardEmulator(
        private val cardProvider: () -> String?,
        private val onCardReceived: (String) -> Unit,
    ) {
        private var selected = false
        private val incoming = ByteArrayOutputStream()
        private var outgoing: List<ByteArray> = emptyList()

        fun reset() {
            selected = false
            incoming.reset()
        }

        fun process(apdu: ByteArray): ByteArray {
            if (apdu.size < 4) return SW_BAD
            val cla = apdu[0].toInt() and 0xFF
            val ins = apdu[1].toInt() and 0xFF
            val p1 = apdu[2].toInt() and 0xFF
            val p2 = apdu[3].toInt() and 0xFF
            if (cla == 0x00 && ins == 0xA4) {
                val lc = if (apdu.size > 4) apdu[4].toInt() and 0xFF else 0
                if (apdu.size < 5 + lc || !apdu.copyOfRange(5, 5 + lc).contentEquals(AID)) {
                    return SW_NOT_READY
                }
                val card = cardProvider() ?: return SW_NOT_READY
                selected = true
                incoming.reset()
                outgoing = chunks(card.toByteArray(Charsets.UTF_8))
                return SW_OK
            }
            if (!selected || cla != 0x80) return SW_BAD
            return when (ins) {
                0x10 -> {
                    if (apdu.size < 5) return SW_BAD
                    val len = apdu[4].toInt() and 0xFF
                    if (apdu.size < 5 + len || incoming.size() + len > MAX_CARD) return SW_BAD
                    incoming.write(apdu, 5, len)
                    if (p2 == 1) onCardReceived(incoming.toString("UTF-8"))
                    SW_OK
                }
                0x20 -> {
                    if (p1 >= outgoing.size) return SW_BAD
                    outgoing[p1] + if (p1 == outgoing.size - 1) SW_OK else SW_MORE
                }
                else -> SW_BAD
            }
        }
    }
}
