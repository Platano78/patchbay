package com.patchbay.wear

import org.junit.Assert.*
import org.junit.Test

class PatchbayProtocolTest {
    @Test fun matchesServerPcmFormat() {
        assertEquals(16_000, PatchbayProtocol.SAMPLE_RATE)
        assertArrayEquals(
            byteArrayOf(0x00, 0x80.toByte(), 0xff.toByte(), 0x7f),
            PatchbayProtocol.packPcm(shortArrayOf(Short.MIN_VALUE, Short.MAX_VALUE), 2)
        )
        assertEquals(64_000, PatchbayProtocol.finishTurnSilence().size)
        assertTrue(PatchbayProtocol.finishTurnSilence().all { it == 0.toByte() })
    }

    @Test fun acceptsOnlyPrivatePlaintextEndpoints() {
        assertNotNull(PatchbayProtocol.validateEndpoint("ws://192.168.77.79:8765"))
        assertNotNull(PatchbayProtocol.validateEndpoint("ws://10.0.0.1:8765"))
        assertNotNull(PatchbayProtocol.validateEndpoint("ws://172.31.1.4:8765"))
        assertNotNull(PatchbayProtocol.validateEndpoint("wss://patchbay.example.test:8443/voice"))
        for (bad in listOf(
            "http://192.168.77.79", "ws://8.8.8.8:8765", "ws://172.32.0.1",
            "ws://192.168.77.999", "ws://localhost:8765", "ws://example.org:8765",
            "wss://user:password@x.example", "wss://x.example/path?token=secret",
            "ws://192.168.77.79:8765/#fragment", ""
        )) assertNull(bad, PatchbayProtocol.validateEndpoint(bad))
    }

    @Test fun handlesKnownEventsAndIgnoresEmptyAssistantFrame() {
        val user = PatchbayProtocol.parseEvent(
            """{"type":"transcription_completed","transcript":"Where is my phone?"}"""
        )
        assertEquals(PatchbayProtocol.Event.Transcript("Where is my phone?"), user)
        // The empty tool frame is never a Reply (nothing to render); it signals a pending tool.
        assertEquals(PatchbayProtocol.Event.ToolCall,
            PatchbayProtocol.parseEvent("""{"type":"assistant_text","text":"","tools":["tool"]}"""))
        assertEquals(PatchbayProtocol.Event.Reply("Found it."),
            PatchbayProtocol.parseEvent("""{"type":"assistant_text","text":"Found it."}"""))
        assertEquals(PatchbayProtocol.Event.Unknown, PatchbayProtocol.parseEvent("{invalid"))
        assertEquals(PatchbayProtocol.Event.Unknown, PatchbayProtocol.parseEvent("""{"type":"future_event"}"""))
    }

    @Test fun amplitudeIsBounded() {
        assertEquals(0f, PatchbayProtocol.amplitude(byteArrayOf()), 0.001f)
        assertEquals(0f, PatchbayProtocol.amplitude(byteArrayOf(0, 0)), 0.001f)
        assertEquals(1f, PatchbayProtocol.amplitude(
            PatchbayProtocol.packPcm(shortArrayOf(Short.MIN_VALUE), 1)
        ), 0.001f)
    }

    @Test fun uplinkGainLiftsQuietSpeechAndSoftClipsLoud() {
        fun g(v: Int) = PatchbayProtocol.uplinkGain(v.toShort()).toInt()
        assertEquals(0, g(0))
        // Quiet wrist-distance speech (RMS ~30-100 measured) gets ~6x.
        assertEquals(600.0, g(100).toDouble(), 6.0)
        // Loud peaks are rounded into range, never wrapped.
        assertTrue(g(8_500) in 28_000..32_767) // tanh: 29_976
        assertTrue(g(Short.MAX_VALUE.toInt()) <= 32_767)
        assertTrue(g(Short.MIN_VALUE.toInt()) >= -32_768)
        assertEquals(-g(4_000), g(-4_000))
        // Monotonic: louder in never gives quieter out.
        var last = Int.MIN_VALUE
        for (v in -32_768..32_767 step 97) { val o = g(v); assertTrue(o >= last); last = o }
    }

