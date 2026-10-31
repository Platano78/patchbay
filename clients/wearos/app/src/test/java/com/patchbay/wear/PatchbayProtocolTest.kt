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
        assertEquals(PatchbayProtocol.Event.Unknown,
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
}
