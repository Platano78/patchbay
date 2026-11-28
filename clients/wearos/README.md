# PatchBay Wear — native Samsung Galaxy Watch6 Classic client

A small Wear OS application that turns the watch into **another physical expression of your existing PatchBay**, not a new assistant. It captures 16kHz mono PCM, streams it to PatchBay, plays the existing TTS, and animates an original 2D face.

**Status:** prototype, usable for everyday experimentation on a home network. Verified on a physical Galaxy Watch6 Classic (47 mm, SM-R960, Android 16 / SDK 36) on 2026-11-01; see [Verified on a Watch6 Classic](#verified-on-a-watch6-classic). Gates still open are in SPEC.md section 8.

## Prerequisites

- Samsung Galaxy Watch6 Classic with Wear OS and wireless debugging enabled
- Android Studio with SDK Platform 35, Android build-tools and JDK 17
- Gradle 8.13 (the independent prototype deliberately does not contain a binary wrapper)
- Existing PatchBay pipeline running on a trusted home network
- The LAN IPv4 address of the PatchBay server and port (default 8765)

The implementation lives entirely under `clients/wearos/`; no other PatchBay code is modified. Open **clients/wearos** as its own Android Studio project.

## Build

```bash
gradle -p clients/wearos :app:testDebugUnitTest :app:assembleDebug
```

The unsigned debug APK will be at `clients/wearos/app/build/outputs/apk/debug/app-debug.apk` **only after a successful build**.

In Galaxy Watch settings, enable Developer options and Wireless debugging, then connect with ADB (including its current IP:port and any pairing step required by the watch):

```bash
adb pair WATCH_IP:PAIRING_PORT
adb connect WATCH_IP:DEBUG_PORT
adb install -r clients/wearos/app/build/outputs/apk/debug/app-debug.apk
```

On the watch, launch PatchBay from Apps. You may optionally assign the watch's Home-key double-press shortcut to this app in Samsung's settings. No root, firmware replacement or new watch face required.

## Configure and use

1. In the watch app, turn the bezel (or swipe up) to reach **Settings**. Type your PatchBay server's LAN IP address on the on-screen keypad, for example `192.168.x.x`; port 8765 is added for you (type `ip:port` for another port). For a `wss://` address, tap **Full URL** to use the system keyboard instead.
2. Save; the app connects and requests `config_get`.
3. Tap the central character to start talking. Grant microphone permission if prompted.
4. Tap again to finish. The app drains the capture and sends VAD silence.
5. Hear the existing PatchBay answer from the watch speaker; view the latest response via **Reply**.
6. If offline, tap the face to retry. Leaving the app disconnects and releases the mic/speaker.

## Networking / security

PatchBay's current WebSocket is unauthenticated and shared by all clients. **Never port-forward it**. For this proof-of-use, `ws://` is limited by the client to RFC1918 IPv4 destinations on trusted home Wi-Fi. `wss://` is also supported without certificate bypass, but the network perimeter must still be authenticated. Phone-based or VPN remote connectivity has **not been implemented**; a paired phone does not magically forward its VPN.

No local AI model, agent, speech model, or independent memory is shipped in this APK. No conversation data is saved to storage. The watch only plays audio when its own PTT initiated a turn. Because the server broadcasts all speech, simultaneous multi-client use needs a later protocol improvement for guaranteed isolation.

## Repo map

- `SPEC.md` — product contract, limitations and acceptance gates
- `app/src/main/java/com/patchbay/wear/WatchScreen.kt` — circular Compose UI + vector character
- `WatchController.kt` — foreground session, PTT, UI phases
- `WatchAudio.kt` — microphone and speaker streams
- `PatchbaySocket.kt` — WebSocket to existing server
- `PatchbayProtocol.kt` — URL restriction, event parser, PCM helpers
- `app/src/test/` — focused unit tests: protocol, endpoint entry, uplink gain, capture statistics
- `.github/workflows/wearos.yml` in repository root — Android CI build

## Verified on a Watch6 Classic

Observed on one 47 mm SM-R960 (Android 16) against a live PatchBay on 2026-11-01. Small samples: read the numbers as preliminary, not benchmarks.

| Area | Result |
|---|---|
| Install, launch, connect | Pass. Debug APK installs and launches; the socket connects and `config_state` arrives. |
| Voice turns | 30+ turns over the evening; 6/6 short, quiet phrases ("Thanks.", "Stop.") on the final build. |
| Long replies | Replies of 17-20 s played to the last frame. |
| Web-search turns | The answer survives the 3-6 s silent gap while the tool runs. |
| Round-screen layout | Every control on Home, Settings and Last Reply scrolls fully inside the circle (screenshots and taps on the device). |
| Server address entry | The in-app keypad cleared and retyped the IP, saved it as `ws://<ip>:8765` and reconnected, driven by taps over adb. Owner hand test still pending; the system keyboards it replaces failed by hand. |
| Last Reply | Shows the latest answer (owner check). |
| Leaving the app | Mic and speaker released ~1 s after exit. |
| Wi-Fi off and on | Mic released at once; no stale audio replayed after reconnecting. |
| Latency | Second tap to first reply audio: median 1.31 s (0.87-1.62 s, 6 short turns, no tools). |
| Memory | 86 MB PSS, 7.8 MB Java heap at idle. |
| Mic levels | Final build, 7 turns: speech peaks of 25-101 RMS (int16) per 200 ms before gain; 0 clipped samples, 0% post-gain saturation. |

Every turn writes one `PatchBayWear` logcat line with capture levels (numbers, never audio), so a "no answer" report can be split into "the mic heard nothing" and "the server ignored it".

## Known limitations

- **Shared session.** PatchBay broadcasts TTS to every connected client. The watch only plays audio for a turn it started, but a desktop cockpit that is open at the same time will also speak the answers to watch questions. Interleaved desktop + watch use is untested.
- **No TTS-done event.** A reply ends when no audio has arrived for 1.6 s, the speaker has played everything it was given, and no tool call is pending (the server's empty-text `assistant_text` with `tools`).
- **Server TTS cap.** PatchBay's remote TTS stops a reply that takes longer than 15 s to synthesize (`remote_speech_max_duration_s`). When the TTS host is busy and synthesizes at about real time, long answers are cut near 15 s on every client, not only the watch.
- **Very short, quiet phrases** can fall under the server VAD's 384 ms active-speech floor. During testing, 4 turns were lost this way, all within one 8-minute stretch that ended after the owner checked the watch's mic again; none were lost in the 20+ turns after it. Capture uses `VOICE_RECOGNITION` plus a fixed +15.6 dB soft-clipped gain; `VOICE_COMMUNICATION` delivered near-silence on the SM-R960.
- **Check the mic hardware first** if no turn is ever transcribed: record in Samsung Voice Recorder and play it back. A partly blocked mic port caused exactly this during testing (blow it out gently or use a soft brush; no pins).
- **Leaving the app** releases mic and speaker about 1 s later: teardown runs in `onStop`, after the exit animation. Moving it to `onPause` would also tear down the session whenever the microphone permission dialog appears.
- **Home network only.** Away from home the watch reaches the internet through the phone's Bluetooth proxy, which reportedly does not carry the phone's VPN (not tested here). Tailscale has no official Wear OS app, and plain `ws://` is restricted to RFC1918 addresses by design.
- **Not measured yet:** battery over a 20-turn session, TalkBack, font scaling, the 43 mm model, and explicit owner confirmation of bezel scrolling on Last Reply.
- No Gradle wrapper binary bundled; use Gradle 8.13 via Android Studio or CLI.
- Signing: a locally built debug APK and the CI artifact use different debug keys. Switching between them needs an uninstall, which clears the saved server address.

## Future enhancements (not started)

- An explicit end-of-speech event from the server, replacing the quiet-interval rule.
- Per-client audio routing or turn identifiers, so a watch turn is not spoken by other open clients.
- Remote use: an authenticated private path (for example a `wss://` endpoint on a tailnet) plus a vetted way for the watch itself to join it. Needs its own experiment and a server-side change.
- A watch complication or tile to launch PatchBay from the watch face.
