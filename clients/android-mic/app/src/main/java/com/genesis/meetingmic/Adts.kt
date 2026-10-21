package com.genesis.meetingmic

/**
 * Raw ADTS framing for AAC-LC, 16 kHz mono.
 *
 * Each frame carries its own 7-byte header with its length, so a file written frame by frame is
 * self-delimiting: a crash or a full disk loses at most the torn last frame, never the file (unlike
 * MP4, whose index is written only at the end). Speech transcription services accept raw ADTS `.aac`
 * directly, so the same files serve as the local backup and as gap-fill uploads.
 */
object Adts {
    const val HEADER_BYTES = 7
    const val SAMPLES_PER_FRAME = 1024
    private const val PROFILE_LC = 1          // audio object type 2 (AAC-LC) minus 1
    private const val FREQ_INDEX_16K = 8      // ADTS sampling-frequency index for 16000 Hz
    private const val CHANNELS_MONO = 1

    /** The 7-byte header (no CRC) for a frame whose raw AAC payload is [payloadBytes] long. */
    fun header(payloadBytes: Int): ByteArray {
        val len = payloadBytes + HEADER_BYTES
        require(len < (1 shl 13)) { "ADTS frame too large: $len" }
        return byteArrayOf(
            0xFF.toByte(),
            0xF1.toByte(),  // MPEG-4, layer 0, no CRC
            ((PROFILE_LC shl 6) or (FREQ_INDEX_16K shl 2) or (CHANNELS_MONO shr 2)).toByte(),
            (((CHANNELS_MONO and 3) shl 6) or (len shr 11)).toByte(),
            ((len shr 3) and 0xFF).toByte(),
            (((len and 7) shl 5) or 0x1F).toByte(),
            0xFC.toByte(),
        )
    }

    /**
     * Byte offsets of the complete frames in [data], in order. Stops at the first byte that is not a
     * valid frame start or at a torn last frame, so readers never pass on a partial frame.
     */
    fun frameOffsets(data: ByteArray): List<Int> {
        val out = ArrayList<Int>()
        var i = 0
        while (i + HEADER_BYTES <= data.size) {
            if ((data[i].toInt() and 0xFF) != 0xFF || (data[i + 1].toInt() and 0xF6) != 0xF0) break
            val len = ((data[i + 3].toInt() and 0x03) shl 11) or
                ((data[i + 4].toInt() and 0xFF) shl 3) or
                ((data[i + 5].toInt() and 0xE0) shr 5)
            if (len < HEADER_BYTES || i + len > data.size) break
            out.add(i)
            i += len
        }
        return out
    }
}
