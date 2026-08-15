// Avatar pane glue: TalkingHead (3D head render) + HeadAudio (audio-driven
// viseme detection), wired to the client's own PCM playback path. Loaded ONLY
// via dynamic import() from index.html (desktop + pane-enabled gate lives
// there) — this module assumes it will only ever run in that context.
//
// Lifecycle is entirely our responsibility: TalkingHead does not stop
// rendering on tab-hide, does not react to WebGL context loss, and has no
// concept of a "collapsed" state — all three are implemented below.
import { TalkingHead } from "./vendor/talkinghead/talkinghead.mjs";
import { HeadAudio } from "./vendor/headaudio/headaudio.mjs";

// Semantic best-fit from the page's 7 MOOD_VALUES onto TalkingHead's 8 moods
// (neutral, happy, angry, sad, fear, disgust, love, sleep). "thinking" and
// "serious" have no good match (see integration report) — mapped to the
// closest non-misleading option rather than left unmapped.
const MOOD_MAP = {
  neutral: "neutral",
  happy: "happy",
  excited: "happy",
  thinking: "neutral",
  concerned: "sad",
  playful: "love",
  serious: "angry",
};

const MAX_CONTEXT_REBUILDS = 3;
const AVATAR_URL = "./avatar/model/brunette-t.glb";
const HEADWORKLET_URL = "./avatar/vendor/headaudio/headworklet.mjs";
const HEADAUDIO_MODEL_URL = "./avatar/vendor/headaudio/model-en-mixed.bin";

// Semantic slot -> real mesh/material name(s), keyed per naming convention
// found in the vendored GLBs (confirmed live via scene traversal, not
// assumed): RPM/Wolf3D for brunette-t.glb, AvatarSDK's own scheme for
// avatarsdk.glb. A theme's `materials` block only ever speaks slot names;
// this map is the only place RPM-vs-other naming is an implementation
// detail. Unmapped slot for a given model = silently skipped (no match
// found while traversing == nothing to tint).
const SLOT_TO_MATERIAL = {
  hair: ["Wolf3D_Hair"],
  skin: ["Wolf3D_Skin", "Wolf3D_Body", "AvatarHead", "AvatarBody"],
  top: ["Wolf3D_Outfit_Top", "outfit_top"],
  eyes: ["Wolf3D_Eye", "AvatarLeftEyeball", "AvatarRightEyeball"],
};
// Every RPM-style material defaults to a white (no-op multiply) base color
// with the actual look baked into its diffuse texture map (confirmed via
// recon: all slots read back #ffffff on the stock brunette-t.glb) — so
// "#ffffff" is the correct reset-to-native value for an absent slot,
// independent of which GLB is currently loaded.
const BASE_MATERIALS = { hair: "#ffffff", skin: "#ffffff", top: "#ffffff", eyes: "#ffffff" };
// Mirrors TalkingHead's own constructor defaults (see talkinghead.mjs opt
// defaults) so an absent lighting field resets to the library's native look.
const BASE_LIGHTING = { ambientColor: "#ffffff", ambientIntensity: 2, keyColor: "#8888aa", keyIntensity: 30 };
// Matches buildHead()'s own TalkingHead constructor option below — the reset
// value for a theme that doesn't specify cameraView.
const BASE_CAMERA_VIEW = "head";

