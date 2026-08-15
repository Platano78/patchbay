# Patchbay Theme Spec

> **STATUS (2026-08-15, instrument port):** the client's base look is now the
> **Analog Studio Instrument** faceplate (`data-theme="instrument"` is the only
> built-in; the instrument palette lives directly on `:root` with the same 23-token
> contract). The former built-ins (`editorial`, `console`, `hifi`) were parked to
> git history — their blocks referenced the pre-port DOM and will return as
> `[data-theme]` skins rebuilt against the new markup in a later theming phase.
> Custom themes still work exactly as described below, but skins written against
> the pre-2026-08-15 DOM need re-validation against the new structure
> (`webclient/revamp-mockups/instrument/DESIGN-LANGUAGE.md` documents the new
> primitives). Where the text below cites `editorial`/`console`/`hifi` as worked
> examples, read them from git history (`git show cba3f4c:webclient/index.html` — the last pre-port tree).

Written for an LLM (or a human) tasked with building the user a **custom theme**
for the Patchbay web client (`webclient/index.html`).

## The one rule that matters

**Themes are CSS-only.** You are never allowed to touch `index.html`'s DOM structure
or JavaScript to build a theme. Every visual identity — colors, type, texture,
component shapes, PTT button treatment, VU/waveform styling — must be expressible
as CSS custom-property overrides plus ordinary CSS rules scoped under a
`[data-theme="<your-id>"]` attribute selector. If you find yourself wanting to add
a `<div>`, rename a class, or add an inline `style=` attribute in the HTML — stop.
The token set below is deliberately complete enough that you shouldn't need to.

**No external network requests from your CSS.** No `@import`, no `@font-face`
with a remote `src: url(...)`, no remote background images. System font stacks
and CSS-generated textures (gradients, `repeating-linear-gradient` scanlines/
stripes, `clip-path`, `box-shadow`, `filter`) only — everything a custom theme
needs must already be sitting on the visitor's machine or expressible in CSS.

## File convention

1. Create `themes/<id>.css` (this directory). `<id>` is a short kebab-case slug,
   e.g. `hacker`, `midnight`.
2. **Every rule in that file must be scoped under `[data-theme="<id>"]`.** Never
   write a bare `body { … }` or `.card { … }` — it would leak into every other
   theme. Always: `[data-theme="hacker"] .card { … }`.
3. Register the theme by adding one entry to `themes/themes.json` (see below).
4. That's it. No other files change. The page's loader (in `index.html`) fetches
   `themes.json` on boot, adds your theme to the picker, and injects
   `<link rel="stylesheet" href="themes/<id>.css">` the first time the user selects
   it. Built-in themes (`editorial`, `console`, `hifi`) never fetch anything —
   only custom ones do, and only on first selection.

## Registering in themes.json

`themes/themes.json` is a JSON array. Append an object:

```json
{ "id": "hacker", "name": "Hacker", "swatch": ["#0a0a0a", "#39ff14"] }
```

- `id` — matches your CSS file's `[data-theme="..."]` value and its filename.
- `name` — short label shown under the swatch in the settings drawer.
- `swatch` — exactly two hex colors used to paint the picker's split-circle swatch
  (background color, then accent color, is the usual choice).

## The complete token list

Every built-in theme block in `index.html` (`[data-theme="editorial"]`,
`[data-theme="console"]`, `[data-theme="hifi"]`) redefines this exact set of
23 custom properties. Your custom theme's CSS file should redefine the same set
under your own `[data-theme="<id>"]` selector — anything you don't override falls
through to whatever theme was active before (usually editorial's `:root`
fallback), which is rarely what you want, so redefine all of them.

