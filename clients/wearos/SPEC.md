# PatchBay Wear: Watch6 Classic expression

**Status:** implementation in a draft branch; not yet built or verified on a watch
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
- Settings: one editable WebSocket endpoint; no brain chooser, no model tuning, no local agent configuration.
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
- There is no server-emitted explicit completion-of-TTS event for this audio path. The prototype uses a 1.6 second arrival-quiet heuristic. **Device testing is mandatory** to detect premature cutoff or delayed speech. Improving this may warrant an explicit server event in a separate protocol PR.
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

**Gate status today:** Source implementation written. Unit tests and physical/watch checks are **not yet verified**. A green unit test alone cannot validate audio quality or reliable turn completion.

## 9. Backlog only after acceptance

1. Explicit audio-done and turn-origin metadata if multi-client use makes the current heuristic unsafe.
2. Private watch-to-phone relay for outside-home use, with authenticated home connection.
3. Better 2D animation assets, more expressive mouth shapes, avatar/persona identity from existing PatchBay.
4. Optional watch complication to launch the app while retaining Samsung's watch face.

## 10. Product success test

The best metric isn't feature count. It is whether the owner naturally uses the watch for short conversations, without taking out the phone or touching the computer, and without disrupting current watch functionality.
