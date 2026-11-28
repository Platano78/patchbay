package com.patchbay.wear

import org.json.JSONObject
import java.net.URI
import kotlin.math.abs
import kotlin.math.max

/**
 * PatchBay's existing contract, not a new assistant protocol.
 * See ../../../../../../../../docs/contracts/ws-client-protocol.md in the main repository.
 */
object PatchbayProtocol {
    const val SAMPLE_RATE = 16_000
    const val CHANNELS = 1
    const val BYTES_PER_SAMPLE = 2
    const val TURN_END_SILENCE_MS = 2_000

    /**
     * VOICE_RECOGNITION has no AGC (CDD 5.4.2), and at wrist distance quiet speech reached the
     * server's VAD below its 384 ms active-speech floor and was discarded. Apply a fixed +15.6 dB
     * with a tanh soft clip so loud speech (raw peaks ~8500 seen) is rounded, not chopped.
     */
    const val UPLINK_GAIN = 6.0

    fun uplinkGain(sample: Short): Short =
        (32767.0 * kotlin.math.tanh(UPLINK_GAIN * sample / 32767.0)).toInt().toShort()

    fun finishTurnSilence(): ByteArray =
        ByteArray(SAMPLE_RATE * BYTES_PER_SAMPLE * TURN_END_SILENCE_MS / 1_000)

    fun packPcm(samples: ShortArray, count: Int): ByteArray {
        require(count in 0..samples.size)
        val bytes = ByteArray(count * BYTES_PER_SAMPLE)
        for (i in 0 until count) {
            val value = samples[i].toInt()
            bytes[i * 2] = (value and 0xff).toByte()
            bytes[i * 2 + 1] = ((value ushr 8) and 0xff).toByte()
        }
        return bytes
    }

    /** Cheap amplitude approximation for the reactive character; not a speech/affect classifier. */
    fun amplitude(pcm: ByteArray): Float {
        if (pcm.size < 2) return 0f
        var peak = 0
        for (i in 0 until pcm.size - 1 step 2) {
            val sample = (pcm[i].toInt() and 0xff) or (pcm[i + 1].toInt() shl 8)
            peak = max(peak, abs(sample.toShort().toInt()).coerceAtMost(32767))
        }
        return (peak / 16000f).coerceIn(0f, 1f)
    }

    const val DEFAULT_PORT = 8765
    private val BARE_IPV4 = Regex("""^\d{1,3}(\.\d{1,3}){3}(:\d{1,5})?$""")

    /**
     * Accepts a full ws:// or wss:// URL, or -- because typing "ws://" and ":" on a watch
     * keyboard proved impractical on a Galaxy Watch6 Classic -- a bare private IPv4 address
     * with an optional port, which becomes ws://<ip>:<port, default 8765>.
     */
    fun validateEndpoint(input: String): String? {
        val trimmed = input.trim()
        val url = if (BARE_IPV4.matches(trimmed)) {
            if (':' in trimmed) "ws://$trimmed" else "ws://$trimmed:$DEFAULT_PORT"
        } else trimmed
        val uri = try { URI(url) } catch (_: Exception) { return null }
        val scheme = uri.scheme?.lowercase()
        if (scheme != "ws" && scheme != "wss") return null
        val host = uri.host ?: return null
        if (host.isBlank() || uri.rawUserInfo != null || uri.rawQuery != null || uri.rawFragment != null) return null
        if (uri.port != -1 && uri.port !in 1..65535) return null
        if (scheme == "ws" && !isPrivateIpv4(host)) return null
        return url
    }

    /** What the number-pad entry field shows for a saved endpoint: just the address. */
    fun editableAddress(endpoint: String): String {
        val uri = try { URI(endpoint) } catch (_: Exception) { return endpoint }
        if (uri.scheme?.lowercase() != "ws" || uri.host == null || !isPrivateIpv4(uri.host)) return endpoint
        if (uri.rawPath?.let { it.isNotEmpty() && it != "/" } == true) return endpoint
        return if (uri.port == -1 || uri.port == DEFAULT_PORT) uri.host else "${uri.host}:${uri.port}"
    }

    /** True when a saved endpoint cannot be shown as a bare IP (e.g. wss://), so the
     *  entry page must open in full-URL mode. Blank means first setup: IP mode. */
    fun needsFullUrl(endpoint: String): Boolean =
        endpoint.isNotBlank() && editableAddress(endpoint) == endpoint