| Token | Role |
|---|---|
| `--bg` | Page background, behind everything |
| `--surface` | Card / panel background (transcript card, assistant card, PTT button base, log, drawer, inputs) |
| `--surface-2` | Secondary panel background — history rail, session rail, recessed/inset elements |
| `--border` | Hairline border / divider color used everywhere (cards, rails, inputs, drawer) |
| `--text` | Primary text color |
| `--text-muted` | Secondary text — labels, captions, meta, placeholder-ish text |
| `--accent` | Primary interactive color — PTT idle glow, focus rings, links, assistant reply text color |
| `--accent-2` | **Reserved.** Defined by all three built-in themes but not currently consumed by any core rule in `index.html` — overriding it has no visible effect unless you also write your own `[data-theme="<id>"]` rules that reference it. Safe to use freely in your own theme's custom rules (a second highlight tone, a hover color, whatever fits); just don't expect it to "do" anything on its own. |
| `--rec` | Recording / danger color — PTT active state, recording pulse ring |
| `--ok` | Connected / success status dot |
| `--warn` | Connecting / in-progress status dot |
| `--bad` | Error status dot, mic-denied state |
| `--radius` | Corner radius for cards, rails, panels |
| `--radius-sm` | Corner radius for small controls — buttons, inputs, log box |
| `--font-body` | UI chrome font — header, buttons, labels, drawer controls |
| `--font-display` | Hero font — desktop assistant-reply and transcript hero text (≥1024px). Pick something with real character: a serif, a monospace, whatever fits the theme's personality. Note: gothic/blackletter has no system-safe stack across platforms — for a dramatic/gothic-leaning feel use a high-contrast serif instead (`Didot`, `"Big Caslon"`, `Georgia`); for a stencil/poster feel use `Impact`, `"Arial Black"` |
| `--font-mono` | Technical/numeric readout font — history timestamps, session-rail values, the diagnostics log |
| `--tracking-label` | Letter-spacing for uppercase section labels ("You said", "Assistant", "History", "Session", etc.) |
| `--shadow-card` | box-shadow value applied to `.card`, `#historyRail`, `#sessionRail` — use `none` for a flat look, or a real shadow recipe for depth |
| `--ptt-shadow-idle` | box-shadow for the PTT button at rest |
| `--ptt-shadow-rec` | box-shadow for the PTT button while recording (usually a glow keyed off `--rec`) |
| `--ptt-size` | PTT button diameter/side-length in px — small and refined, or big and focal, your call |
| `--ptt-radius` | PTT button border-radius — `50%` for a circular orb/knob, a px value (e.g. `20px`) for a squarish "key" shape |

Anything your theme needs beyond color/font/shape (paper grain, brushed-metal
sheen, scanlines, a VU-meter needle glow, etc.) is exactly the kind of thing that
does NOT fit in a custom property — add it as an ordinary override rule in your
`themes/<id>.css`, e.g.:

```css
[data-theme="hacker"] body {
  background: linear-gradient(180deg, #050505 0%, #0a0a0a 100%);
}
[data-theme="hacker"] .wbar {
  box-shadow: 0 0 6px var(--accent);
}
```

## Structural hooks — what you may style, never rename

These ids/classes are load-bearing: the page's JavaScript selects them directly.
Restyle them freely (color, font, size, shape, shadow, spacing) but never rename,
remove, or restructure them, and never change what they mean.

**Layout containers:** `header`, `#contentGrid` (desktop 3-col grid: `#historyRail`
| `main` | `#sessionRail`), `main`.