    @Test fun toolCallFrameIsNotAReplyButIsRecognised() {
        // Shape observed live: empty text + tools, then the real answer in a second frame.
        assertEquals(PatchbayProtocol.Event.ToolCall, PatchbayProtocol.parseEvent(
            """{"type":"assistant_text","text":"","tools":[{"name":"web_search","arguments":"{}"}]}"""))
        assertEquals(PatchbayProtocol.Event.Reply("No games today."), PatchbayProtocol.parseEvent(
            """{"type":"assistant_text","text":"No games today.","tools":[]}"""))
        assertEquals(PatchbayProtocol.Event.Unknown, PatchbayProtocol.parseEvent(
            """{"type":"assistant_text","text":""}"""))
    }

    @Test fun bareIpEntryBecomesALanWebSocketUrl() {
        assertEquals("ws://10.0.0.5:8765", PatchbayProtocol.validateEndpoint("10.0.0.5"))
        assertEquals("ws://10.0.0.5:9000", PatchbayProtocol.validateEndpoint(" 10.0.0.5:9000 "))
        // The private-LAN rule still applies to the shorthand.
        assertNull(PatchbayProtocol.validateEndpoint("8.8.8.8"))
        assertNull(PatchbayProtocol.validateEndpoint("10.0.0"))
        assertNull(PatchbayProtocol.validateEndpoint("10.0.0.5:99999"))
        assertNull(PatchbayProtocol.validateEndpoint("10.0.0.256"))
        // Full URLs keep working unchanged.
        assertEquals("ws://10.0.0.5:8765", PatchbayProtocol.validateEndpoint("ws://10.0.0.5:8765"))
    }

    @Test fun editableAddressShowsOnlyTheIpForNumberPadEditing() {
        assertEquals("10.0.0.5", PatchbayProtocol.editableAddress("ws://10.0.0.5:8765"))
        assertEquals("10.0.0.5:9000", PatchbayProtocol.editableAddress("ws://10.0.0.5:9000"))
        // Anything a bare IP cannot express stays a full URL.
        assertEquals("wss://voice.example:443", PatchbayProtocol.editableAddress("wss://voice.example:443"))
        assertEquals("ws://10.0.0.5:8765/x", PatchbayProtocol.editableAddress("ws://10.0.0.5:8765/x"))
        assertEquals("", PatchbayProtocol.editableAddress(""))
    }

    @Test fun entryPageOpensInIpModeUnlessTheUrlNeedsMore() {
        // Regression: the page compared the other way round and always opened in full-URL mode.
        assertFalse(PatchbayProtocol.needsFullUrl("ws://10.0.0.5:8765"))
        assertFalse(PatchbayProtocol.needsFullUrl("ws://10.0.0.5:9000"))
        assertFalse(PatchbayProtocol.needsFullUrl(""))
        assertTrue(PatchbayProtocol.needsFullUrl("wss://voice.example:443"))
    }

    @Test fun captureStatsKeepTheStopTapOutOfSpeechLevelsAndCountClipping() {
        val st = CaptureStats()
        fun feed(level: Int, windows: Int) = repeat(windows * CaptureStats.WINDOW) { i ->
            val raw = (if (i % 2 == 0) level else -level).toShort()
            st.add(raw, PatchbayProtocol.uplinkGain(raw))
        }
        feed(100, 3)      // quiet speech, RMS 100
        feed(32_767, 2)   // stop tap at the rail, last 400 ms
        assertEquals(listOf(100, 100, 100), st.speechWindows())
        assertTrue(st.summary().contains("speechPeakWinRms=100"))
        assertTrue(st.summary().contains("rawClipped=6400"))   // the tap's 2 windows
        assertTrue(st.summary().contains("postSaturated=40.00%"))
    }
}