    private fun isPrivateIpv4(host: String): Boolean {
        val parts = host.split(".")
        if (parts.size != 4) return false
        if (parts.any { it.isEmpty() || it.length > 3 || !it.all(Char::isDigit) }) return false
        val octets = parts.map { it.toIntOrNull() ?: return false }
        if (octets.any { it !in 0..255 }) return false
        return octets[0] == 10 ||
            (octets[0] == 172 && octets[1] in 16..31) ||
            (octets[0] == 192 && octets[1] == 168)
    }

    sealed interface Event {
        data class Transcript(val text: String) : Event
        data class Partial(val delta: String) : Event
        data class Reply(val text: String) : Event
        /** Empty-text assistant_text carrying `tools`: the real answer comes later. */
        data object ToolCall : Event
        data object Configured : Event
        data object Unknown : Event
    }

    /** Unknown and tool-progress messages are harmless, never user-facing as invented replies. */
    fun parseEvent(text: String): Event = try {
        val json = JSONObject(text)
        when (json.optString("type")) {
            "transcription_completed" -> Event.Transcript(json.optString("transcript"))
            "partial_transcription" -> Event.Partial(json.optString("delta"))
            "assistant_text" -> {
                val reply = json.optString("text")
                val tools = json.optJSONArray("tools")
                when {
                    reply.isNotBlank() -> Event.Reply(reply)
                    tools != null && tools.length() > 0 -> Event.ToolCall
                    else -> Event.Unknown
                }
            }
            "config_state", "config_ack" -> Event.Configured
            else -> Event.Unknown
        }
    } catch (_: Exception) {
        Event.Unknown
    }
}

enum class WatchPhase {
    UNCONFIGURED, CONNECTING, READY, LISTENING, WAITING, SPEAKING, OFFLINE, ERROR
}

data class WatchUiState(
    val phase: WatchPhase = WatchPhase.UNCONFIGURED,
    val endpoint: String = "",
    val connected: Boolean = false,
    val transcript: String = "",
    val partialTranscript: String = "",
    val lastReply: String = "",
    val audioLevel: Float = 0f,
    val detail: String = "Set the PatchBay address in Settings"
)

/**
 * Per-turn capture levels for field diagnosis: numbers only, never audio. Separates a quiet
 * speaker from a clipping one, and keeps the stop tap (which lands in the last few hundred ms
 * and can reach full scale on its own) out of the speech figures.
 */
class CaptureStats {
    private val windows = ArrayList<Int>()          // pre-gain RMS per 200 ms window
    private var winSq = 0.0
    private var winN = 0
    private var samples = 0L
    private var rawClipped = 0L                    // pre-gain samples at the int16 rail
    private var postSaturated = 0L                 // post-gain samples at >= 97.7% of full scale

    fun add(raw: Short, sent: Short) {
        val v = raw.toInt()
        samples++
        if (v >= 32_760 || v <= -32_760) rawClipped++
        if (sent >= 32_000 || sent <= -32_000) postSaturated++
        winSq += v.toDouble() * v
        if (++winN == WINDOW) {
            windows.add(kotlin.math.sqrt(winSq / WINDOW).toInt())
            winSq = 0.0; winN = 0
        }
    }

    /** Windows that can contain speech: all but the last [TAP_WINDOWS] (the stop tap). */
    fun speechWindows(): List<Int> = windows.dropLast(TAP_WINDOWS)

    fun summary(): String {
        val speech = speechWindows()
        val peakWin = speech.maxOrNull() ?: 0
        val median = speech.sorted().let { if (it.isEmpty()) 0 else it[it.size / 2] }
        val satPct = if (samples > 0) 100.0 * postSaturated / samples else 0.0
        return "capture: secs=%.2f speechPeakWinRms=%d speechMedianWinRms=%d rawClipped=%d postSaturated=%.2f%% env200ms=%s"
            .format(samples / PatchbayProtocol.SAMPLE_RATE.toFloat(), peakWin, median, rawClipped, satPct,
                windows.joinToString(","))
    }

    companion object {
        const val WINDOW = 3_200   // 200 ms at 16 kHz
        const val TAP_WINDOWS = 2  // last 400 ms
    }
}