**Cards / hero text:** `.card`, `#transcript` / `#transcriptBody` (user's words),
`#assistantText` / `#assistantBody` (assistant's reply — gets `.speaking` class
while TTS audio is actively playing back).

**PTT + waveform:** `#pttWrap`, `#ptt` (gets `.recording` class while the mic is
live — this is the ONLY reliable signal for "is the user talking right now"),
`.pttIcon`, `.pttPulse` (the expanding ring drawn only while `.recording`),
`#pttCaption`, `.waveform` / `#waveform`, `.wbar` (7 bars, JS drives their
`transform: scaleY(...)` inline per audio frame — don't fight that with a CSS
transition longer than ~100ms or the bars will lag visibly behind the audio).

**Settings drawer:** `#gearBtn`, `#settingsOverlay` / `.open`, `#settingsPanel` /
`.open`, `#settingsCloseBtn`, `#themeSwatches` (JS-populated; each entry is a
`.themeSwatch` button containing `.swatchDot` + `.swatchName`, gets `.active` on
the current theme), `#brainStatus`, `#brainList` (JS-populated `.brainOption`
rows), `#personaInput`, `#voiceLabel` / `#voiceSelect`, `.panelBtn` /
`.panelBtn.primary`.

**History rail (left, desktop only):** `#historyRail`, `#historyList`
(JS-populated `.historyEntry` rows, each with `.historyTime` / `.historyUser` /
`.historyAssistant`).

**Session rail (right, desktop only):** `#sessionRail`, `.sessionRow` /
`.sessionKey` / `.sessionVal`, `#sessionBrain`, `#sessionVoice`, `#sessionTools`,
`#sessionConn`, `.sessionPersona` / `#sessionPersonaSnippet`.

**Diagnostics:** `.diagToggle`, `#log` (uses `--font-mono`; keep it legible —
it's the escape hatch when something's broken).

**State classes you can key off of:** `#dot.connected` / `.connecting` /
`.error`, `#statusText.shimmer` (mid-request), `#ptt.recording`, `#assistantText.speaking`.

**Cockpit — quest card (top of `#sessionRail`, desktop only):** `#questCard`
(root — carries `data-cockpit-status`, one of `idle` / `sent` / `running` /
`done` / `error`; JS toggles the element's `hidden` attribute — visible
whenever a delegation is active or has result data, or a permission is
pending; hidden otherwise so the rail looks exactly like it did before this
card existed), `.questHeader` (base text "TASK" — restyle freely, including a
full CSS-`content` word swap, as long as the real text node stays in the DOM
for assistive tech — e.g.

```css
[data-theme="midnight"] .questHeader { font-size: 0; }
[data-theme="midnight"] .questHeader::before {
  content: "【 QUEST 】";
  font-size: 1rem;
}
```

), `#questTask` / `.questTaskText` (truncated task string, full text in the
`title` attribute), `#questStatusLine` / `.questStatusLine` (status label +
a live elapsed-time ticker while active — key color/animation off the
parent's `data-cockpit-status`), `#questUnreachable` / `.questUnreachable`
(shown when the server reports `hermes_ok: false`), `#questSteps` /
`.questSteps` (JS-populated, scrollable, `.questStep` rows each with
`.questStepRole` / `.questStepText`), `#questResult` / `.questResult` (shown
only when status is `done` or `error`).

**Cockpit — approval gate banner (fixed, ALL viewports — including mobile,
never hidden below `1024px` the way the rails are):** `#gateBanner` (JS
toggles `hidden`; visible whenever the server's permission list is
non-empty), `.gateDesc` / `.gateMore`, `.gateControls`, `#gateDeny` /
`.gateDenyBtn` (single-click deny, gets a native `disabled` attribute while a
response is in flight), `#gateApprove` / `.gateHold` (the hold-to-approve
control — gets `aria-disabled="true"` and a `.holding` class while a hold is
in progress), `#gateApproveFill` / `.gateHoldFill` (the progress fill; width
driven every frame by the JS-set `--hold-progress` custom property, `0` to
`1` — restyle color/shape freely, but the fill mechanism itself is JS-owned).

**Avatar pane (desktop only, lazy-loaded — top of the center column, above
the transcript/assistant cards; see `avatar/avatar.mjs`):** `#avatarPane`
(root container — `display:none` below `1024px` like the rails; `.collapsed`
class shrinks it to a thin strip when the user toggles it closed), `#avatarMount`
(the TalkingHead render target — the avatar module appends its own `<canvas>`
here; never add DOM of your own inside it), `#avatarFallback` (JS toggles
`hidden`; shown with a static message if the avatar module fails to load or
exhausts its WebGL-context-loss rebuild attempts), `#avatarToggle` (collapse/
expand chevron button — JS keeps its `aria-expanded` in sync).

**Mechanics you must never override** (in addition to never renaming/
removing/restructuring any hook above): `#gateApprove`'s `touch-action: none`
and `user-select: none` (dropping either breaks the hold gesture on
touchscreens — the whole point of `pointerdown`/`pointerup` being the only
source of truth for hold state), `#gateBanner`'s JS-driven `hidden` toggling
(a theme may restyle when-visible, never force it visible/invisible itself),
and the one-shot approve/deny guard states (`#gateApprove[aria-disabled]` /
`tabindex`, `#gateDeny[disabled]`) — a theme's `:hover`/`:active` rules may
style these states but must never use `pointer-events` or `!important` to
re-enable a control the script has disabled, since that reopens the
double-submit window the guard exists to close. For the avatar pane:
the `<canvas>` inside `#avatarMount` has its size and existence managed
entirely by `avatar/avatar.mjs` (TalkingHead's own `ResizeObserver` sizes it
off `#avatarMount`'s box) — never set `display`/`width`/`height` on the
canvas element itself in a theme rule. `#avatarPane.collapsed` mirrors
JS/localStorage state (`va-avatar`) — restyle its collapsed appearance
freely (the built-in rule shrinks `height`) but never rename the class or
fight its JS-driven transition with `!important`. `#avatarFallback`'s
JS-driven `hidden` toggling on load-failure/context-loss must never be
forced visible/invisible by a theme, same rule as `#gateBanner` above. The
avatar's 3D head model, material tinting, and lighting are theme-config-owned
via the `avatar` block in `themes.json` (see "Avatar theming" below) — never
hardcode head/model styling in a theme CSS file (CSS can't reach into WebGL
anyway, but the config-vs-code line is the same principle as everywhere else
in this spec).

## Avatar theming

A theme entry (built-in, in `index.html`'s `BUILTIN_THEMES`, or custom, in
`themes.json`) may carry an optional `avatar` block. Every field is optional;
an absent block, or an absent field within a present block, resets that
aspect to `avatar/avatar.mjs`'s own base default (not to whatever the
previous theme left behind) — so switching themes back and forth is always
idempotent, never additive drift.

```jsonc
"avatar": {
  "model": "./avatar/model/<file>.glb",        // omit = keep current/base GLB
  "materials": { "hair": "#hex", "skin": "#hex", "top": "#hex", "eyes": "#hex" },
  "lighting": { "ambientColor": "#hex", "ambientIntensity": 2, "keyColor": "#hex", "keyIntensity": 30 },
  "cameraView": "head",                         // passed straight to TalkingHead's setView()
  "defaultMood": "neutral"                      // one of the app's own mood values (see Mood system below), not a TalkingHead mood name
}
```

**`model`** — GLB URL. Different from the currently-loaded model → the avatar
module swaps it via TalkingHead's `showAvatar()` (which disposes/replaces the
previous armature/materials on its own — no manual cleanup needed). Supersede-
safe: a generation counter means a stale in-flight swap can never clobber a
newer theme change that arrived after it.

**`materials`** — semantic slot names, not raw mesh/material names. `hair` /
`skin` / `top` / `eyes` are the only slots. Real name mapping lives in
`avatar/avatar.mjs`'s `SLOT_TO_MATERIAL`, e.g. RPM/Wolf3D-convention GLBs use
`Wolf3D_Hair` / `Wolf3D_Skin` (mesh `Wolf3D_Head`) / `Wolf3D_Outfit_Top` /
`Wolf3D_Eye`; other vendored GLBs (e.g. `avatarsdk.glb`) use their own scheme
(`AvatarHead`/`AvatarBody`, `outfit_top`, `AvatarLeftEyeball`/
`AvatarRightEyeball`) and are mapped alongside. A slot with no matching
mesh/material on the currently-loaded model is skipped silently — this is
expected, not an error (e.g. `avatarsdk.glb` has no distinct hair mesh).
Colors are applied as `material.color.set(hex)`, a tint multiply over the
GLB's own diffuse texture — RPM-style materials default to white
(no-op tint), which is why an absent slot resets to `#ffffff` rather than to
some captured "original" value: white IS the model's native, untinted look
on any GLB built to the same convention.

**`lighting`** — drives TalkingHead's own lit-scene API (`head.setLighting()`
under the hood): `ambientColor`/`ambientIntensity` → the scene's ambient
light, `keyColor`/`keyIntensity` → its directional "key" light. The library's
separate spot light is not exposed here (left at its construction default in
every theme). Absent fields reset to TalkingHead's own constructor defaults
(`#ffffff` / `2` ambient, `#8888aa` / `30` key) — same idempotency guarantee
as materials.