export async function initAvatar(mountNode, fallbackNode, hooks) {
  let head = null;
  let ha = null;
  let tapNode = null;
  let userExpanded = true;
  let rebuildAttempts = 0;
  let currentModelUrl = AVATAR_URL;
  let themeGen = 0; // supersede guard: only the most recent applyAvatarTheme() call may land
  const debugState = { visemeEvents: 0 };

  function applyRunState() {
    if (!head) return;
    const shouldRun = userExpanded && document.visibilityState === "visible";
    if (shouldRun && !head.isRunning) head.start();
    else if (!shouldRun && head.isRunning) head.stop();
  }
  document.addEventListener("visibilitychange", applyRunState);

  function showFallback(message) {
    if (mountNode) mountNode.hidden = true;
    if (fallbackNode) {
      fallbackNode.textContent = message;
      fallbackNode.hidden = false;
    }
  }

  function attachContextGuard(canvas) {
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
    }, false);
    canvas.addEventListener("webglcontextrestored", async () => {
      rebuildAttempts += 1;
      if (rebuildAttempts > MAX_CONTEXT_REBUILDS) {
        showFallback("Avatar unavailable (context lost)");
        return;
      }
      try {
        if (head) { try { head.dispose(); } catch { /* already gone */ } head = null; }
        await buildHead();
        head.opt.update = ha ? ha.update.bind(ha) : null;
        rebuildAttempts = 0; // successful rebuild — don't let a single flaky loss count toward the cap
      } catch {
        showFallback("Avatar unavailable (context lost)");
      }
    }, false);
  }

  async function buildHead() {
    head = new TalkingHead(mountNode, {
      cameraView: "head",
      cameraRotateEnable: false,
      modelFPS: 30,
      lipsyncLang: "en",
      // Only "en" is vendored — TalkingHead's constructor eagerly dynamic-imports
      // every entry in lipsyncModules (default ['fi','en','lt']); leaving the
      // default in place 404s two unhandled module imports on every load.
      lipsyncModules: ["en"],
    });
    attachContextGuard(head.renderer.domElement);
    await head.showAvatar({ url: AVATAR_URL, body: "F", lipsyncLang: "en" });
    applyRunState();
  }

  async function setupHeadAudio() {
    const audioCtx = hooks.getPlayCtx();
    try { await audioCtx.audioWorklet.addModule(HEADWORKLET_URL); }
    catch (e) { /* processor may already be registered from a prior avatar load; new HeadAudio() below is the real gate */ }
    ha = new HeadAudio(audioCtx, {
      processorOptions: { visemeEventsEnabled: true },
      parameterData: { vadGateActiveDb: -40, vadGateInactiveDb: -60 },
    });
    await ha.loadModel(HEADAUDIO_MODEL_URL);
    ha.onvalue = (key, value) => {
      if (!head) return;
      Object.assign(head.mtAvatar[key], { newvalue: value, needsUpdate: true });
      debugState.visemeEvents += 1;
    };
    // Pure analysis sink: gain node the playback path connects into IN
    // ADDITION to its existing destination connection (see hooks.setTap
    // caller in index.html) — never itself connected to destination, so it
    // cannot double the audio output.
    tapNode = audioCtx.createGain();
    tapNode.gain.value = 1;
    tapNode.connect(ha);
    hooks.setTap(tapNode);
  }

  // Head render is REQUIRED (no head = nothing to show, fall back). Lip-sync
  // is OPTIONAL — a HeadAudio failure (e.g. AudioWorklet unavailable on a
  // plain-HTTP, non-secure-context origin) degrades to a silent, motionless-
  // mouth head rather than killing the whole pane.
  try {
    await buildHead();
  } catch {
    showFallback("Avatar unavailable");
    return { start() {}, stop() {} };
  }
  try {
    await setupHeadAudio();
  } catch (e) {
    ha = null;
    console.warn("[avatar] lip-sync unavailable:", e.message);
  }
  head.opt.update = ha ? ha.update.bind(ha) : null;

  window.__avatarDebug = {
    get head() { return head; },
    get ha() { return ha; },
    get visemeEvents() { return debugState.visemeEvents; },
    get rebuildAttempts() { return rebuildAttempts; },
  };
  window.__avatarMood = (mood) => {
    head?.setMood(MOOD_MAP[mood] || "neutral");
  };

  function applyMaterials(materials) {
    if (!head || !head.armature) return;
    const wanted = Object.assign({}, BASE_MATERIALS, materials);
    head.armature.traverse((obj) => {
      if (!obj.isMesh) return;
      const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
      for (const [slot, matNames] of Object.entries(SLOT_TO_MATERIAL)) {
        const hex = wanted[slot];
        mats.forEach((m) => {
          if (m && m.color && matNames.includes(m.name)) m.color.set(hex);
        });
      }
    });
  }

  function applyLighting(lighting) {
    if (!head) return;
    const l = Object.assign({}, BASE_LIGHTING, lighting);
    head.setLighting({
      lightAmbientColor: l.ambientColor,
      lightAmbientIntensity: l.ambientIntensity,
      lightDirectColor: l.keyColor,
      lightDirectIntensity: l.keyIntensity,
    });
  }

  // Applies a theme's optional `avatar` config block (model/materials/
  // lighting/cameraView/defaultMood — see themes/THEME-SPEC.md). Every field
  // is optional; an absent field resets to its BASE_* default so switching
  // themes back and forth is idempotent (no drift). Supersede-safe: if a
  // newer call arrives while a model swap is in flight, the stale one bails
  // out after its await points instead of clobbering the newer result.
  async function applyAvatarTheme(themeCfg) {
    if (!head) return; // avatar not loaded yet, or context lost — no-op
    const gen = ++themeGen;
    const cfg = themeCfg || {};
    const wantUrl = cfg.model || AVATAR_URL;
    if (wantUrl !== currentModelUrl) {
      try {
        await head.showAvatar({ url: wantUrl, body: "F", lipsyncLang: "en" });
      } catch (e) {
        console.warn("[avatar] theme model swap failed:", e.message);
        return; // leave currentModelUrl unchanged — still reflects what's actually on screen
      }
      if (gen !== themeGen) return; // a newer theme change already won — don't clobber it
      currentModelUrl = wantUrl;
      applyRunState(); // showAvatar() calls this.stop() internally; resume per current run state
    }
    if (gen !== themeGen) return;
    applyMaterials(cfg.materials || {});
    applyLighting(cfg.lighting || {});
    head.setView(cfg.cameraView || BASE_CAMERA_VIEW);
    // mood is LLM-owned live state; defaultMood only seeds when present, never resets (would stomp set_mood).
    if (cfg.defaultMood) window.__avatarMood?.(cfg.defaultMood);
  }
  window.__avatarTheme = (themeCfg) => {
    applyAvatarTheme(themeCfg).catch((e) => console.warn("[avatar] theme apply failed:", e.message));
  };

  // Full teardown so the mount can host the other (2D) module. Called at most
  // once per controller during a live head swap. Stops rendering, removes the
  // visibilitychange listener (SAME fn ref — a leaked one would keep toggling
  // this dead module's run-state), bumps themeGen so any in-flight model swap
  // bails, disconnects the audio graph THIS module built (NOT the shared
  // playback context), disposes TalkingHead's WebGL, drops its canvas, and
  // deletes the globals it installed.
  function destroy() {
    userExpanded = false;
    themeGen += 1; // supersede any in-flight applyAvatarTheme so it can't re-run against a dead head
    document.removeEventListener("visibilitychange", applyRunState);
    try { head?.stop(); } catch {}
    try { ha?.disconnect(); } catch {}
    try { tapNode?.disconnect(); } catch {}
    const canvasEl = head?.renderer?.domElement;
    try { head?.dispose(); } catch {}
    try { canvasEl?.remove(); } catch {}
    head = null;
    try { delete window.__avatarDebug; delete window.__avatarMood; delete window.__avatarTheme; } catch {}
  }

  return {
    start() { userExpanded = true; applyRunState(); },
    stop() { userExpanded = false; applyRunState(); },
    destroy,
  };
}
