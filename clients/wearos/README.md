# PatchBay Wear — native Samsung Galaxy Watch6 Classic client

A small Wear OS application that turns the watch into **another physical expression of your existing PatchBay**, not a new assistant. It captures 16kHz mono PCM, streams it to PatchBay, plays the existing TTS, and animates an original 2D face.

**Status:** source prototype; not yet verified on an actual Watch6 Classic. No APK is claimed to exist until a successful Android build.

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

1. In the watch app, open **Settings** and enter `ws://192.168.x.x:8765` with the **real** trusted LAN address of your existing PatchBay instance.
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
- `app/src/test/` — focused protocol unit tests
- `.github/workflows/wearos.yml` in repository root — Android CI build

## Known limitations / next tests

- No true server-side turn ownership or per-client playback routing.
- The server doesn't emit an explicit TTS-done event on this path. 1.6s stream inactivity is a **heuristic**; verify there is no speech cutoff on device.
- 16kHz AudioRecord/AudioTrack availability, mic permission, gain, latency and battery must be tested on a real watch.
- No Gradle wrapper binary bundled; use Gradle 8.13 via Android Studio or CLI.
- No Android companion app or Wear Data Layer relay in v0.1. Only direct watch-to-server networking.
