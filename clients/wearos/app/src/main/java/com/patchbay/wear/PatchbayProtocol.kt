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

    fun validateEndpoint(input: String): String? {
        val url = input.trim()
        val uri = try { URI(url) } catch (_: Exception) { return null }
        val scheme = uri.scheme?.lowercase()
        if (scheme != "ws" && scheme != "wss") return null
        val host = uri.host ?: return null
        if (host.isBlank() || uri.rawUserInfo != null || uri.rawQuery != null || uri.rawFragment != null) return null
        if (uri.port != -1 && uri.port !in 1..65535) return null
        if (scheme == "ws" && !isPrivateIpv4(host)) return null
        return url
    }

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
