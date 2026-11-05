# PatchBay Wear: Watch6 Classic expression

**Status:** draft branch; built in CI and verified on a physical Watch6 Classic (47 mm) for gates A0-A6 and most of A7 (see section 8, updated 2026-11-01)
**Target:** Samsung Galaxy Watch6 Classic, 43 mm or 47 mm, Wear OS
**Date:** 2026-10-31

## 1. Product promise

PatchBay exists already. The watch is a **physical expression of the same PatchBay agent**, not an agent of its own. No distinct model, persona, memory, tools, speech recognition, or speech synthesizer may be introduced to the watch. The voice on the watch is the voice configured in the existing PatchBay.

> Double-press the watch Home key (after the user configures the shortcut), tap the character, speak a question, tap to finish, and hear PatchBay's answer from the watch speaker. The watch displays a reactive 2D character.

No new hardware purchase is needed for the first acceptance program.

## 2. Design and behavior

- Keep the normal Samsung watch face. This is a foreground Wear OS application, not a replacement watch face.
- Original simple 2D avatar occupies the center of a circular OLED-first design; oversized hit area serves as the only main command.
- Ready: blue ring, relaxed face, "Tap to talk".
- Listening: teal ring, mouth/reactive level, "Tap again when finished".
- Waiting: violet ring, three dots; describes server activity without pretending to visualize internal reasoning.
- Speaking: blue ring, mouth openness from decoded PCM amplitude. No claim of viseme accuracy.
- Offline/error: warm red status and explicit retry.
- Latest answer: separate readable, bezel-scrollable view. No full conversation browser.
- Settings: one server address, typed on an in-app 0-9 / . / delete keypad as a bare private IPv4 (optionally `:port`; saved as `ws://<ip>:8765`). "Full URL" switches to the system keyboard for `wss://`. No brain chooser, no model tuning, no local agent configuration. (The system keyboards could not enter an IP on the Watch6 Classic: QWERTY needs page switches for every symbol, the decimal pad allows one dot, the phone pad has no dot.)
- Round screen: Home, Settings and Last Reply scroll with touch and the rotating bezel, with padding scaled to the display, so no control is left at the narrow bottom of the circle.
- Accessibility: descriptive click labels, readable text, large touch target; animation respects system-wide animator disable where practical.
- Layout must be evaluated on both watch sizes (432x432 and 480x480 physical pixel variants), font scaling, and TalkBack.

Palette: near-black #05070D; navy #13243B; text #E8F7FF; ready #77CFFF; listening #50E5C2; waiting #C29BFF; speaking #6DB7FF; error #FF9D94.

## 3. Reuse boundary

**Existing server:** `patches/websocket_streamer.py` and the published `docs/contracts/ws-client-protocol.md` are the protocol source of truth.

**Existing phone research:** `Platano78/phonelinux` contains `LifelineSocket.kt` and `LifelineSession.kt`. Their websocket, turn-end silence, and audio playback ideas inform this implementation. The full Embodiment APK, local models, native libraries, and its local decision making are **not** embedded in Wear.

**New client:** `clients/wearos/` is the entire build boundary. No modifications to PatchBay server semantics or production configuration are required for this first experiment.

## 4. Protocol and voice turn

One WebSocket connection to the existing server on port 8765 (unless a private gateway gives it another URL).

- Binary frames carry signed little-endian 16-bit mono PCM at exactly 16000 Hz, in both directions.
- Text frames contain JSON with `type` discriminator, not `event_type`.
- On open, send `{"type":"config_get"}`.
- Observe `transcription_completed`, `partial_transcription`, and non-empty `assistant_text`. Ignore unknown server events.
- On PTT release, stop/drain the microphone and send 2 seconds of PCM zeroes to close the server VAD turn; this mirrors Lifeline.
- Do not capture through spoken playback; no continuous wake word in v0.1.
- Handle arbitrary TTS frame boundaries in a continuous AudioTrack stream.
- There is no server-emitted explicit completion-of-TTS event for this audio path. A reply ends only when (a) no audio arrived for 1.6 s, (b) the AudioTrack has played out every frame written (the server streams ~2x faster than real time, so arrival-quiet alone cut replies short), and (c) no tool call is pending (empty-text `assistant_text` carrying `tools`, until the answer frame). The playback queue is bounded at 60 s of audio, not by chunk count. An explicit server event would still be cleaner, in a separate protocol PR.
- UI only plays audio after the wearer initiated a PTT turn, while the app is foreground. This is a mitigation, **not** true per-client routing; simultaneous clients remain a known limitation.

## 5. Network and confidentiality

- The current PatchBay raw WebSocket has **no application-level login**. It also shares the same conversation and broadcasts TTS to all connected clients.
- **Do not** expose port 8765 to the public internet.
- Development uses `ws://` only with a manually specified RFC1918 IPv4 address on a trusted home Wi-Fi network.
- `wss://` endpoints must validate their TLS certificates normally. Encryption is **not authentication**; remote access still requires a real authenticated private path.
- The Android watch's network connection via a paired phone does not imply access to the phone's private VPN. That requires its own on-device test.
- Store only the user-specified URL in app preferences. No GPS, voice recordings, tokens or transcript history are persisted. The most recent speech text remains in RAM only.
- On background, the socket, playback, and microphone are torn down. Stale uplink audio may not be replayed on reconnect.

