package com.patchbay.wear

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sqrt
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

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
            // VOICE_RECOGNITION, not VOICE_COMMUNICATION: on a Galaxy Watch6 Classic (SM-R960,
            // Android 16) VOICE_COMMUNICATION delivered speech at RMS ~1-30 (the server VAD never
            // fired), while VOICE_RECOGNITION -- CDD 5.4.2: no AGC, no noise suppression,
            // calibrated level -- was transcribed on most turns; its misses were quiet speech,
            // which PatchbayProtocol.uplinkGain addresses. Push-to-talk keeps the mic closed
            // during playback, so the echo canceller VOICE_COMMUNICATION would bring is not needed.
            val source = AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
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
                var samples = 0L
                var peak = 0
                var sumSquares = 0.0
                try {
                    while (isActive && running.get()) {
                        val count = source.read(buf, 0, buf.size, AudioRecord.READ_BLOCKING)
                        if (count < 0) {
                            if (running.get()) onError("Microphone read failed: $count")
                            break
                        }
                        for (i in 0 until count) {
                            val v = buf[i].toInt()
                            peak = max(peak, abs(v))
                            sumSquares += v.toDouble() * v
                        }
                        samples += max(count, 0)
                        for (i in 0 until count) buf[i] = PatchbayProtocol.uplinkGain(buf[i])
                        if (count > 0 && running.get()) {
                            val pcm = PatchbayProtocol.packPcm(buf, count)
                            onPcm(pcm)
                            onLevel(PatchbayProtocol.amplitude(pcm))
                        }
                    }
                } catch (t: Exception) {
                    if (running.get()) onError("Microphone error: ${t.message}")
                } finally {
                    // One line per turn, levels only: tells "mic heard nothing" apart from
                    // "server ignored it" without recording anything.
                    val rms = if (samples > 0) sqrt(sumSquares / samples).toInt() else 0
                    Log.i(TAG, "capture: secs=${samples / PatchbayProtocol.SAMPLE_RATE.toFloat()} " +
                        "peak=$peak rms=$rms")
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
    // Bounded by seconds of audio, not chunk count: the server streams TTS ~2x faster than
    // real time, so a 40-chunk cap (~5 s of backlog) failed mid-reply on every answer longer
    // than ~8 s and tore the session down.
    private val queue = Channel<ByteArray>(capacity = Channel.UNLIMITED)
    private val running = AtomicBoolean(true)
    @Volatile private var track: AudioTrack? = null
    /** Bytes offered but not yet handed to the AudioTrack. */
    private val pendingBytes = AtomicLong(0)
    /** Frames handed to the AudioTrack so far. */
    @Volatile private var framesWritten = 0L

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
                    framesWritten += written / PatchbayProtocol.BYTES_PER_SAMPLE
                    pendingBytes.addAndGet(-written.toLong())
                }
            }
        } catch (t: Exception) {
            if (running.get()) onError("Speaker error: ${t.message}")
        } finally {
            releaseTrack()
        }
    }

    fun offer(pcm: ByteArray): Boolean {
        if (!running.get()) return false
        if (pendingBytes.get() + pcm.size > MAX_PENDING_BYTES) return false
        pendingBytes.addAndGet(pcm.size.toLong())
        if (queue.trySend(pcm).isSuccess) return true
        pendingBytes.addAndGet(-pcm.size.toLong())
        return false
    }

    /**
     * True once every offered byte has been written AND played out by the hardware.
     * The server streams TTS faster than real time, so "no new audio arrived" is not
     * "finished speaking" -- stopping then flushes the unplayed tail of the reply.
     */
    fun isDrained(): Boolean {
        if (pendingBytes.get() > 0) return false
        val out = track ?: return true
        val played = out.playbackHeadPosition.toLong() and 0xffffffffL
        return played >= framesWritten
    }

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

private const val TAG = "PatchBayWear"

/** 60 s of 16 kHz mono int16 not yet played: far beyond any real reply, still a hard bound. */
private const val MAX_PENDING_BYTES =
    60L * PatchbayProtocol.SAMPLE_RATE * PatchbayProtocol.BYTES_PER_SAMPLE