**`cameraView`** / **`defaultMood`** — thin passthroughs (`head.setView()`,
the existing mood pipeline) for a theme that wants a different framing or
starting expression; most themes can leave both unset.

Reapplication is guaranteed after a model swap too (a fresh `showAvatar()`
call resets every material to the GLB's own defaults, so `materials`/
`lighting` are always re-applied immediately after any swap completes, not
just on a same-model theme change).

## Mood system

Alongside `data-theme`, `documentElement` also carries a `data-mood`
attribute — one of `neutral`, `happy`, `excited`, `thinking`, `concerned`,
`playful`, `serious`. Unlike the theme, mood is not user-chosen: the LLM sets
it itself mid-conversation by calling the `set_mood` tool (see
`voice_tools.py`); the client reads the tool call off the `assistant_text`
event's `tools` array and applies the attribute. It resets to `neutral` on
page load and whenever the chat is reset.

Themes MAY layer mood-specific styling with a compound selector:

```css
[data-theme="midnight"][data-mood="thinking"] #fxBack { filter: brightness(0.8); }
```

Rules:

- **Appearance only.** A mood rule may change color, glow, animation speed,
  opacity — never layout, and never a structural hook's meaning. The
  "Structural hooks" rules above (no renaming, no new elements, no DOM/JS
  changes) apply in full to mood rules too.
