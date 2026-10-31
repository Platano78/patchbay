package com.patchbay.wear

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * One view into one existing PatchBay conversation. No separate assistant state.
 *
 * Lifecycle rule: only while the watch activity is visibly foregrounded may we hold
 * the socket, microphone or speaker. On background, tear everything down.
 */
class WatchController(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(
        "patchbay_wear_config", Context.MODE_PRIVATE
    )
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val _ui = MutableStateFlow(
        WatchUiState(endpoint = preferences.getString("endpoint", "") ?: "")
    )
    val ui: StateFlow<WatchUiState> = _ui.asStateFlow()

    private var visible = false
    private var expectingReply = false
    private var heardSpeech = false
    private var socket: PatchbaySocket? = null
    private var microphone: WatchMicrophone? = null
    private var speaker: WatchSpeaker? = null
    private var idleJob: Job? = null
    private var waitingJob: Job? = null
    private var stoppingCapture = false

    fun onForeground() {
        if (visible) return
        visible = true
        connect()
    }

    fun onBackground() {
        if (!visible) return
        visible = false
        tearDown()
        _ui.update {
            it.copy(
                connected = false, audioLevel = 0f,
                phase = if (it.endpoint.isBlank()) WatchPhase.UNCONFIGURED else WatchPhase.OFFLINE,
                detail = "Paused while watch app is closed"
            )
        }
    }

    fun dispose() {
        onBackground()
        scope.cancel()
    }

    fun configure(raw: String) {
        val endpoint = PatchbayProtocol.validateEndpoint(raw)
        if (endpoint == null) {
            _ui.update {
                it.copy(
                    phase = WatchPhase.ERROR,
                    detail = "Enter a wss:// URL or ws:// private LAN IPv4 address"
                )
            }
            return
        }
        preferences.edit().putString("endpoint", endpoint).apply()
        _ui.update { it.copy(endpoint = endpoint) }
        if (visible) connect()
    }

    fun permissionDenied() {
        if (!visible) return
        _ui.update {
            it.copy(detail = "Microphone permission is needed for voice input")
        }
    }

    fun retry() {
        if (visible) connect()
    }

    private fun connect() {
        tearDown()
        val endpoint = PatchbayProtocol.validateEndpoint(_ui.value.endpoint)
        if (endpoint == null) {
            _ui.update { it.copy(phase = WatchPhase.UNCONFIGURED, connected = false) }
            return
        }
        _ui.update {
            it.copy(phase = WatchPhase.CONNECTING, connected = false,
                detail = "Connecting to PatchBay…", audioLevel = 0f)
        }
        lateinit var fresh: PatchbaySocket
        fresh = PatchbaySocket(object : PatchbaySocket.Callbacks {
            override fun connected() {
                scope.launch {
                    if (!visible || socket !== fresh) return@launch
                    _ui.update {
                        it.copy(phase = WatchPhase.READY, connected = true,
                            detail = "Tap to talk")
                    }
                }
            }

            override fun receivedAudio(pcm: ByteArray) {
                scope.launch {
                    if (!visible || socket !== fresh || !expectingReply) return@launch
                    if (speaker == null) {
                        speaker = WatchSpeaker(scope,
                            onLevel = { level ->
                                scope.launch {
                                    if (visible) _ui.update { it.copy(audioLevel = level) }
                                }
                            },
                            onError = { problem -> scope.launch { fail(problem) } }
                        )
                    }
                    if (speaker?.offer(pcm) != true) {
                        fail("Audio playback queue is full")
                        return@launch
                    }
                    heardSpeech = true
                    _ui.update {
                        it.copy(phase = WatchPhase.SPEAKING, detail = "PatchBay is speaking")
                    }
                    idleJob?.cancel()
                    idleJob = scope.launch {
                        // The server does not broadcast an end-of-TTS event. Wait for
                        // a quiet interval before accepting a new turn. Verify on hardware.
                        delay(1_600L)
                        if (visible && socket === fresh && expectingReply && heardSpeech) {
                            expectingReply = false
                            speaker?.stop()
                            speaker = null
                            _ui.update {
                                it.copy(phase = WatchPhase.READY, audioLevel = 0f,
                                    detail = "Tap to talk")
                            }
                        }
                    }
                }
            }

            override fun receivedEvent(event: PatchbayProtocol.Event) {
                scope.launch {
                    if (!visible || socket !== fresh) return@launch
                    when (event) {
                        is PatchbayProtocol.Event.Transcript ->
                            if (expectingReply || _ui.value.phase == WatchPhase.LISTENING) {
                                _ui.update {
                                    it.copy(transcript = event.text, partialTranscript = "")
                                }
                            }
                        is PatchbayProtocol.Event.Partial ->
                            if (expectingReply || _ui.value.phase == WatchPhase.LISTENING) {
                                _ui.update {
                                    it.copy(partialTranscript =
                                        (it.partialTranscript + event.delta).takeLast(240))
                                }
                            }
                        is PatchbayProtocol.Event.Reply -> if (expectingReply) {
                            _ui.update { it.copy(lastReply = event.text) }
                        }
                        else -> Unit
                    }
                }
            }

            override fun disconnected(reason: String) {
                scope.launch {
                    if (visible && socket === fresh) fail(reason)
                }
            }
        })
        socket = fresh
        try { fresh.connect(endpoint) } catch (t: Exception) {
            fail(t.message ?: "Could not open socket")
        }
    }

    /** The character is a single large button; no background wakeword mode. */
    fun toggleTalk() {
        when (_ui.value.phase) {
            WatchPhase.READY -> beginTalk()
            WatchPhase.LISTENING -> endTalk()
            else -> Unit
        }
    }

    private fun beginTalk() {
        if (!visible || _ui.value.phase != WatchPhase.READY || socket == null) return
        idleJob?.cancel()
        waitingJob?.cancel()
        expectingReply = false
        heardSpeech = false
        speaker?.stop()
        speaker = null
        stoppingCapture = false

        val mic = WatchMicrophone(scope)
        microphone = mic
        val started = mic.start(
            onPcm = { pcm ->
                if (socket?.sendPcm(pcm) != true) {
                    scope.launch { fail("Microphone uplink interrupted") }
                }
            },
            onLevel = { level ->
                scope.launch {
                    if (visible && _ui.value.phase == WatchPhase.LISTENING) {
                        _ui.update { it.copy(audioLevel = level) }
                    }
                }
            },
            onError = { problem -> scope.launch { fail(problem) } }
        )
        if (started) {
            _ui.update {
                it.copy(phase = WatchPhase.LISTENING,
                    transcript = "", partialTranscript = "",
                    audioLevel = 0f, detail = "Tap again when finished")
            }
        } else {
            fail("Microphone could not start")
        }
    }

    private fun endTalk() {
        if (!visible || _ui.value.phase != WatchPhase.LISTENING || stoppingCapture) return
        stoppingCapture = true
        _ui.update {
            it.copy(phase = WatchPhase.WAITING, audioLevel = 0f,
                detail = "Sending your question…")
        }
        scope.launch {
            microphone?.stopAndDrain()
            microphone = null
            stoppingCapture = false
            if (!visible || !_ui.value.connected || _ui.value.phase != WatchPhase.WAITING) return@launch
            // Server-side VAD needs actual zero samples to close the user turn.
            // See the same technique in Embodiment's LifelineSession.
            val sent = socket?.sendPcm(PatchbayProtocol.finishTurnSilence()) == true
            if (!sent) {
                fail("Could not finish the voice turn")
                return@launch
            }
            expectingReply = true
            _ui.update { it.copy(detail = "PatchBay is working…") }
            waitingJob?.cancel()
            waitingJob = launch {
                delay(40_000L)
                if (expectingReply && !heardSpeech && visible) {
                    _ui.update { it.copy(detail = "Still waiting for speech from PatchBay…") }
                }
            }
        }
    }

    private fun fail(reason: String) {
        if (!visible) return
        tearDown()
        _ui.update {
            it.copy(
                phase = WatchPhase.ERROR, connected = false,
                audioLevel = 0f, detail = reason.take(120)
            )
        }
    }

    private fun tearDown() {
        expectingReply = false
        heardSpeech = false
        idleJob?.cancel()
        waitingJob?.cancel()
        idleJob = null
        waitingJob = null
        microphone?.stopImmediately()
        microphone = null
        speaker?.stop()
        speaker = null
        socket?.close()
        socket = null
        stoppingCapture = false
    }
}
