// Avatar pane glue: 2D still-image render (Monarch) + HeadAudio (audio-driven
// viseme detection), wired to the client's own PCM playback path. This is the
// 2D sibling of ./avatar.mjs (3D TalkingHead path) — same module contract so
// index.html can dispatch to either. Loaded ONLY via dynamic import() from
// index.html; assumes it will only ever run in that context.
//
// Lifecycle is entirely our responsibility: unlike avatar.mjs there is no
// underlying render library to delegate start/stop to — the rAF loop and
// HeadAudio's start()/stop() are both driven directly below, and (unlike the
// 3D path) HeadAudio's update(dt) is driven from OUR rAF loop rather than
// from a render library's own loop.
import { HeadAudio } from "./vendor/headaudio/headaudio.mjs";

const MONARCH_URL = "./avatar/refs/monarch.png";
const HEADWORKLET_URL = "./avatar/vendor/headaudio/headworklet.mjs";
const HEADAUDIO_MODEL_URL = "./avatar/vendor/headaudio/model-en-mixed.bin";

// R3 v1 calibrated baseline (splitFrac / oval geometry) — do NOT change
// without re-calibrating against the reference still.
const SPLIT_FRAC = 0.565;
const REST_OPEN = 0.15;

// Per-viseme mouth-open contribution. HeadAudio's onvalue reports each
// viseme's activation independently; the instantaneous "how open" signal is
// the max across all currently-nonzero visemes.
const OPEN_SCALE = {
  viseme_aa: 1, viseme_O: 1, viseme_U: 0.9, viseme_E: 0.7, viseme_I: 0.65,
  viseme_nn: 0.5, viseme_RR: 0.5, viseme_CH: 0.5, viseme_PP: 0.2, viseme_FF: 0.25,
  viseme_SS: 0.3, viseme_TH: 0.3, viseme_DD: 0.35, viseme_kk: 0.35, viseme_sil: 0,
};