- **Graceful degradation.** A theme that defines zero mood rules must still
  look correct: `data-mood` simply has no matching selector and the theme's
  normal `[data-theme="<id>"]` look shows through unchanged. Never make a
  theme's base (mood-less) look conditional on a specific mood being set.
- **Recommended, not mandatory, semantic mapping** — a starting translation,
  not a spec: `thinking` → cooler/dimmer with slower pulses; `excited` →
  brighter with faster accent motion; `concerned` → warmer/amber shift;
  `serious` → desaturated and stiller. `neutral`, `happy`, `playful` are
  open to the theme's own interpretation.
- A mood rule that changes an `animation-duration` still inherits whatever
  `@media (prefers-reduced-motion: reduce)` rule already disables that
  animation for the theme — don't reintroduce motion reduced-motion turned
  off.

No built-in theme currently layers mood-specific rules — the compound-selector
pattern above is the whole technique; apply it in your own `themes/<id>.css`.

## Component fidelity — the extra mile beyond recoloring

Swapping the 23 tokens gets you a same-shaped page in your palette. That is
the floor, not the ceiling. If you have a design reference (a Stitch comp, a
screenshot, a mood board), **treat it as the spec, not inspiration** — match
its component-level treatments (meter style, button shape and material,
transcript-panel texture, dividers/labels), not just its colors. The built-in
`hifi` and `console` themes are the worked examples: both start from the same
23 tokens as `editorial`, then layer component fidelity on top using the
primitives below. Read their `[data-theme="hifi"]` / `[data-theme="console"]`
blocks in `index.html` before building your own — they show the pattern:
restyle the mount points, don't fight the token system.

## Theme primitives — CSS-level tools for component fidelity

These exist so a theme can build meters, panel chrome, PTT decoration, a
living background, a banner, and side stat columns — all without any DOM/JS
changes of its own. All of them are inert (zero visual effect) unless a
theme's CSS opts in with a scoped `[data-theme="…"]` rule. The shared base
rule also sets `pointer-events: none` on all of them — decorative mounts
never intercept clicks/taps even after a theme makes them visible and
positions them over interactive elements (e.g. `#pttDeco` sitting on top of
`#ptt`). If your theme's rule needs the mount to be interactive for some
reason, that's a deliberate opt-out, not the default — think twice before
overriding it.

### The `--level` visual bus

**`--level` (custom property, 0..1)** — set every audio frame by the
mic-capture handler (`pushWaveform`), lightly smoothed, and zeroed by
`resetWaveform`. It's a true global bus: both `pushWaveform` and
`resetWaveform` set it on `#waveform` itself (kept for back-compat with
anything already reading it off `#waveform`/`.waveform` via a descendant or
combinator selector) **and** on `document.documentElement`, so any element
anywhere in the page can read a live value via plain CSS inheritance from
the root — no descendant-of-`#waveform` requirement. Drive continuous
things in pure CSS with it — a VU needle rotation (`transform: rotate(calc(-45deg
+ var(--level, 0) * 90deg))`), a meter-fill width, a glow intensity. `hifi`
uses it for the analog needle. JS already zeroes `--level` when idle — key
your "resting" animation speeds off that same fallback (`var(--level, 0)`)
rather than inventing a second idle concept.

Worked examples:

```css
/* Slower pulse at rest, faster as level rises */
animation-duration: calc(0.5s + (1 - var(--level, 0)) * 2s);
/* Brighten with level, never animate the filter itself */
filter: brightness(calc(1 + var(--level, 0) * 0.8));
```

**Perf laws (non-negotiable for anything reading `--level` or otherwise
"living"):**
1. **Never animate blur.** Pre-blur (a static `feGaussianBlur` / `blur()`)
   and animate `opacity` or `transform` instead — animating blur radius is
   expensive to rasterize every frame.
2. **Opacity-of-pre-blurred, not blur-of-sharp.** If something needs to
   "intensify," fade a pre-blurred layer in/out rather than changing how
   blurred it is.
