package com.patchbay.wear

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.max
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Hardware I/O only. This watch does not transcribe, reason, or synthesize.
 * Foreground push-to-talk mic capture and continuous 16kHz PCM speaker output.
 */
class WatchMicrophone(private val scope: CoroutineScope) {
    private var recorder: AudioRecord? = null
    private var captureJob: Job? = null
    private val running = AtomicBoolean(false)

    fun start(onPcm: (ByteArray) -> Unit, onLevel: (Float) -> Unit, onError: (String) -> Unit): Boolean {
        if (running.get()) return false
        val format = AudioFormat.Builder()
            .setSampleRate(PatchbayProtocol.SAMPLE_RATE)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
            .build()
        try {
            val min = AudioRecord.getMinBufferSize(
                PatchbayProtocol.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            if (min <= 0) throw IllegalStateException("Watch microphone does not accept 16kHz PCM")
            val source = AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                .setAudioFormat(format)
                .setBufferSizeInBytes(max(min * 2, 6400))
                .build()
            if (source.state != AudioRecord.STATE_INITIALIZED) {
                source.release()
                throw IllegalStateException("Watch microphone could not initialize")
            }
            source.startRecording()
            if (source.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                source.release()
                throw IllegalStateException("Watch microphone did not start")
            }
            recorder = source
            running.set(true)
            captureJob = scope.launch(Dispatchers.IO) {
                val buf = ShortArray(640) // 40 ms
                try {
                    while (isActive && running.get()) {
                        val count = source.read(buf, 0, buf.size, AudioRecord.READ_BLOCKING)
                        if (count < 0) {
                            if (running.get()) onError("Microphone read failed: $count")
                            break
                        }
                        if (count > 0 && running.get()) {
                            val pcm = PatchbayProtocol.packPcm(buf, count)
                            onPcm(pcm)
                            onLevel(PatchbayProtocol.amplitude(pcm))
                        }
                    }
                } catch (t: Exception) {
                    if (running.get()) onError("Microphone error: ${t.message}")
                }
            }
            return true
        } catch (t: Exception) {
            running.set(false)
            releaseNow()
            onError(t.message ?: "Microphone unavailable")
            return false
        }
    }

    /** Stop capture, wait for final sent audio, THEN let the controller send VAD silence. */
    suspend fun stopAndDrain() {
        running.set(false)
        val source = recorder
        try { source?.stop() } catch (_: Exception) {}
        captureJob?.join()
        try { source?.release() } catch (_: Exception) {}
        captureJob = null
        recorder = null
    }

    /** No waiting for UI lifecycle: immediately stop the microphone when leaving foreground. */
    fun stopImmediately() {
        running.set(false)
        val source = recorder
        recorder = null
        try { source?.stop() } catch (_: Exception) {}
        captureJob?.cancel()
        captureJob = null
        try { source?.release() } catch (_: Exception) {}
    }

    private fun releaseNow() {
        try { recorder?.release() } catch (_: Exception) {}
        recorder = null
    }
}

/**
 * Buffered gapless playback across arbitrary WebSocket frame boundaries.
 * The bounded queue fails rather than storing many seconds of unexpected audio.
 */
class WatchSpeaker(
    private val scope: CoroutineScope,
    private val onLevel: (Float) -> Unit,
    private val onError: (String) -> Unit
) {
    private val queue = Channel<ByteArray>(capacity = 40)
    private val running = AtomicBoolean(true)
    @Volatile private var track: AudioTrack? = null

    private val worker: Job = scope.launch(Dispatchers.IO) {
        try {
            for (pcm in queue) {
                if (!running.get()) break
                if (track == null) track = createTrack()
                val out = track ?: break
                onLevel(PatchbayProtocol.amplitude(pcm))
                var offset = 0
                while (offset < pcm.size && running.get()) {
                    val written = out.write(
                        pcm, offset, pcm.size - offset, AudioTrack.WRITE_BLOCKING
                    )
                    if (written <= 0) throw IllegalStateException("Speaker write error: $written")
                    offset += written
                }
            }
        } catch (t: Exception) {
            if (running.get()) onError("Speaker error: ${t.message}")
        } finally {
            releaseTrack()
        }
    }

    fun offer(pcm: ByteArray): Boolean =
        running.get() && queue.trySend(pcm).isSuccess

    fun stop() {
        if (!running.getAndSet(false)) return
        queue.close()
        try { track?.pause() } catch (_: Exception) {}
        try { track?.flush() } catch (_: Exception) {}
        worker.cancel()
    }

    private fun createTrack(): AudioTrack {
        val min = AudioTrack.getMinBufferSize(
            PatchbayProtocol.SAMPLE_RATE,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (min <= 0) throw IllegalStateException("Watch speaker does not support 16kHz PCM")
        val created = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(PatchbayProtocol.SAMPLE_RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setBufferSizeInBytes(max(min, 6400))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        if (created.state != AudioTrack.STATE_INITIALIZED) {
            created.release()
            throw IllegalStateException("Watch speaker could not initialize")
        }
        created.play()
        return created
    }

    private fun releaseTrack() {
        val old = track
        track = null
        try { old?.stop() } catch (_: Exception) {}
        try { old?.release() } catch (_: Exception) {}
        onLevel(0f)
    }
}
