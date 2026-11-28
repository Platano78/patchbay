package com.patchbay.wear

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit

/**
 * Wire-only transport. No LLM, persona state, speech synthesis, or local memory.
 * Connections are explicitly scoped to the visible watch activity.
 */
class PatchbaySocket(private val callbacks: Callbacks) {
    interface Callbacks {
        fun connected()
        fun receivedAudio(pcm: ByteArray)
        fun receivedEvent(event: PatchbayProtocol.Event)
        fun disconnected(reason: String)
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(25, TimeUnit.SECONDS)
        .build()

    private var generation = 0L
    private var socket: WebSocket? = null
    private var open = false

    @Synchronized
    fun connect(url: String) {
        close()
        val session = generation
        val request = Request.Builder().url(url).build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                synchronized(this@PatchbaySocket) {
                    if (session != generation) { webSocket.cancel(); return }
                    open = true
                    webSocket.send("""{"type":"config_get"}""")
                }
                callbacks.connected()
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (isLive(session)) callbacks.receivedAudio(bytes.toByteArray())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (isLive(session)) callbacks.receivedEvent(PatchbayProtocol.parseEvent(text))
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (markClosed(session)) callbacks.disconnected(t.message ?: "Connection failed")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (markClosed(session)) callbacks.disconnected("Socket closed ($code)")
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        })
    }

    @Synchronized
    private fun isLive(session: Long): Boolean = session == generation && open

    @Synchronized
    private fun markClosed(session: Long): Boolean {
        if (session != generation) return false
        open = false
        socket = null
        return true
    }

    /**
     * Drop instead of queueing unbounded microphone data across an outage.
     * OkHttp queueSize includes pending outgoing traffic.
     */
    @Synchronized
    fun sendPcm(pcm: ByteArray): Boolean {
        val ws = socket ?: return false
        if (!open || ws.queueSize() > 1024 * 1024L) return false
        return ws.send(pcm.toByteString())
    }

    @Synchronized
    fun close() {
        generation++
        open = false
        socket?.cancel()
        socket = null
    }
}