3. **`will-change: transform, opacity`** on anything continuously animating
   those two properties, so the compositor promotes it to its own layer
   instead of repainting on every frame.

**Fixed as of the standardized-primitives pass:** CSS custom properties only
inherit down the DOM tree, from an element to its descendants — so setting
`--level` on `#waveform` alone would only reach `#waveform`'s own
descendants, leaving sibling branches like `#meterChrome`, `#pttDeco`,
`#fxBack`, and `.statCol` stuck on the `var(--level, 0)` fallback. That's
why `--level` is now also set on `document.documentElement` every frame
(see above) — every mount in this document, including all four of those,
sees a genuinely live value today. If you're building a *new* mount
elsewhere in the page, it inherits from the root the same way; you don't
need to do anything special to opt in.

**`#meterChrome`** — an empty `<div>` inside `#pttWrap`, immediately before
`.waveform`, `display:none` by default. Holds three empty, unstyled children:
`.meterFace`, `.meterNeedle`, `.meterTicks`. Intended for analog-VU or
segment-meter chrome that replaces (or frames) the bar waveform — style the
three children as a gauge face, a rotating needle (keyed off `--level`), and
tick marks respectively. If your theme uses this, typically also hide the
bars (`[data-theme="you"] .waveform { display: none; }`) since the two are
alternate visualizations of the same signal — don't run both.

**`.panelFrame`** — one empty, inert `<div>` appended as the last child of
each `.card` (`#transcript`, `#assistantText`), of `#historyRail`, of
`#sessionRail`, and of `#settingsPanel` — five total, `display:none` by
default. Intended for decorative panel chrome: frame edges, corner screws,
tape marks, window/bezel treatment, toggle-switch ornaments. Give the parent
`position: relative` and the frame `position: absolute; inset: 0` (or a
corner placement) in your scoped rule.

**`#pttDeco`** — one empty, inert `<span>` inside `#ptt`, after `.pttPulse`
and before `.pttIcon`, `display:none` by default. Intended for PTT-button
decoration that isn't the icon itself: a keycap indicator slot, a recessed
ring, a sigil, a status lamp. Key its appearance off `#ptt.recording` the
same way the built-in themes do to give recording state a second visual
signal beyond the icon/background color change.

`#pttDeco` also holds **8 static, empty, inert `<i>` children** so a theme
can build a genuine CSS-only 3D polyhedron sigil with no DOM changes of its
own. The pattern: give `#ptt` (the parent) a `perspective` value — that's
the 3D viewing context its descendants render into — then make `#pttDeco`
itself the 3D stage with `transform-style: preserve-3d`, and position each
`<i>` as one face with its own fixed `transform` (a `clip-path` triangle,
`rotateY` in steps around the vertical axis, `rotateX` to tilt it, and
`translateZ` to push it out from center — the standard CSS "bipyramid"
recipe: 4 faces tilted one way for the upper half, the same 4 headings
tilted the opposite way for the lower half). Animate the *whole shape*
spinning by keyframing `#pttDeco`'s own `transform` (e.g. `rotateY`) —
composing correctly with each `<i>`'s static per-face transform because
they all share `#pttDeco`'s `preserve-3d` context. A center "core" (an
`::before`/`::after` on `#pttDeco`, `translateZ(0)`, not one of the 8 faces)
is a natural home for a breathing/pulsing glow independent of the face
rotation. A worked example — one of the 8 upper-pyramid faces and the
breathing core, the rest of the bipyramid following the same pattern at
`rotateY` steps of 90deg:

```css
[data-theme="midnight"] #ptt { perspective: 400px; }
[data-theme="midnight"] #pttDeco { transform-style: preserve-3d; animation: spin 8s linear infinite; }
[data-theme="midnight"] #pttDeco i:nth-child(1) {
  clip-path: polygon(50% 0, 100% 100%, 0 100%);
  transform: rotateY(0deg) rotateX(35deg) translateZ(20px);
  background: var(--accent);
}
[data-theme="midnight"] #pttDeco::before {
  content: ""; position: absolute; inset: 35%; border-radius: 50%;
  background: var(--accent); animation: breathe 2.4s ease-in-out infinite;
}
```

Guard rotation/breathing under `@media (prefers-reduced-motion: reduce)`;
static glow (box-shadow, not itself animated) should stay visible when motion
is off.