export async function initAvatar2D(mountNode, fallbackNode, hooks) {
  let ha = null;
  let userExpanded = true;
  let running = false;
  let rafHandle = null;
  let lastFrameTime = null;
  const visemeVals = {};
  let visemeEventCount = 0;

  let img = null;
  let cv = null;
  let ctx = null;
  let offscreen = null;
  let octx = null;
  let ready = false;
  let curOpen = REST_OPEN;
  let tapNode = null;

  function showFallback(message) {
    if (mountNode) mountNode.hidden = true;
    if (fallbackNode) {
      fallbackNode.textContent = message;
      fallbackNode.hidden = false;
    }
  }

  function applyRunState() {
    const shouldRun = userExpanded && document.visibilityState === "visible";
    if (shouldRun && !running) startRunning();
    else if (!shouldRun && running) stopRunning();
  }
  document.addEventListener("visibilitychange", applyRunState);

  // R3 v1 mouth renderer, feathered: the prototype used a hard ctx.clip()
  // ellipse to cut in the displaced mouth region, which leaves a visible
  // seam. Here the displaced region is built on an offscreen canvas, then
  // masked with an elliptical radial-gradient alpha ramp (destination-in)
  // before compositing over the untouched base — same geometry, soft edge.
  function setOpen(a) {
    if (!ready) return;
    curOpen = Math.max(0, Math.min(1, a));
    const W = cv.width, H = cv.height;
    const sy = Math.round(H * SPLIT_FRAC), off = Math.round(curOpen * H * 0.06);
    const cx = W * 0.5, rx = W * 0.30, ry = H * 0.10;

    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(img, 0, 0, W, H, 0, 0, W, H); // full base, untouched

    octx.clearRect(0, 0, W, H);
    octx.drawImage(img, 0, 0, W, sy, 0, 0, W, sy); // upper teeth re-laid
    const g = octx.createLinearGradient(0, sy, 0, sy + off);
    g.addColorStop(0, "rgba(26,4,6,0.4)"); // softened top edge, blends into upper region
    g.addColorStop(1, "#050203");
    octx.fillStyle = g;
    octx.fillRect(0, sy, W, off); // mouth interior gap
    octx.drawImage(img, 0, sy, W, H - sy, 0, sy + off, W, H - sy); // lower teeth/lip drop

    // Elliptical feather mask: alpha 1 out to ~0.55 of the radius, ramping
    // to alpha 0 at the radius, so there is no hard oval outline.
    octx.save();
    octx.globalCompositeOperation = "destination-in";
    octx.translate(cx, sy);
    octx.scale(1, ry / rx);
    const mask = octx.createRadialGradient(0, 0, 0, 0, 0, rx);
    mask.addColorStop(0, "rgba(0,0,0,1)");
    mask.addColorStop(0.55, "rgba(0,0,0,1)");
    mask.addColorStop(1, "rgba(0,0,0,0)");
    octx.fillStyle = mask;
    octx.fillRect(-rx, -rx, rx * 2, rx * 2);
    octx.restore();

    ctx.drawImage(offscreen, 0, 0);
  }

  function frame(t) {
    rafHandle = requestAnimationFrame(frame);
    if (lastFrameTime === null) { lastFrameTime = t; return; }
    const dt = t - lastFrameTime; // ms — ha.update() expects ms (internally da = dt/100)
    lastFrameTime = t;
    if (!ha) return; // degrade path: no HeadAudio, hold the still at rest
    ha.update(dt);
    let maxOpen = 0;
    for (const [key, scale] of Object.entries(OPEN_SCALE)) {
      const v = (visemeVals[key] || 0) * scale;
      if (v > maxOpen) maxOpen = v;
    }
    setOpen(maxOpen);
  }

  function startRunning() {
    running = true;
    ha?.start();
    lastFrameTime = null;
    rafHandle = requestAnimationFrame(frame);
  }

  function stopRunning() {
    running = false;
    if (rafHandle !== null) { cancelAnimationFrame(rafHandle); rafHandle = null; }
    ha?.stop();
  }

  async function loadStill() {
    ctx = cv.getContext("2d");
    if (!ctx) throw new Error("2D context unavailable");
    octx = offscreen.getContext("2d");
    await new Promise((resolve, reject) => {
      img = new Image();
      img.onload = resolve;
      img.onerror = () => reject(new Error("monarch image failed to load"));
      img.src = MONARCH_URL;
    });
    cv.width = offscreen.width = img.naturalWidth;
    cv.height = offscreen.height = img.naturalHeight;
    ready = true;
    ctx.drawImage(img, 0, 0);
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
      visemeVals[key] = value;
      visemeEventCount += 1;
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

  cv = document.createElement("canvas");
  cv.className = "avatar2d"; // scoped CSS hook: object-fit letterboxing (2D still is portrait; pane is landscape)
  offscreen = document.createElement("canvas");
  if (mountNode) mountNode.appendChild(cv);

  // Head render is REQUIRED (no still = nothing to show, fall back). Lip-sync
  // is OPTIONAL — a HeadAudio failure (e.g. AudioWorklet unavailable on a
  // plain-HTTP, non-secure-context origin) degrades to a silent, motionless
  // still rather than killing the whole pane.
  try {
    await loadStill();
  } catch {
    showFallback("Avatar unavailable");
    return { start() {}, stop() {} };
  }
  try {
    await setupHeadAudio();
  } catch (e) {
    ha = null;
    console.warn("[avatar2d] lip-sync unavailable:", e.message);
  }

  window.__avatar2dDebug = {
    get ha() { return ha; },
    get visemeEvents() { return visemeEventCount; },
    get currentOpen() { return curOpen; },
    setOpen,
  };

  // Full teardown so the mount can host the other (3D) module. Called at most
  // once per controller during a live head swap. Stops the rAF loop + HeadAudio,
  // removes the visibilitychange listener (SAME fn ref — a leaked one would keep
  // toggling this dead module's run-state), disconnects the audio graph THIS
  // module built (NOT the shared playback context), drops the canvas, and
  // deletes the global it installed.
  function destroy() {
    userExpanded = false;
    stopRunning();
    document.removeEventListener("visibilitychange", applyRunState);
    try { ha?.disconnect(); } catch {}
    try { tapNode?.disconnect(); } catch {}
    cv?.remove();
    try { delete window.__avatar2dDebug; } catch {}
  }

  return {
    start() { userExpanded = true; applyRunState(); },
    stop() { userExpanded = false; applyRunState(); },
    destroy,
  };
}