## 6. Architecture

```
Watch6 Classic (Wear OS, foreground)
  MainActivity
    WatchScreen (Compose + Canvas original avatar)
    WatchController (phase + state)
      WatchMicrophone (AudioRecord PCM16/16k)
      WatchSpeaker (AudioTrack PCM16/16k)
      PatchbaySocket (OkHttp WebSocket)
          | binary audio, text control/events |
          v
    Existing PatchBay speech-to-speech pipeline :8765
      ASR -> currently selected brain/persona/tools -> existing TTS
```

## 7. Explicitly deferred

- A phone companion: only add Android Wear Data Layer ChannelClient or another phone relay **after** local Wi-Fi proves the interaction is worthwhile.
- True per-client session audio isolation or request/turn identifiers (server protocol change).
- Background/always listening, AI on the watch, watch-face replacements, voice cloning, local notifications, or floating agent surfaces.
- New cloud services and new hardware.

## 8. Test and acceptance program

| Gate | Evidence required |
|---|---|
| A0 | Gradle resolves dependencies; `testDebugUnitTest` and `assembleDebug` pass in CI |
| A1 | Debug APK installs and launches on actual Galaxy Watch6 Classic |
| A2 | A real home Wi-Fi socket connects; configuration event arrives |
| A3 | Ten PTT turns: correct start, final-silence VAD completion, correct transcript |
| A4 | Ten returned TTS answers: intelligible, uninterrupted, no self-feedback |
| A5 | Watch screen off/background: mic, socket, speaker stop; nothing spoken unsolicited |
| A6 | Connection drop mid-turn: no buffered audio is replayed after retry |
| A7 | Circular layout and TalkBack verified on actual watch; rotary reply scrolling works |
| A8 | Interleaved desktop use explored; warn of shared-audio limitations rather than fake session privacy |
| A9 | Battery and latency measured over 20 interactions plus idle period; thresholds set from actual baseline |

**Gate status (2026-11-01, one 47 mm SM-R960, Android 16; small samples, preliminary):**

| Gate | Result | Evidence |
|---|---|---|
| A0 | Pass | CI green on every pushed head; 10 unit tests (protocol, endpoint entry, gain, capture stats). |
| A1 | Pass | Debug APK installed and launched; no crash seen. |
| A2 | Pass | Socket connects on home Wi-Fi; `config_state` arrives. |
| A3 | Pass, with a known edge | 30+ turns over the session; 6/6 short, quiet phrases on the final build. 4 turns were lost to the server VAD's 384 ms active-speech floor, all within one 8-minute stretch that ended after the owner checked the watch's mic again; none in the 20+ turns after. |
| A4 | Pass, with a server-side limit | Replies of 17-20 s played to the last frame; web-search answers survive 3-6 s tool gaps; no self-feedback. One reply was cut at 15 s by PatchBay's own remote-TTS synthesis cap while the TTS host was busy; that affects every client. |
| A5 | Pass | Mic and speaker released ~1 s after leaving the app (teardown in `onStop`). |
| A6 | Pass | Wi-Fi off mid-listen released the mic at once; nothing replayed after reconnect. |
| A7 | Partly | Circular layout: every control reachable inside the circle (device screenshots, taps). Address entry works through the in-app keypad (adb-driven taps; owner hand test pending). Last Reply shows the latest answer. Not done: TalkBack, font scaling, 43 mm model, owner confirmation of bezel scrolling. |
| A8 | Not done | Expected from the code: an idle watch stays silent during desktop turns, but the desktop cockpit speaks the answers to watch turns (server broadcast). Needs a hands-on check. |
| A9 | Partly | Latency, second tap to first reply audio: median 1.31 s (0.87-1.62 s, n=6, no tools). Memory at idle: 86 MB PSS. Battery over 20 turns + idle: not measured yet. Playback-queue peak memory: bounded by design at 60 s of PCM (1.92 MB), not measured on device. |

## 9. Backlog only after acceptance

1. Explicit audio-done and turn-origin metadata if multi-client use makes the current heuristic unsafe.
2. Outside-home use through an authenticated private path. Findings so far: the watch's Bluetooth proxy through the phone reportedly does not carry the phone's VPN (other users' reports, not tested here); Tailscale has no official Wear OS app; a `wss://` URL with a valid certificate already passes the client's validation, so the work is mostly server-side plus a vetted way for the watch to join the network.
3. Better 2D animation assets, more expressive mouth shapes, avatar/persona identity from existing PatchBay.
4. Optional watch complication to launch the app while retaining Samsung's watch face.

## 10. Product success test

The best metric isn't feature count. It is whether the owner naturally uses the watch for short conversations, without taking out the phone or touching the computer, and without disrupting current watch functionality.