**`#fxBack`** — one empty, inert `<div>`, the first child of `body`,
`display:none` by default. When a theme opts in it's typically
`position: fixed; inset: 0` with a negative `z-index` so it sits behind
literally everything (header, cards, rails) as a full-viewport living
background layer — the "canvas" background propagated from `body`'s own
`background` still shows through underneath it, so themes can layer #fxBack
over a simple flat `body { background: var(--bg); }` fallback rather than
maintaining two competing gradient systems.

**`#vaMist`** — not a DOM mount but an inert, static, zero-size
(`width="0" height="0"`) inline `<svg>` `<filter>` block, defined once near
the top of `body`, available to any theme via `filter: url(#vaMist)` on any
element (typically `#fxBack`). It chains `feTurbulence` (fractal noise) →
`feDisplacementMap` (warps whatever it's applied to using that noise) →
`feGaussianBlur` (softens the result) into a "void mist" texture. The
filter itself is static — no SMIL `<animate>` inside it — because animating
the filter's own parameters would mean re-rendering the filter graph every
frame; a "drifting" mist look instead comes from animating `transform`/
`opacity` on the *element* the filter is applied to (see the perf laws
above). One shared `<filter id="vaMist">` serves every theme; there's
nothing theme-specific to register, just reference it.

**`.statCol`** — two empty, inert `<div>`s, `#statColL` and `#statColR`,
inside `#pttWrap`, flanking `#ptt` (one immediately before the button, one
immediately after), `display:none` by default. Intended for side stat-bar/
meter chrome flanking the PTT orb. Because the shared base rule already
keeps them hidden, a theme typically turns them on only inside its own
`@media (min-width: 1024px)` block — mobile stays inert for free, no extra
rule needed. To position them without disturbing `#pttWrap`'s normal flex
column flow, give `#pttWrap` `position: relative` in your own scoped rule
(same convention `.panelFrame`'s parents and `hifi`'s `#meterChrome` use)
and make `.statCol` `position: absolute`, placed off `#ptt`'s own
`--ptt-size` so it stays correctly offset regardless of that token's value.

**Not a mount point, but related:** `.wbar` (the 7 existing waveform bars)
can be restyled directly — border-radius, size, background, even a
`repeating-linear-gradient` to fake discrete LED/segment blocks — without any
new markup. `console` does exactly this instead of using `#meterChrome`.
Prefer restyling `.wbar` over `#meterChrome` when your design still wants a
7-bar reading, just styled differently; reach for `#meterChrome` when the
design wants a genuinely different instrument (a needle, a single fill bar).

## Font primitive — self-hosted, subsetted, opt-in-loaded

The "no external network requests" rule (top of this document) still holds —
a theme can add a distinctive display font, but only a same-origin,
subsetted, self-hosted one, never a remote `src: url(...)`.

1. Pick a properly-licensed font (SIL OFL is the safe default — Google Fonts'
   catalog is all OFL or Apache). Subset it to what the UI actually needs:
   Latin basic + digits + punctuation is `U+0020-007E`. If the font only
   ships as a variable font (no static Bold/Black files), instantiate the
   weights you need first with `fonttools varLib.instancer` before
   subsetting — e.g. `fonttools varLib.instancer -o Bold.ttf Font[wght].ttf
   wght=700`. Do this in a throwaway venv (`python3 -m venv`, `pip install
   fonttools brotli`), not any project's runtime venv — it's a one-time build
   step, not a runtime dependency.
2. Subset each weight to woff2: `pyftsubset in.ttf --flavor=woff2
   --output-file=out.woff2 --unicodes="U+0020-007E"`. Target ≤25KB per file;
   a display font subsetted this tightly to 1-2 weights typically lands
   around 14-15KB.
3. Place the resulting `.woff2` files in `themes/fonts/` (same-origin,
   sibling to the theme CSS files) plus the font's license file (e.g.
   `OFL.txt`) alongside them, unmodified, as required by most open-font
   licenses when redistributing.
4. Reference them with `@font-face` **inside your theme's own `themes/<id>.css`**
   (not in `index.html`) — one block per weight, `font-weight` matching the
   file, `font-style: normal`, and always `font-display: swap` so text
   renders immediately in a fallback and swaps in once the woff2 arrives.
   Loading is automatically scoped to "first time this theme is selected":
   the browser only fetches a `@font-face` source the first time something
   on the page actually renders with that `font-family`, and nothing renders
   with it until your theme's `[data-theme="<id>"]` rules are both loaded
   (theme CSS is fetched on first selection, per the File Convention above)
   and active (the attribute matches) — so other themes never pay for a font
   they don't use.
