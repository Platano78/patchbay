---
title: "Vision capability — shared cross-project contract"
date: 2026-07-10
type: reference
status: LIVE
applies_to:
  - patchbay (LAN-server vision: webclient → streamer → Gemma-12B :8084)
  - a phone-embodiment client (on-device vision: Gemma E2B mmproj, offline)
relates_to: embodiment-camera-vision, voice-pipeline-tested
---

# Vision capability — shared contract

Two projects implement "point at the world → tell me / list what you see." Their **runtimes are
deliberately different**; this doc is the **portable layer** (intent phrases, schemas, prompts,
lessons) both implement, so the next vision feature starts from the matured version instead of a
re-derivation. **Share the contract, not the runtime.**

## Runtimes (do NOT merge)
| | Phone-embodiment client | Voice cockpit |
|---|---|---|
| Vision model | on-device **Gemma E2B** (llama.cpp mmproj/mtmd, JNI) | LAN-server **Gemma-12B** (`:8084`, OpenAI API) |
| Deployment | **offline, phone-IS-body** (Faulkner rejected phone-as-thin-client) | phone = thin client → LAN server-brain |
| Trigger | **deterministic** `IntentTriage` regex route (small model won't tool-call) | LLM **tool-call** `look` (12B tool-calls reliably — verified) |
| Structured | GBNF `json_schema_to_grammar` (native) | `response_format: json_schema` (`:8084`) |
| Frame | CameraX FGS → cacheDir/camera_in.jpg (downscaled 1024) | webclient JPEG (512px) → ws `camera_frame` → `/dev/shm` |

A **shared vision service** is the wrong move: it would force the phone-embodiment client onto the LAN
and break offline-first. Convergence, if ever: the phone-embodiment client's roadmapped **MCP client**
could call the 12B as an
*optional* higher-quality lane when home — on-device E2B stays the offline default.

## 1. Intent phrase sets (portable — regex/word-lists)
- **Describe intent** (`isCameraVisual`): "look at this", "identify this", "scan this", "what am I
  looking at"; bare "what is this/that" **only when terminal** (so it doesn't hijack "what is this
  app/song"). Never claim a "screen" utterance.
- **Scan/list intent** (`isCameraScan`, a stricter subset → structured mode): `scan`, `make a list`,
  `list the/what/everything`, `itemize`, `inventory`, `read the receipt/label/menu/sign/text`,
  `what items/objects/things`. Plain "scan this" alone stays describe.
- Cockpit port lives in `look.py::_SCAN_RE`; the phone-embodiment client in `IntentTriage.kt`.

## 2. Schemas (portable — the `describe → do` upgrade)
Free-text is *for a human to hear*; JSON is *for tools to consume*. Constrain with grammar/json_schema —
do **not** prompt-only (small models drift/add prose).
- **List**: `{"items": string[]}` → spoken "I see a, b, and c."
- **Receipt** (example of the acting payload): `{"merchant", "total", "date", "items": [...]}`.

## 3. Prompt templates
- describe: `"<user question>. Answer in 1-2 short spoken sentences, plain text, no markdown or lists."`
- scan: `"List the distinct objects and items visible in this image."` + the list json_schema.

## 4. Design lessons (paid for once — reuse everywhere)
1. **Deterministic route beats an LLM vision-tool when the model is small** — the phone-embodiment client rejected an
   `describe_camera` tool because E2B won't reliably tool-call. The cockpit's 12B *does* (verified), so
   the tool is fine there; but a deterministic pre-LLM route also removes the "Let me check" round-trip.
   (Cockpit currently does deterministic *mode* selection inside the tool; a full pre-LLM route is an
   optional future enhancement.)
2. **Constrain structured output with a grammar / json_schema**, never prompt-only.
3. **Release the scarce vision slot before TTS** (phone-embodiment client "3c" bug): holding the single-flight latch
   across a blocking `speak()` wedged vision for *minutes*. Free the slot the moment describe returns;
   speak outside the latch.
4. **Camera OFF by default** (privacy); send/capture frames only while enabled; gate on frame freshness
   (cockpit: 10s staleness → "can't see"; degrade calmly, never crash).
5. **Async ack for slow vision**: ack ("taking a look…") immediately, speak the real answer when the
   ~vision-latency later resolves.
6. Platform gotchas are project-specific (OxygenOS FGS-from-tile, camera single-slot) — keep those in
   the project doc, not here.

## 5. Implementation pointers
- **Cockpit**: `voice-tools/look.py` (describe + scan), `websocket_streamer.py` `camera_frame` →
  `/dev/shm`, `webclient/index.html` camera toggle. Spec: `patchbay/docs/plans/camera-vision-lane_spec.md`.
- **Phone-embodiment client**: `IntentTriage.kt` (`isCameraVisual`/`isCameraScan`), `CameraLook*`, `CameraController`
  (describe half + LIST_SCHEMA), `llama_jni` GBNF path. Spec: `embodiment/docs/camera-vision_spec.md`.
  Memory: [[embodiment-camera-vision]].
