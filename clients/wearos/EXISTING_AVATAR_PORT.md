# PatchBay Existing Characters — Wear OS / ESP32 avatar adaptation

**Date:** 2026-11-01  
**Purpose:** Correct the wearable-avatar design brief. **PatchBay already has characters. Do not design another mascot, cast, or brand identity.**

## Design decision (owner-directed)

Use the **characters already available in PatchBay's existing avatar system** as the visual characters for the Galaxy Watch6 Classic, Waveshare ESP32-S3-Touch-AMOLED-1.75C, and eventually EchoBay. The earlier generated Rook/Vee/Vale concept board and generic neon robots are **rejected as new identities**. Any additional design should be **expressions, animation, framing, and adaptation of existing models**, not character invention.

PatchBay is one self-hosted voice assistant with multiple device interfaces. Watch6 voice behavior already works. Avatar adaptation is client-side presentation work, not a new brain, persona, TTS, assistant, or cloud dependency.

## Inspect these actual sources first

1. `webclient/index.html`, section `AVATAR_REGISTRY` (around line 8111 in the main branch). **Actual source of truth** for selectable avatar IDs, names, GLB paths and selection. Do not hand-code a second authoritative roster or infer the roster from old screenshots.
2. `webclient/avatar/model/*.glb` — actual source character models.
3. `webclient/avatar/model/LICENSE-NOTE.txt` and root `NOTICE` — per-asset redistribution rights. Root MIT license does **not** override GLB asset terms.
4. `webclient/avatar/avatar.mjs` — TalkingHead 3D rendering, HeadAudio-driven visemes, mood mapping, theme skin/material support and lifecycle.
5. `webclient/avatar/avatar2d.mjs` — pre-existing 2D still-image lip-sync path. It currently references `./avatar/refs/monarch.png`, which is absent from the public repository by design.
6. `webclient/avatar/local/registry.mjs` and `webclient/avatar/local/` — optional gitignored *local* avatars; check the real host for installed local avatars. Shadow Monarch cinematic assets are an example, **not** an asset to regenerate or publish.
7. `.gitignore`, `webclient/themes/THEME-SPEC.md` — protected local-only asset boundaries.
8. `clients/wearos/` — inspect current feature branch and deployed app before editing anything; the phone/watch audio client is already operational.

### Roster observed in GitHub's main-branch registry on 2026-11-01

**Six original Forge heads already made for this project:**
- `forge-old-man` → `old-man.glb`
- `forge-young-woman` → `young-woman.glb`
- `forge-heavyset` → `heavyset-neutral.glb`
- `forge-prof-woman` → `professional-woman.glb`
- `forge-young-man` → `young-man.glb`
- `forge-elder-woman` → `elder-woman.glb`

**Six other bundled existing heads:**
- `brunette` → `brunette-t.glb`
- `avatarsdk` → `avatarsdk.glb`
- `avaturn` → `avaturn.glb`
- `mpfb` → `mpfb.glb`
- `vroid` → `vroid.glb`
- `brunette-a` → `brunette.glb`

**Additional local-only avatars:** inspect the live host's local registry; do not assume any local asset exists or is redistributable. A public clone will not have private Monarch art/rigs.

The above is a **snapshot**, not a promise about what is installed in the running host today. Confirm current `AVATAR_REGISTRY` and optional local overlay before implementation.

## Licensing boundary

- Six Forge heads use CC0 mesh/morph/texture assets and a TalkingHead-derived rig attributed under MIT. `mpfb.glb` is CC0.
- Remaining bundled third-party example heads have varying non-commercial restrictions; read per-file notes before deriving assets or committing exported art.
- Shadow Monarch/local artwork is intentionally gitignored and may be copyright-encumbered. Keep it private and host-local. Do not invent "replacement" art or publish it in the public repo.
- Generate public distributable sprites/atlases only from assets with appropriate rights; record source model, exact ID, derivation method and attribution in an asset manifest. Do not blanket-import every GLB into Wear OS or the public ESP32 client.

## Concrete technical direction

**Pilot one existing permitted Forge character first** (owner chooses using actual previews; no new character), then generalize to existing roster with the same stable avatar IDs. This minimizes work while protecting the living Watch6 implementation.

- Use offline rendering from the **actual GLB character and its morph targets/face units** to make high-quality head-and-shoulders frame(s) and expression sprites. Prefer the existing model's animation/viseme capabilities. Do not ask an image model to redraw or reinterpret the character: that would break identity consistency.
- For the Watch6: compare native lightweight rendering vs pre-rendered 2D sprite atlases; choose the approach grounded in watch memory, battery and performance. A Wear OS UI should not import browser TalkingHead + three.js wholesale. Keep the current PTT and audio flow untouched.
- For the Waveshare 1.75C: prefer optimized 2D sprite assets using its own ESP-IDF/LVGL BSP and device RAM/flash limits; there is no requirement to run an interactive GLB renderer on an ESP32.
- At 432–466px round-screen resolution, compose a close head shot, with eyes/brows and mouth large enough to read; preview on a real 47mm Watch6 before committing to a frame size. Provide actual screenshots of 1:1 circle clipping, including chin and hair.
- Separate conversation phases (Ready, Listening, Working, Speaking, Error) from emotional expression (neutral, happy, concerned etc.). The web avatar currently has its own `MOOD_MAP` — inspect its semantics, do not invent LLM emotions or claim an actual mood signal is available on every device.
- Drive speech animation from audio *actually played on the watch*, not audio packet arrival; preserve the validated playback drain and tool-wait fixes. Amplitude-based opening is not viseme-perfect lip-sync; offline viseme frames only improve realism if there is a real timing/control signal.
- Avatar selection in the web cockpit currently uses browser `localStorage` key `va-avatar-model`. **It is NOT yet a shared server-wide avatar setting.** Do not assert the watch will automatically follow the browser selection. Initial device-local character selection is acceptable. If synchronized selection is desired, specify and scope the minimal change separately, not in this character adaptation.
- Lifecycle: idle subtle blinks / listening attentive eyebrows / working non-misleading anticipation / speaking mouth and expression / app background halts animation and audio. Respect reduced-motion and watch battery.

## Acceptance / handoff for Claude Code

1. Locate and inventory **all current** public and local existing avatars, including actual availability, file path, registry ID, license and usable facial morph targets. Use GitHub plus the owner's local machine; explicitly call out unavailable assets.
2. Show side-by-side **real renders from the existing GLB models** at actual Watch6 and 1.75C display sizes, not AI-generated substitute faces.
3. Make an expression and mouth-frame proof of concept for **one existing Forge character**, with matching neutral/listening/speaking/thinking states, preserving identity across every frame.
4. If owner approves, implement it *behind a selectable rendering option* on Wear OS without breaking existing Watch6 speech or changing the selected default without approval.
5. Test actual animation on the physical Watch6 using its real PCM playback; report memory/battery/frame rate, mouth/audio timing, full-turn reliability.
6. For the ESP32, record a future adapter plan only. Hardware is ordered but not yet available; do not claim physical validation or flash a different board.
7. Do not modify production PatchBay, merge PR #3, or change ongoing Watch6 acceptance work without coordination. Work on a separate clean branch/worktree and present previews for approval.

**Success means:** the same **existing PatchBay character** is immediately recognizable on the cockpit and wearable; we have *adapted* the character instead of invented a new one.