5. Set `--font-display` (or whatever rule needs the font) to the family name
   with the same fallback-stack discipline as everything else: your font
   first, then a system stack that's a reasonable stand-in if it's slow to
   arrive. If you only subsetted specific weights, pin an explicit
   `font-weight` everywhere you reference the family — don't leave it to
   the browser to guess a nearest-match against a font that has no 400/italic
   face, especially for large hero text where a wrong-weight substitution is
   obvious. `check-theme.py` treats a bare `@font-face { … }` block the same
   way it treats `@media` and `@keyframes` preludes — as a resource
   declaration, not a selector to scope — so it doesn't need `[data-theme="…"]`
   wrapped around it; that exemption is already in the checker.

No built-in theme currently ships a custom font, so here's a worked example
instead of a reference file — Cinzel (OFL), instantiated from Google Fonts'
variable `Cinzel[wght].ttf` at weights 700 and 900, subsetted to
`themes/fonts/Cinzel-Bold.woff2` / `Cinzel-Black.woff2` (~14-15KB each) with
`themes/fonts/OFL.txt` alongside:

```css
[data-theme="midnight"] {
  --font-display: "Cinzel", Didot, "Big Caslon", Georgia, serif;
}
@font-face {
  font-family: "Cinzel";
  src: url("fonts/Cinzel-Bold.woff2") format("woff2");
  font-weight: 700;
  font-style: normal;
  font-display: swap;
}
```

## Verification gates (run all of these before calling a custom theme done)

1. `python3 themes/check-theme.py themes/<id>.css <id>` → exit `0`. This is the
   canonical gate-1 command — it's a stdlib-only parser (not a line-grep) that
   correctly ignores `@keyframes` bodies and `@media` preludes while still
   checking selectors nested inside `@media` blocks, so it won't false-positive
   on animation/responsive rules the way a naive grep for lines ending in `{`
   would. Exit `1` prints every offending selector by name.
2. `curl -s -o /dev/null -w '%{http_code}' http://<host>:8770/themes/<id>.css` → `200`
3. `curl -s -o /dev/null -w '%{http_code}' http://<host>:8770/themes/themes.json` → `200`, and your entry is present in the JSON
4. Load the page, open the settings drawer, confirm your swatch appears with the right two colors and name
5. Click your swatch: `data-theme` on `<html>` flips to your id, `localStorage.getItem("va-theme")` matches, and the page visibly re-skins with no console errors
6. Reload the page: your theme persists (boot reads `localStorage`, re-fetches `themes.json`, re-applies)
7. Resize/check both layouts: mobile single column (<1024px) and desktop 3-zone grid (≥1024px) both look intentional, not just "colors changed"
8. Trigger `#ptt.recording` (hold the button) and confirm the `.recording` / `.pttPulse` state reads clearly against your palette
9. No DOM or JS was touched — diff `index.html` against its pre-theme-work state and confirm zero changes outside what boot/loader wiring already required
10. If you added a font: `ls -la themes/fonts/*.woff2` shows each file ≤25KB, the license file is present alongside, `curl -s -o /dev/null -w '%{http_code}' http://<host>:8770/themes/fonts/<file>.woff2` → `200`, and your `@font-face` blocks use `font-display: swap`
11. If you used `#fxBack`/`#bannerMount`/`.statCol`/the `#pttDeco` faces: confirm they're invisible on every *other* theme (the shared base rule staying `display:none` for them) and that any keyframe name you added doesn't collide with one in another theme file or in `index.html` — a quick cross-file `grep -oh '@keyframes [A-Za-z0-9_-]*'` over `index.html themes/*.css` should show every name exactly once

## Ready-to-build candidates

A Stitch mockup already exists as an inspiration reference in this `_refs/`
directory (local/untracked, not part of this repo's history) and is ready to
be adapted into a real custom theme whenever asked:

- `_refs/custom-ref-hacker.html` — terminal/hacker green-on-black aesthetic

Treat it the same way the three built-ins' refs were treated: mine it for
color, type, and texture ideas — never copy its Tailwind classes or DOM.
