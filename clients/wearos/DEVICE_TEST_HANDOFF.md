# PatchBay Wear — Watch6 Classic field validation & Claude Code handoff

**Recorded:** 2026-11-01  
**Repository:** `Platano78/patchbay`  
**Active implementation branch:** `feat/wearos-charm-client` — [draft PR #3](https://github.com/Platano78/patchbay/pull/3)  
**Documentation delivery:** separate `handoff/wearos-device-validation-20261101` branch to avoid moving the watch implementation branch while device-tested fixes are still local.

> **Evidence provenance:** The observations in this document were reported by the owner from a hands-on Claude Code/ADB session on the Samsung Galaxy Watch6 Classic. The author of this handoff has **not** independently inspected the unpushed local code or physical watch. Treat the session report as a verified-by-operator baseline, and reverify critical assertions before modifying the working client.

## 0. Mission and architectural boundary

Bring the *already working* PatchBay Wear client into a safe, reviewable state, solve the remaining usability problems, and validate on the physical device. **Do not redesign PatchBay.**

The watch is a thin, native Wear OS expression of **one existing, self-hosted PatchBay**. The same core owns the model, persona, STT, tools, conversation state, and TTS. The watch owns microphone, speaker, animation and UI. EchoBay, desktop cockpit and future ESP32 endpoints are other expressions of the same core. Muse is only possible **hardware-driver reference material** for future ESP32 endpoints; neither Muse firmware nor Muse services belong in this watch task.

Do not introduce a watch AI runtime, alternate voice backend, new agent framework, second assistant session, phone bridge, or cloud dependency. Do not edit or restart the production PatchBay server unless a measured defect actually requires it and the owner approves.

## 1. Git state: do this first

**The device-tested fixes are on the owner's local checkout, not yet pushed. Preserve them.**

At the time this handoff was authored:
- The GitHub PR #3 feature head was `e98aee01f06a4b73fd7102a11d2f1d2952dee06d` (remote evidence).
- The owner reports the *local* watch fixes are committed on the same feature branch as `f2fa4a4`.
- The owner has explicitly approved **pushing the validated fixes**, but **not merging PR #3**.
- The separate handoff branch contains only this markdown file based on the older remote feature head.

From the owner's local repository:

```sh
git status --short --branch
git branch --show-current
git log --oneline -8
git fetch origin
git rev-parse origin/feat/wearos-charm-client
git show --stat --oneline f2fa4a4
git diff --stat origin/feat/wearos-charm-client...HEAD
```

**Do not run `reset --hard`, `clean -fdx`, rebase a dirty tree, or force-push.** If `f2fa4a4` is not found, the working tree isn't the expected one, or the branch has unexpected changes, stop and present the observed Git state for owner approval. Do not reproduce or overwrite the device-tested code from memory.

Bring this documentation commit into the local feature branch *without altering the remote feature branch first*. On a clean local feature branch, after checking that the handoff branch is based on the expected remote head:

```sh
git fetch origin handoff/wearos-device-validation-20261101
git log --oneline -2 origin/handoff/wearos-device-validation-20261101
git cherry-pick origin/handoff/wearos-device-validation-20261101
```

The cherry-pick should add only `clients/wearos/DEVICE_TEST_HANDOFF.md`. If it is already present or history has changed, avoid duplicating it and explain what you found.

### Pre-push safety inspection

Inspect **every changed path** relative to the remote feature branch, and confirm the five fixes and their tests are present. Review for accidentally committed secrets, private audio, `brains.json`, signing keys, local configuration, ADB serials, watch identifiers, exact home IP addresses, raw debug logs, or recordings. In particular, never publish scratch recordings or keystore material.

Then, when the branch is clean and the expected files are present, push to the **existing feature branch only**:

```sh
git push origin feat/wearos-charm-client
```

Never force-push or merge PR #3. Verify the remote HEAD matches the pushed commit and record the CI run result. If GitHub has advanced meanwhile, fetch and reconcile before pushing.

## 2. Field results (owner-reported, 2026-11-01)

| Test | Outcome and limits |
| --- | --- |
| Device / ADB | Samsung Galaxy Watch6 Classic, model SM-R960; Android 16; Linux ADB 34.0.5; wireless pairing/connection succeeded. The ADB connection port changes after Wi-Fi toggles; pairing persists. |
| Install | `com.patchbay.wear` 0.1.0 installed. The current working APK is a **locally built debug APK** using the Linux machine's signing key. The GitHub CI APK uses a different key. |
| Launch | App launches, no observed crash. |
| Server | Existing PatchBay WebSocket on trusted home LAN; no new public port or network exposure. |
| Screens | Ready, Listening, Working, Speaking observed; mouth reacts during TTS. Last Reply and bezel scrolling **not tested**. |
| Voice | 12 consecutive complete turns: watch mic → PatchBay transcription/brain → existing TTS → watch speaker → Ready. |
| Long TTS | 17–20 second answers played to final audio frame after playback-drain fix; no reported echo or dropped connections. |
| Web search | Tool-bearing replies survived 3–6 second pauses after handling search-in-progress correctly. |
| Background | Mic/speaker released about **one second after app exit**, not immediately. Needs lifecycle investigation only if reproducible/important. |
| Wi-Fi failure | Mic released immediately; no stale uplink audio replay after reconnect. |
| UX faults | Settings Back/Save buttons partly cut off by round display; entering URL with `adb input text` loses characters on Samsung keyboard; operator temporarily wrote app setting through ADB. |
| Short phrases | Very short quiet questions sometimes fail speech recognition. |
| Tests | Owner reports **6/6 unit tests passing** on local changes; a prior assertion concerning search-turn behavior was intentionally updated. Confirm in code. |

Five reported **code** fixes on `f2fa4a4`:
1. Switch mic capture source to speech recognition and apply a bounded/fixed capture boost, after separate **physical cleaning of the watch mic port**. Samsung Voice Recorder was also near-silent before cleaning. Keep physical and software causes distinct.
2. End speech only after the speaker **actually drains playback**, not merely after the last incoming network frame.
3. Increase effective playback queue budget to approximately **60 seconds** to tolerate network audio arriving faster than speaker output.
4. Respect web-search/tool pauses rather than prematurely closing the response.
5. Return from the Working state after approximately **40 seconds** without a reply.

A voice stream of 16 kHz, mono, signed 16-bit PCM consumes **32,000 bytes per second**; a 60-second queue budget is about **1.92 MB** of raw PCM. Inspect actual queue implementation, error handling and peak device memory rather than assuming this is free.

## 3. Current work order (keep scope deliberately small)

### Gate A — Preserve the known-good working build

- Verify branch and commit provenance, review the actual five fixes and the changed search test, and retain operator evidence.
- Cherry-pick this documentation only if appropriate, safety-check the diff, then push the tested local branch to PR #3.
- Verify a fresh GitHub Actions run compiles and passes `testDebugUnitTest`, and emits the expected debug APK.
- Do **not** replace the working local debug APK on the watch with GitHub's differently signed APK merely to prove the artifact exists. An uninstall would discard app data and needs owner approval.

### Gate B — Fix the genuine watch UI defects

1. **Round-screen clipping:** make Settings Back and Save reachable and fully inside circular safe bounds on the real Watch6 Classic. Use scroll, responsive spacing, and IME-aware focus. Do not simply shrink controls below accessible touch sizes. Confirm with a physical screenshot and taps.
2. **Usable endpoint entry:** validate entry and persistence from the actual watch UI without editing SharedPreferences through ADB. First distinguish a limitation of `adb input text` from Samsung's real on-watch keyboard. Do not invent a phone companion if on-watch typing works. If manual entry fails, propose and implement the smallest self-contained reliable method; keep no hard-coded address or public token in the APK.
3. **Last Reply:** display the actual last reply and prove bezel scrolling on the physical watch.
4. Retest Ready/Listening/Working/Speaking visuals after layout changes.

### Gate C — Natural short-speech reliability

- Reproduce failures with at least five short/quiet utterances and log evidence, without storing raw audio unnecessarily.
- Distinguish hardware mic-port obstruction, capture-source effects, playback echo, clipping and server VAD/STT sensitivity.
- Check actual sample levels and clipping after the fixed boost. Do not increase gain blindly; prefer bounded gain and tests.
- Keep voice capture half-duplex, foreground-only, and correct at 16 kHz mono PCM16.
- Fix only a demonstrated app-side defect; server global VAD changes need explicit scope/approval.

### Gate D — Measurements and remaining checks

- Repeat at least ten end-to-end watch turns after any code changes; include a 20-second TTS and a web-search answer.
- Capture empirical end-to-end latency and approximate watch battery impact over **20 interactions** plus an idle baseline; label small samples as preliminary, not laboratory measurements.
- Test background release latency, screen off, Wi-Fi loss/recovery and stale-audio discard.
- Test TalkBack at least once; note untested font-scale/43mm behavior if only the 47mm watch is available.
- Test simultaneous desktop cockpit and watch interaction. **Important:** current PatchBay broadcasts TTS to every client and shares a conversation; listening gating is not proven client isolation. Record this behavior; **do not** redesign server routing during this work.
- Measure playback queue memory and verify that correct network-frame arrival does not imply playback completion.

### Gate E — Cleanup and report

- The owner reported a temporary Voice Recorder entry (named `Voice 001`) and scratch test recordings on the Linux host. **Ask owner approval before deleting recordings** and confirm exact paths/filenames before any removal. Never commit them.
- Keep changes on `feat/wearos-charm-client`; use small, descriptive commits and push only after tests.
- Update `clients/wearos/README.md` and `SPEC.md` with what is **actually** verified, correcting stale remote statements.
- Keep draft PR #3 **draft and unmerged**.
- Final report must separate: **observed pass / observed fail / untested**, commits and CI URL, installed APK signing status, fixes made, remaining risks, and whether daily home use remains viable.

## 4. Non-goals / operating constraints

- No Muse firmware/service; no new cloud account; no other brain, memory, tools or TTS on watch.
- No full Embodiment Android application on Wear OS.
- No Tailscale/mobile relay or remote access in this acceptance cycle; don't assume the watch inherits a paired phone VPN.
- No public forwarding of PatchBay's unauthenticated WebSocket.
- No new protocol / turn isolation project unless the existing shared-session behavior is demonstrated to break watch use and the owner approves a separate scope.
- No wholesale refactor or enterprise control framework.
- No reset, uninstall, or signing-key replacement without the owner's explicit approval.
- Follow repository `AGENTS.md` and `docs/contracts/ws-client-protocol.md`.

## 5. Stop conditions

Stop and ask the owner if any of the following occurs:
- The expected local `f2fa4a4` commit, branch, or device-tested fixes cannot be found.
- A dirty or diverged Git history cannot be reconciled by a safe fast-forward/cherry-pick.
- A fix requires restarting production PatchBay, modifying server-global voice VAD, adding a companion app or changing its WebSocket protocol.
- Debug APK signing mismatch would require uninstall and loss of device settings.
- A proposed cleanup includes files not created by this session or whose origin is unclear.

Everything else: perform focused investigation, implement minimal verified fixes, test them on device and CI, and document the result.
