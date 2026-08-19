# Patchbay Theme Spec

> **STATUS (2026-08-15, instrument port):** the client's base look is the
> **Analog Studio Instrument** faceplate. `data-theme="instrument"` is the only
> built-in theme; `BUILTIN_THEMES` in `index.html` carries exactly that one
> entry. The hooks documented below are verified directly against the shipped
> markup as of 2026-08-15. The former built-ins (`editorial`, `console`,
> `hifi`) are parked in git history at `cba3f4c` (`git show
> cba3f4c:webclient/index.html`) - their blocks were written against the
> pre-port DOM and are no longer live anywhere in the page. Mine them for
> technique (component fidelity, `--level`-driven chrome) if useful, but never
> copy their selectors; several of the ids/classes they targeted no longer
> exist. `webclient/revamp-mockups/instrument/DESIGN-LANGUAGE.md` documents
> the design tokens and primitives the current markup implements.

Written for an LLM (or a human) tasked with building the user a **custom theme**
for the Patchbay web client (`webclient/index.html`).

## The one rule that matters

**Whatever you build, you never touch `index.html`.** That is the rule, and it is the
only one that has never changed. What you are allowed to *ship alongside* your theme
depends on which of two tiers you are writing.

### Skins — the default, and what the rest of this document describes

**A skin is CSS-only.** Every visual identity — colors, type, texture, component
shapes, PTT button treatment, VU/waveform styling — is expressed as CSS
custom-property overrides plus ordinary rules scoped under a
`[data-theme="<your-id>"]` attribute selector. If you find yourself wanting to add
a `<div>`, rename a class, or add an inline `style=` attribute in the HTML — stop.
The token set below is deliberately complete enough that you shouldn't need to.

A skin cannot break the app: a scoped rule that doesn't match is inert. That safety
is the whole reason the constraint exists, and it still holds.

### Rigs — a theme that ships code

A **rig** is a theme that additionally ships `themes/<your-id>.mjs` and declares
`"module": true` in its manifest entry. It gets an ES module, a WebGL or canvas
context, physics, and its own DOM *inside the mount points listed below* — but still
not one line of `index.html`.

**A rig can break the app, and a skin cannot.** That is the entire difference, and it
is why a rig carries obligations a skin doesn't: a required `destroy()`, a teardown
contract, and gates that prove it leaks nothing. **Installing a third-party rig runs
its JavaScript in your cockpit.** No sandboxing is attempted. The host-side reaper
defends against *incompetence* — a rig that forgets to clean up — not against malice.

Do not write a rig from this document alone. The authoring guide, the host API, the
lifecycle contract and the gates you must pass live in **`themes/AGENTS.md`**; this
file remains the deep reference for tokens, mounts and primitives, which both tiers
share. Until that guide exists, the working contract is the code: `setTheme()` and
`mountRigForTheme()` in `index.html`, and the teardown/reaper gates in
`webclient/test_webclient.py`.

**Pick a skin unless you actually need code.** Most themes shouldn't be rigs.

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
   it. The built-in `instrument` theme never fetches anything - only custom
   ones do, and only on first selection. (A private/local theme uses the same
   mechanism against a second, gitignored manifest - see "Local overlay" below.)

## Registering in themes.json

`themes/themes.json` is a JSON array. Append an object:

```json
{ "id": "hacker", "name": "Hacker", "swatch": ["#0a0a0a", "#39ff14"] }
```

- `id` — matches your CSS file's `[data-theme="..."]` value and its filename.
- `name` — short label shown under the swatch in the settings drawer.
- `swatch` — exactly two hex colors used to paint the picker's split-circle swatch
  (background color, then accent color, is the usual choice).

## Local overlay (private skins that never ship)

`webclient/themes/local/` is an entirely gitignored directory - nothing placed
inside it is ever tracked, so it survives `git status` clean and is never part
of the public export (`scripts/export-public.sh` exports via `git ls-files`,
and untracked files are invisible to that command). Use it for a skin that's
private, or built on encumbered assets you can't redistribute (a licensed
font, someone else's artwork), without keeping it off `main` by hand.

- `webclient/themes/local/themes.json` - same JSON array schema as the tracked
  `themes/themes.json` above (`id`, `name`, `swatch`, optional `avatar`).
- A theme registered in the **local** manifest loads its CSS from
  `themes/local/<id>.css` instead of `themes/<id>.css`. A theme in the tracked
  manifest still loads from `themes/<id>.css` - the manifest a theme is listed
  in decides which directory its CSS is fetched from.
- Its assets (images, fonts) live under `webclient/themes/local/assets/<id>/…`
  and are referenced from the local CSS with URLs relative to the CSS file,
  e.g. `url("assets/<id>/foo.png")`.
- Merge order at boot: tracked manifest entries load first, then local ones.
  **On an `id` collision the local entry wins**, replacing the tracked entry
  in place so the picker's ordering doesn't jump around.
- On a fresh clone `webclient/themes/local/` doesn't exist at all - that's the
  normal case. The manifest fetch 404s, is swallowed silently, and the picker
  shows only the tracked themes. No console error.
- Everything else about writing a theme - scoped under
  `[data-theme="<id>"]`, the `check-theme.py` gate, no external network
  requests - applies to a local theme identically.

The same pattern exists for avatars: `webclient/avatar/local/` is likewise
gitignored and may hold a `registry.mjs` whose entries get appended to the
avatar registry at boot, so a local-only theme can pair with a local-only
avatar module - see "Avatar theming" below for what an avatar block itself
can carry.

## The complete token list - two layers

The current markup carries two layers of custom properties on `:root`, and a
skin can override either or both.

The **outer layer** is the same 23-token contract this spec has always
documented (table below) - abstract UI roles (`--bg`, `--accent`, `--radius`,
…) that every theme, built-in or custom, is expected to redefine.

The **inner layer**, new with the instrument port, is 16 Analog Studio
Instrument primitives - `--room`, `--metal-hi`, `--metal-mid`, `--metal-low`,
`--metal-shadow`, `--ink`, `--ink-dim`, `--cream`, `--amber`, `--amber-deep`,
`--lamp-off`, `--r`, `--r-glass`, `--ease`, `--mono`, `--grotesk` (documented
in full in `webclient/revamp-mockups/instrument/DESIGN-LANGUAGE.md`). Most of
the 23-token contract is now defined *in terms of* these primitives on
`:root` (e.g. `--bg: var(--room)`), so overriding a handful of primitives
re-skins the whole faceplate - metal color, ink, the single accent hue, lamp
glow, corner radius - in one move, rather than chasing all 23 outer tokens
individually. The primitives are the higher-leverage lever; the 23-token
contract remains the floor every theme must still redefine (a theme that only
touches primitives but leaves an outer token hardcoded to the instrument's
values will still look intentional, since the outer tokens already resolve
through the primitives - but redefining both layers gives full control,
including over hooks that read an outer token directly rather than through a
primitive).

`instrument` (`BUILTIN_THEMES` in `index.html`) has no `[data-theme="instrument"]`
CSS block at all - it's just the `:root` defaults for both layers, registered
in JS only for its swatch/name/avatar entry. Your custom theme's CSS file
should redefine all 23 outer-layer tokens under your own
`[data-theme="<id>"]` selector - anything you don't override falls through to
whatever theme was active before (the instrument `:root` defaults, absent a
prior selection), which is rarely what you want, so redefine all of them. You
may additionally override any of the 16 primitives if your theme wants to
lean on the faceplate's own component styling rather than replace it.

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
| `--tracking-label` | Letter-spacing for uppercase section labels ("You said", "Assistant", "Tape", "Links", etc.) |
| `--shadow-card` | box-shadow value applied to `.card`, `#historyRail`, `#sessionRail` — use `none` for a flat look, or a real shadow recipe for depth |
| `--ptt-shadow-idle` | box-shadow for the PTT button at rest |
| `--ptt-shadow-rec` | box-shadow for the PTT button while recording (usually a glow keyed off `--rec`) |
| `--ptt-size` | PTT button diameter/side-length in px. Live since 2026-08-16 - see the note below. |
| `--ptt-radius` | PTT button border-radius - `50%` for a circular orb/knob, a px value (e.g. `20px`) for a squarish "key" shape. Live since 2026-08-16 - see the note below. |

> **FIXED 2026-08-16. Both tokens are live.** They were dead from the instrument port
> until now: `index.html` declares `#ptt` twice, and the second rule — unscoped, so it
> applied to *every* theme — hardcoded `width: 78px; height: 78px; border-radius: var(--r)`.
> Same selector, same specificity, later in source order, so the literals won. It stayed
> invisible because every shipped skin declared `--ptt-size: 78px`, agreeing with the
> hardcode by coincidence; a theme declaring anything else silently got 78px.
>
> The later rule now reads `width: var(--ptt-size); height: var(--ptt-size);
> border-radius: var(--ptt-radius)`. Since the base declares exactly the previous
> literals, no existing theme changed by a pixel — `test_ptt_size_token_actually_sizes_the_button`
> pins both halves of that: a theme declaring `--ptt-size: 120px` measures 120, and the
> base still measures exactly 78.
>
> **You may now compute layout from `--ptt-size`** — it describes the button's real
> rendered box again. Historical note worth keeping, because it is why this mattered: a
> `.statCol` placement once broke by positioning columns with a `calc()` against
> `--ptt-size` while the token was dead, and the columns ended up hanging off the edge of
> the chassis.

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

### Contrast law: light on dark, ink on metal, never light-on-light

Light/accent-colored text (`--cream`, `--amber`) belongs on powered dark
surfaces; ink-dark text (`--ink`) belongs printed on the bare chassis. Never
put light text directly on the metal — measure the element's real painted
background (a screenshot sample, not an assumption) before picking a color,
the same way `test_webclient.py`'s contrast gate does.

`.lcd-label` is the trap: the same class is used for nomenclature bare on
the faceplate (`.selector-label`, `.stat .lcd-label`) *and* nomenclature
inside a real `.lcd-window`/`.lcd-head` — cream is only correct in the
second case. `.stat-strip` (`index.html:1517`) paints its own hardcoded dark
gradient, so `.stat .lcd-label` counts as a powered surface despite sitting
outside an explicit `.lcd-window` element; `.selector-label` doesn't, so it
needs the ink treatment instead.

```css
/* correct: cream inside a real lit window */
.lcd-window .lcd-text { color: var(--cream); }
/* correct: ink printed on bare metal */
.baseplate .nom { color: var(--ink); }
```

## Structural hooks — what you may style, never rename

These ids/classes are load-bearing: the page's JavaScript selects them directly.
Restyle them freely (color, font, size, shape, shadow, spacing) but never rename,
remove, or restructure them, and never change what they mean.

**There is no `<main>` element any more.** The pre-port DOM's `#historyRail |
main | #sessionRail` 3-column grid is gone. If you're carrying selectors from
an older theme forward, drop any rule targeting `main` - it now matches
nothing.

**Chassis primitives (instrument port, present on both the main `.device` and
`#firstrunOverlay`'s `.device`):** `.device` (the faceplate itself),
`.chassis-screw` (+ corner modifiers `.tl` / `.tr` / `.bl` / `.br`),
`header.nameplate`, `.seam` (divider strips between major zones),
`footer.baseplate`, `.nom` (engraved nomenclature, printed directly on metal -
used on dozens of labels; takes a `.tiny` size modifier), `.lcd-label`
(backlit nomenclature, for anything printed on a powered dark surface instead
of engraved metal - also takes `.tiny`), `.lcd-window` / `.lcd-head` /
`.lcd-text` (+ `.dim`), `.jack` (single-line backlit input field) +
`.lcd-lamp`, `.disc-lamp` (disclosure-group indicator lamp), `.chev`
(disclosure chevron). These are the DOM's implementation of the primitives
`webclient/revamp-mockups/instrument/DESIGN-LANGUAGE.md` defines - read that
doc for the intended look of each, this spec only says what JS depends on.

**Layout containers:** `header`, `#contentGrid` - now three `<section
class="zone">` siblings instead of the old 3-column grid:

- `.zone.zone-input` - arm switch + PTT + waveform (below).
- `.zone.zone-meter` - the four state lamps and the VU-style gauge (below).
- `.zone.zone-output` - **this element IS `#sessionRail`** (`<section
  class="zone zone-output" id="sessionRail">`); the transcript/assistant
  cards and session readouts live inside it (below).

`#lowerBay` - a container below `#contentGrid`, after a `.seam` - holds the
avatar pane, the history rail, the quest card, the links card, the
diagnostics toggles, and the log (all below). `#historyRail` lives here now,
not inside `#contentGrid`, and is **no longer a desktop-only left rail**.

**Desktop viewport-fit contract (S10) — SUPERSEDED, see "Chassis regions"
below for the current mechanism.** The paragraph that used to live here
described a fixed 1000px height threshold and a flex-column `.device`; both
are gone (the threshold was replaced by a runtime measured gate, and V4 made
`body > .device` a CSS grid at 1024px+, capped or not — see below). Left
here only so old links don't 404 into nothing; do not implement against this
paragraph. **The PTT key (`#ptt`) must remain clickable at every viewport,
capped or not** — that half of the old contract is still exactly true, and
is still verified the same way: a hit-test (`elementFromPoint()` at its own
centre), not a geometry check, since an ancestor's `overflow-y: auto` clip
or a sibling painted on top of it (the avatar module's own canvas covered it
in one build during S10's own development) both defeat a plain
`getBoundingClientRect()` comparison while still failing the one thing that
actually matters — can the user press TALK.

## Chassis regions — a public grid-area contract

**As of V4, `body > .device` is a CSS grid at 1024px+ (below that it's
plain block flow, unchanged), and its regions are named, not numbered.**
This is the seam a rig uses to relayout the chassis itself — not just the
mount points below, the whole faceplate — by redefining
`grid-template-areas` alone, scoped under its own `[data-theme="…"]`. No
`grid-column`/`grid-row` line-number placement exists anywhere in this
grid for exactly that reason: a numbered placement can't be overridden by
redefining an area map, so if one had survived here this contract would be
decorative. There is a test for this, not just a claim — see
`test_webclient.py::test_rig_can_reclaim_chassis_regions_by_area_name`,
which fails RED if placement is ever hardcoded again.

**As of the tape/links rebalance, the base map is 3 columns, not 2** —
`console` and `legend` each span the first two columns (columns 1+2
carry `console`'s own 989px floor between them, see `index.html`'s
`body > .device` comment); `monitor` and `controls` are column 3 alone.
`tape` (~60% of the row) is column 1 alone; `links` (~40%) spans columns
2+3.

| area | required | holds | current default position |
|---|---|---|---|
| `nameplate` | yes | `header.nameplate` + every `.seam` | full width, top |
| `console` | **yes — must stay reachable** | `#contentGrid` (arm/PTT, the gauge, transcript/persona/voice) | columns 1+2, below nameplate |
| `monitor` | no | `#avatarPane` (the avatar) | column 3, `console`'s row only |
| `tape` | no | `#historyRail` | column 1, below `console`, beside `links` — ~60% of the row |
| `legend` | no | `#legendPlate` (silkscreened command legend) | columns 1+2, below `tape` |
| `controls` | no | `.lowerBayControls` (diagnostics toggles) | column 3, below `monitor`, beside `legend` |
| `task` | no | `#questCard` (hidden unless a quest is running) | full width, below `legend`/`controls` |
| `links` | no | `#linksCard` (hidden unless there are links) | columns 2+3, `tape`'s row, beside it — ~40% of the row |
| `log` | no | `#log` (hidden unless diagnostics is open) | full width, below `task` |
| `baseplate` | yes | `footer.baseplate` | full width, bottom |

**`deck` is not a chassis-level area.** `#vaDeck` (a control deck fed by a
state provider -- Home Assistant is the current/only implementation, see
"Deck" below) briefly held a named `deck` chassis area, but the base
layout now nests it *inside* `console`'s meter zone instead — a sibling of
`.meter-housing` inside `.zone-meter`, not a `body > .device` grid child.
Why: `#contentGrid`'s three zones (input/meter/output) stretch-align to
the tallest, and at real viewports (measured 1440x1271) that leaves the
fixed-size gauge sitting on top of ~412px of permanent dead space in
`.zone-meter` — a chassis-level `deck` region left that space unused and
added a second, separate box below it. `#vaDeck` now fills that void
directly instead. A rig that wants the deck back at the chassis level (its
own row, spanning the full width like `task`) can still do so: add
a `deck` cell to its own `grid-template-areas` override and give `#vaDeck`
`grid-area: deck` in its own stylesheet — the mechanism this section
describes doesn't care that the base layout stopped using that name.

**`console` is the one hard requirement.** It holds `#ptt` — S10's whole
point is that the interaction band stays reachable, so a rig's own
`grid-template-areas` must still give `console` a real cell (any size,
any position) or it inherits an unreachable PTT key, same failure S10
exists to catch. Everything else is optional: a rig may drop `monitor` or
any of the others from its map entirely (an area with no matching
template cell just doesn't render its content, same as any
`display: none` — nothing errors).

**Reused names, checked for collision, not assumed:** `monitor` / `tape` /
`controls` / `task` / `links` / `log` are not new — they're `#lowerBay`'s
own pre-existing internal `grid-template-areas` names (the `#lowerBay {
grid-template-areas: … }` block, `index.html`, active below 1024px and
inert above it since `#lowerBay` itself is `display: contents` there).
Reusing them at chassis level was deliberate: `#avatarPane` /
`#historyRail` / `#questCard` / `#linksCard` / `#log` /
`.lowerBayControls` already carry `grid-area: monitor` etc. from that
block, so those pre-existing declarations apply directly to `body >
.device`'s grid once `#lowerBay` promotes its children into it — no
duplicate placement rule to keep in sync. The two templates never
actually collide in effect: `#lowerBay`'s own template only ever governed
`#lowerBay`'s own (real) box, which no longer exists at this width.
`nameplate`, `console`, `deck`, and `baseplate` are new to this slice —
`#lowerBay` never had those. `legend` is new too (command legend plate) —
`#lowerBay`'s own template still says `"controls controls"` for that row
(it's the inert one, see above); only `body > .device`'s map reclaims the
row's left cell as `legend`.

**A rig's `grid-template-areas` must be a complete, valid map** (same
column/row count throughout, every cell filled — CSS itself enforces
this; an invalid map is ignored wholesale and the base layout above
applies instead, not a partial one). It does not need to keep the base
map's 3-column shape — a rig may use any column/row count it wants, as
long as `console` (and, if used, whichever other names it keeps) appear
somewhere in it.

**The centrepiece (`#meterFace`, inside `console`) has a floor too, just a
much lower one than `console` itself: 440px wide, and legible, if a theme
keeps a centrepiece at all.** A rig legitimately replaces it outright —
that's the whole point of the rig tier, the owner's bar is "a different
interface, not a different palette" — so this is prose, not a test
assertion; `test_webclient.py`'s own regression guard
(`test_v4_chassis_widens_avatar_column_meter_undiminished`) is
deliberately scoped to `instrument` only (575x245, what it actually
measures there) and asserts `#meterFace` exists before checking its size,
so a rig that removes it entirely doesn't fail a gate that was never
written for it.

**For THIS owner, the S10-capped state is not one case among several —
it is the state he actually occupies.** Measured, not assumed: at his
real viewport (1271px tall) the runtime fit gate (`data-s10="cap"`,
`s10UpdateFit()`) engages at every width from the 1280px floor up through
1920px, every time. A rule written as "give a region a real resting size
in the base/uncapped state, then reset it away under the cap" reads like
a base case with a capped exception, but for this owner it collapses to
just the capped branch — the base-state rule is dead code that LOOKS like
a feature, because the state it targets never actually renders for him.
If you're adding a base/capped pair to this chassis, put whatever you
actually want him to see in the CAPPED branch, not the base one, and
verify against 1271px tall before assuming otherwise (V4's tape/deck
floors, `minmax(190px, 1fr)` / `minmax(150px, 1fr)` in the `html[data-
s10="cap"] body > .device` rule, `index.html`, exist precisely because
the base-state version of that same idea was checked and found to never
apply for him).

**Cards / hero text:** `.card`, `#transcript` / `#transcriptBody` (user's
words), `#assistantText` / `#assistantBody` (assistant's reply - gets
`.speaking` class while TTS audio is actively playing back).

**PTT + waveform (inside `.zone-input`):** `.control#pttWrap`, `#ptt` (gets
`.recording` class while the mic is live - this is the ONLY reliable signal
for "is the user talking right now"), `.pttIcon`, `.pttPulse` (the expanding
ring drawn only while `.recording`), `.ptt-talk-label.nom`, `#pttCaption`,
`#pttDeco` (see "Theme primitives" below), `.statCol#statColL` /
`.statCol#statColR` flanking `#ptt`, `.waveform` / `#waveform`, `.wbar` (7
bars, JS drives their `transform: scaleY(...)` inline per audio frame - don't
fight that with a CSS transition longer than ~100ms or the bars will lag
visibly behind the audio). Also in this zone: `.control#armSwitch.toggle-btn`
(`aria-pressed`, the wake/open-mic arm toggle) with `.toggle-track[data-armed]`
> `.toggle-lamp` + `.toggle-knob`, and `#meterChrome` (see "Theme primitives").

**State lamps + gauge (`.zone-meter`):** `.zone-head` > `.zone-label.nom` +
`.state-lamps` holding four `.lamp[data-lamp="idle|listening|thinking|speaking"]`,
each a `.bulb` + `.nom`. `.meter-housing` > `.meter-face#meterFace` (>
`.meter-glass`, an inline SVG gauge with `.gaugeArc` / `.gaugeRedZone` /
`.gaugeTick` (+ `.major`) / `.gaugeLabel`, then `#needleWrap` > `#needle` +
`#needlePivot`) and `.meter-readout.nom` (> `#gaugePeak`, `#stateTag.state-tag`,
`#gaugeRms`). The lamps and `#stateTag`'s displayed word are driven purely by
CSS `:has()` keyed off the state carriers below - see that section for the
selector pattern before restyling them.

**Settings drawer:** `#gearBtn`, `#settingsOverlay` / `.open`, `#settingsPanel` /
`.open` (its head is now `.panelHead` > `h2#settingsTitle` + `#settingsCloseBtn`),
`#brainStatus`, `#brainList` (JS-populated `.brainOption` rows - some now use
the horizontal switch primitive: `.switch-h-row` > `.switch-h-visual` >
`input.switch-h-input` + `.switch-track-h` > `.switch-knob-h` +
`.switch-lamp-h`), `#personaInput`, `#voiceLabel` / `#voiceSelect`,
`.panelBtn` / `.panelBtn.primary`, `.voiceCloneLane` (button rows in the
Voice Lab group). Settings are now organized as five `<details>` disclosure
groups, each `<summary>` a `.disc-lamp` + `.nom` + `.chev`: `#personaGroup`
("Persona Workshop"), `#appearanceGroup` ("Faceplate" - holds
`#themeSwatches` and `#avatarSelect`), `#inputGroup` ("Input Stage" - holds
`#wakeToggle` / `#wakeModelSelect` / `#phoneContextToggle` / `#camSelect`),
`#wsUrlAdvanced` ("Connection"), `#voiceCloneAdvanced` ("Voice Lab"). Within
those: `#themeSwatches` (JS-populated; each entry is a `.themeSwatch` button
containing `.swatchDot` + `.swatchName`, gets `.active` on the current theme).

**History rail:** `#historyRail` (now inside `#lowerBay`, all viewports -
see "Layout containers" above), `#historyList` (JS-populated `.historyEntry`
rows, each with `.historyTime` / `.historyUser` / `.historyAssistant`).

**Session output (`.zone-output` == `#sessionRail`):** `.card#transcript`,
`.card#assistantText` (as above), a persona selector (`.selector` >
`.selector-label.lcd-label.tiny` + `.rotary#personaDisplay` - a rotary
position-switch, `.rotary-row` > two `.rotary-tick.nom` + `.rotary-knob` >
`.rotary-pointer`, then `.rotary-current`), a voice selector (`.selector` >
`.selector-label` + `.rail#voiceDisplay` - a rail position-switch,
`.rail-track` > `.rail-stop.s1/.s2/.s3` + `.rail-pointer` (JS sets an inline
`left`), then `.rail-labels` > three `<span>`s), `.stat-strip` of five
`.stat` (each a `.lcd-label.tiny` + `<b>`): `#sessionBrain`, `#sessionVoice`,
`#sessionTools`, `#sessionMood`, `#sessionConn`, and
`.lcd-window#sessionPersonaWindow` (> `.lcd-head.lcd-label.tiny`,
`.lcd-text.dim#sessionPersonaSnippet`). **Removed, do not target:**
`.sessionRow` / `.sessionKey` / `.sessionVal` / `.sessionPersona` no longer
exist - their roles are now `.stat-strip`/`.stat` and
`#sessionPersonaWindow`/`.lcd-text`. `#sessionPersonaSnippet` itself survives
unchanged, just relocated inside `#sessionPersonaWindow`.

**Diagnostics:** `.lowerBayControls` (three `.diagToggle` buttons: `#diagToggle`,
`#camBtn`, `#screenBtn`), `#log` (uses `--font-mono`; keep it legible - it's
the escape hatch when something's broken).

**Deck (B2/patchbay, relocated):** `#vaDeck` holds a control surface fed by
a state provider — a dense rack of jack points for whatever set of
addressable, stateful things that provider exposes. Home Assistant is the
current/only implementation (`ha_deck.py`, `HA_URL`/`HA_TOKEN`), not what
the deck fundamentally is; absent unless the server has that configured —
most downstream clones never render anything here. It nests
inside `.zone-meter`, below `.meter-housing` (see "Chassis regions" above
for why it isn't a chassis-level area), and fills whatever height is left in
that zone (`flex: 1 1 auto`, no hardcoded height) — the void left by
`#contentGrid` stretch-aligning input/meter/output to the tallest of the
three. `.zone-meter` itself needs `min-height: 0` (index.html, 1024px+
block) to actually honor that budget: it's a grid item under
`#contentGrid`'s `align-items: stretch`, and grid items default to
`min-height: auto`, the same automatic-minimum-size trap this file already
documents for `#contentGrid` against `.device`'s own row — without the
override, a full deck's natural content height floors `.zone-meter` past
`#contentGrid`'s bottom edge and #historyRail (tape) paints under it.
`#deckTiles` is a grid (`repeat(auto-fill, minmax(76px, 1fr))`, never
`clamp()` in that track slot — see the panel-grid comment `index.html`
already carries that trap), not a single-column stack, so the deck's usual
narrow-but-tall box (~530x400 at 1440px) shows ~6 columns × ~5 rows of a
real 29-tile deck at once, not a couple. It is the base renderer's own mount
point, hidden and replaced wholesale by a rig exactly like `#meterChrome`;
each `.deckTile` inside it is created once per entity and mutated in place,
never rebuilt, so its DOM node identity survives every SSE update.

Visually each `.deckTile` is a **jack point**, not a web-list row — the
faceplate's own machined-socket look (`.chassis-screw`'s radial-gradient
bezel) at a larger radius, with `.deckTileDot` as the socket and its
`::after` pseudo-element as the bore/lamp at the centre (same idea as
`.lamp .bulb`), sitting above `.deckTileName` as an engraved legend strip
(`.lamp .nom`'s mono/uppercase/tracked treatment). No new DOM nodes — a
rig replacing `#deckTiles` wholesale is unaffected either way.

Published attribute contract a rig can key off of:
- `data-state` on a `.deckTile`: one of `on` / `off` / `unavailable` /
  `unknown`. `state` itself (from the state provider — Home Assistant
  today) is an OPEN set. `"on"`, `"open"` (cover), and `"unlocked"` (lock)
  classify to `on`; `"off"`, `"closed"`, and `"locked"` classify to `off` —
  HA's own domain-specific vocabulary, each with an unambiguous
  active/inactive reading; a future provider brings its own vocabulary
  through the same classifier contract. Everything else, including climate
  modes (`heat`/`cool`/`idle`/`auto`/…), classifies to `unknown`, **never**
  `off`; an unhandled reading must never present as a real, physical OFF,
  and a thermostat is deliberately never forced onto the on/off axis at
  all. The raw provider state is never discarded: when a tile classifies to
  `unknown`, its `aria-label`/`title` includes the raw value alongside the
  classification (e.g. `Home Thermostat: unknown (cool), …`), and
  `.deckTileRaw` renders it visibly on the tile itself.
- `data-write` on a `.deckTile`: the closed enum `unknown` / `pending` /
  `confirmed` / `failed`, mirroring the write status of a control intent
  (`/ha/intent` today). Independent of `data-state` — a `pending` tile
  still shows its last known state.
- `data-link` on `#vaDeck`: `"down"` while the `/ha/stream` SSE connection
  is lost, absent otherwise. Losing the link never flips a tile's
  `data-state` — "link down" means *cannot tell*, not "off".
- `?decksim=<value>` (dev/review only) forces every tile's `data-state` or
  `data-write` and shows a fixed `SIMULATED: <value>` badge in the deck's
  label row (never a tile row — the badge stays unmissable but costs no
  tile-grid space); absent by default, no forcing and no badge.
- `?deckfilter=<comma-separated substrings>` (opt-in, part 3) EXCLUDES any
  entity whose id or friendly name contains one of the given substrings
  (case-insensitive), applied both to the initial render and to every SSE
  update — a filtered entity never mounts a tile. Absent by default: every
  allowlisted-domain entity renders, no silent cap. When active, `#deckFilterStatus`
  (next to the "Deck" label text) shows `<shown> of <total>` so a filter can
  never drop entities without saying so.

**Deck keyboard model (accessibility follow-up):** `#deckTiles` is
`role="toolbar"` (a container of controls with roving `tabindex`, not
`role="grid"` — a real ARIA grid requires owned `role="row"` wrapper
elements, which would have to sit inside the same CSS Grid track as the
tiles themselves and risks the row-snap layout this file already documents
being pulled apart; toolbar needs no such DOM restructuring and every jack
keeps the `role="button"` it already had). Only one clickable jack
(`data-clickable="true"`) is ever a `tabindex="0"` stop at a time — Tab
enters/exits the whole deck in one stop each way. Inside it: Left/Right move
between clickable jacks in DOM order; Up/Down jump a real measured row (the
grid's actual `auto-fill` column count, not a constant) and land on the
nearest clickable jack in that row; Home/End jump to the first/last
clickable jack. Read-only jacks (`climate.*`, `lock.*`) are never a stop
either way — consistent with them carrying no `role`/`tabindex` at all.
Enter/Space keep firing the existing per-tile toggle, untouched. A rig
replacing `#deckTiles` wholesale inherits none of this for free and should
reimplement the same roving-tabindex contract if it keeps native clickable
tiles.

**Deck live announcements:** `#deckLiveStatus`, a visually-hidden
`role="status" aria-live="polite"` span next to the deck's label row,
announces two kinds of transition in words — never a raw attribute dump,
and never on every SSE frame: a tile's `data-write` transitioning *into*
`"failed"` (`"<friendly name>: write failed"`), and `#vaDeck[data-link]`
transitioning into/out of `"down"` (`"Deck link lost"` / `"Deck link
restored"`). Multiple transitions arriving within the same ~400ms window
coalesce into one announcement rather than firing once per element.

**Service Panel controls (V4 follow-up):** `?deckfilter=`/`?decksim=` shipped
as query params with nothing in the UI; a `DECK` section in `#settingsPanel`
(`#deckGroup`, present only once `/ha/states` confirms a real deck — same
absence rule as `#vaDeck` itself) now layers four controls on top, each a
presentation toggle only, never a DOM rebuild:
- `#vaDeck[hidden]` — the section's on/off switch (`va-deck` in
  localStorage, unset = on). `display: none` regardless of the 1024px+
  media query.
- `.deckTile[hidden]` — per-point visibility (`va-deck-hidden`, a JSON
  array of hidden entity ids, unset = none) and/or "hide unavailable"
  (`va-deck-hide-unavailable`, unset = off) ORed together. Neither ever
  removes or reorders a tile — same persistent-stage/node-identity
  guarantee as everywhere else in this section. "Hide unavailable" is
  evaluated only at load and at the instant its switch is flipped, **never**
  on a live SSE update, for the same reason tile order is frozen at load: a
  point disappearing out from under a live inspection is the hazard the
  freeze exists to prevent.
- The forced-state picker mirrors `?decksim=`'s values but is layered
  *under* it — the URL param always wins when present — and is
  deliberately **not** persisted (no localStorage key at all): a saved
  forced state is exactly the kind of lie the `SIMULATED:` badge exists to
  prevent, so it always resets on reload.

**State classes and attributes you can key off of:** `html[data-va-state]` -
one of `connecting` / `connected` / `error`, set at boot/connect/disconnect -
plus the existing, still-live per-element classes: `#dot.connected` /
`.connecting` / `.error`, `#statusText.shimmer` (mid-request), `#ptt.recording`,
`#assistantText.speaking`. See "State carriers" immediately below for how
these compose.

### State carriers

The instrument port added a root-level state attribute alongside the
existing classes. All of these are live and composable:

- `html[data-va-state]` - `connecting` / `connected` / `error`.
- `#dot.connected` / `.connecting` / `.error`, `#statusText.shimmer`,
  `#ptt.recording`, `#assistantText.speaking`, `#gateApprove.holding`,
  `#settingsPanel.open` / `#settingsOverlay.open`, `html.avatar-immersive`,
  `#questCard[data-cockpit-status]`, `html[data-mood]`, `html[data-theme]`.

The base stylesheet already derives the four `.state-lamps` bulbs and
`#stateTag`'s displayed word from these carriers with `:has()` selectors -
e.g. the "listening" lamp lights via `body:has(#ptt.recording) .lamp[data-lamp="listening"]
.bulb { … }`, and `#stateTag`'s text comes from `body:has(#ptt.recording)
.state-tag::after { content: "CAPTURING"; }`. A theme restyling the lamps or
gauge readout should key off the same carriers rather than adding a JS hook
of its own, e.g.:

```css
[data-theme="midnight"] body:has(#ptt.recording) .lamp[data-lamp="listening"] .bulb {
  background: var(--your-accent);
}
```

`--level` (0..1) is still set every audio frame on both `#waveform` and
`document.documentElement` - that section of this spec (below, under "Theme
primitives") is unchanged, still correct.

**Cockpit - quest card (inside `#lowerBay`, all viewports):** `#questCard`
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

**Cockpit - approval gate banner (fixed, ALL viewports - including mobile):**
`#gateBanner` (JS
toggles `hidden`; visible whenever the server's permission list is
non-empty), `.gateDesc` / `.gateMore`, `.gateControls`, `#gateDeny` /
`.gateDenyBtn` (single-click deny, gets a native `disabled` attribute while a
response is in flight), `#gateApprove` / `.gateHold` (the hold-to-approve
control — gets `aria-disabled="true"` and a `.holding` class while a hold is
in progress), `#gateApproveFill` / `.gateHoldFill` (the progress fill; width
driven every frame by the JS-set `--hold-progress` custom property, `0` to
`1` — restyle color/shape freely, but the fill mechanism itself is JS-owned).

**Avatar pane (inside `#lowerBay`, all viewports, lazy-loaded; see
`avatar/avatar.mjs`):** `#avatarPane` (root container - visible at every
width now: `#lowerBay #avatarPane`'s own display rule outranks the older
`display:none` base rule that used to gate it to desktop; `.collapsed` class
shrinks it to a thin strip when the user toggles it closed), `#avatarMount`
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

**Local-only avatar modules** pair with a local-only theme (see "Local
overlay" above): `webclient/avatar/local/` is gitignored, and a
`registry.mjs` placed there has its entries appended to the avatar registry
at boot, the same idea as `themes/local/themes.json` for CSS themes. Use it
for a GLB you can't redistribute in the public repo.

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
transcript-panel texture, dividers/labels), not just its colors. The
instrument faceplate itself is the current worked example of component
fidelity built on the primitives below - its chassis, machined keys, VU
gauge, LCD windows, rotary/rail selectors, and lamps (`index.html` body, and
`webclient/revamp-mockups/instrument/DESIGN-LANGUAGE.md` for the design
intent behind each) show the pattern: restyle the mount points, don't fight
the token system. The former `hifi`/`console` built-ins did the same thing
against the pre-port DOM and are still worth reading for technique
(`git show cba3f4c:webclient/index.html`), but their selectors target hooks
that no longer exist - don't copy them directly.

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

### Visual buses

Three custom properties carry live mic level into CSS, all derived from the
same `pushWaveform()`/`resetWaveform()` mic-capture handler — no theme, rig
or skin should ever open a second `getUserMedia`/audio tap to get a live
level. Pick the bus by what you're building, not by habit:

| bus | inherits | cadence | scope | for |
|---|---|---|---|---|
| `--level` | yes | every audio frame (~125-375Hz, deduped) | `document.documentElement` (+ `#waveform` for back-compat) | legacy — any element in the page, CSS-only skins |
| `--heat` | yes | ~12Hz (smoothed envelope) | `document.documentElement` | the room — ambient/chassis reactions anywhere in the tree |
| `--level-fast` | no | 60Hz | `#meterChrome` only | rigs — canvas-rendered centrepiece content |

An author picking `--level` or `--heat` gets a value readable from anywhere
via plain inheritance; an author picking `--level-fast` gets the freshest
signal but only on `#meterChrome` itself — canvas content inside that mount
reads it via JS (`getComputedStyle`), not CSS inheritance.

#### The `--level` visual bus

**`--level` (custom property, 0..1)** — set every audio frame by the
mic-capture handler (`pushWaveform`), lightly smoothed, and zeroed by
`resetWaveform`. It's a true global bus: both `pushWaveform` and
`resetWaveform` set it on `#waveform` itself (kept for back-compat with
anything already reading it off `#waveform`/`.waveform` via a descendant or
combinator selector) **and** on `document.documentElement`, so any element
anywhere in the page can read a live value via plain CSS inheritance from
the root — no descendant-of-`#waveform` requirement. Drive continuous
things in pure CSS with it — a VU needle rotation (`transform: rotate(calc(-45deg
+ var(--level, 0) * 90deg))`), a meter-fill width, a glow intensity. The
instrument faceplate's own `#needle` (inside `.zone-meter`) is the current
worked example. JS already zeroes `--level` when idle - key your "resting"
animation speeds off that same fallback (`var(--level, 0)`) rather than
inventing a second idle concept.

**Measured cost, GRANDFATHERED (S4b, 240-frame burst, CDP
`Performance.getMetrics` deltas):** `--level` on `document.documentElement`
costs **~1.8ms/frame style-recalc on `instrument`, ~2.7ms/frame on a themed
skin, with a forced full-document layout every single frame**
(240/240 layout events). That cost is almost entirely the act of writing
*any* registered `inherits: true` custom property on `documentElement` at
all — a registered property nothing reads measured the same
(~1.8ms/frame) — not `--level`'s own consumers, which are a real but much
smaller second-order effect (+0.05-0.34ms/frame). Scoping the write off
`:root` was tried and rejected: element-scoping (`#waveform` only) gets the
cost down to ~0.09-0.31ms/frame with zero forced layout, but three shipped
themes read `--level` from `:root` descendants and one reads it from
outside `.device` entirely, so nothing but `#waveform` itself is safe to
narrow to; `.device`-scoping only recovers ~25% of the cost because
`.device` is nearly the whole page. `--level` stays exactly as it is —
inherited, on `:root`, every frame — with the cost now measured and
documented so you can choose knowingly; **`--heat`** and **`--level-fast`**
below exist specifically so new work doesn't have to pay it.

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

#### The `--heat` visual bus

**`--heat` (custom property, 0..1)** — a second, SLOWER smoothing pass on
top of `--level`'s own smoothing, set at only **~12Hz** (not every audio
frame) on `document.documentElement`, and zeroed by `resetWaveform`. Same
inheritance as `--level` — readable from any element in the page via plain
CSS — but deliberately cheap to afford: because the measured cost above is
the act of an inherited root write, not what reads it, `--heat` pays that
cost 12 times/second instead of 60-375 times/second. Use it for the
*room* — `#fxBack`, chassis/ambient reactions, anything that wants a live
signal from anywhere in the tree via inheritance but doesn't need per-frame
precision. It reads as a genuine envelope (slow attack/decay), not `--level`
sampled less often.

#### The `--level-fast` visual bus

**`--level-fast` (custom property, 0..1)** — the FAST bus, written at
**60Hz directly on `#meterChrome`** (nowhere else) and zeroed there by
`resetWaveform`. Registered `inherits: false`: it is readable on
`#meterChrome` itself but **not** on its children or any other element —
that confinement is what buys the ~20x over `--level`'s root write
(measured ~0.09-0.31ms/frame element-scoped vs ~1.8-2.7ms/frame for an
inherited root write, same harness). This is the bus for **rigs** —
`#meterChrome` is the confirmed rig centrepiece mount (a rig whose
centrepiece is a full-panel canvas claims `#meterChrome`; see "Structural
hooks" / the theme-rigs plan). A rig reads it via
`getComputedStyle(mount).getPropertyValue('--level-fast')` in its own JS
render loop and drives canvas content directly — not via CSS inheritance,
which is exactly what `inherits: false` forecloses.

**The transform/opacity-only rule binds RIGS ONLY.** A rig reacting to
`--level-fast` (or anything else) must do so through `transform`/`opacity`
on its own canvas/DOM elements — the compositor path, no layout, no paint —
because rigs are new code with no legacy readers to break. This does
**not** apply retroactively to CSS-only skins: an existing skin using
`filter: blur()`, `box-shadow`, or `animation-duration` keyed off `--level`
is grandfathered and may keep doing so;
so is the base faceplate's own `--level`-in-`box-shadow` glow
(`index.html`'s `--level` VU-needle glow). If you're building a *new* rig,
follow the rule; if you're editing an existing skin, you don't need to
retrofit it.

**`#meterChrome`** - an empty `<div>`, a sibling of `#pttWrap` and
`.waveform` inside `.zone-input` (not nested inside `#pttWrap`), `display:none`
by default - the instrument faceplate uses its own dedicated `.zone-meter`
gauge (SVG arc + `#needle`, documented under "Structural hooks" above) for
its live-level display, so `#meterChrome` stays an unused, available mount
just like before the port. Holds three empty, unstyled children: `.meterFace`,
`.meterNeedle`, `.meterTicks`. Intended for analog-VU or segment-meter chrome
that replaces (or frames) the bar waveform - style the three children as a
gauge face, a rotating needle (keyed off `--level`), and tick marks
respectively. If your theme uses this, typically also hide the bars
(`[data-theme="you"] .waveform { display: none; }`) since the two are
alternate visualizations of the same signal - don't run both. Note the
instrument base CSS already sets `.waveform`/`#waveform` to `display:none`
by default too (again in favor of its own gauge), so a theme reviving the
7-bar look needs to explicitly show it.

**`.panelFrame`** — one empty, inert `<div>` appended as the last child of
each `.card` (`#transcript`, `#assistantText`), of `#historyRail`, of
`#sessionRail` (the `.zone-output` section itself), of `#avatarPane`, of
`#linksCard`, and of `#settingsPanel` - seven total, `display:none` by
default. Intended for decorative panel chrome: frame edges, corner screws,
tape marks, window/bezel treatment, toggle-switch ornaments. Give the parent
`position: relative` and the frame `position: absolute; inset: 0` (or a
corner placement) in your scoped rule.

**`#pttDeco`** — one empty, inert `<span>` inside `#ptt`, after `.pttPulse`
and before `.pttIcon`, `display:none` by default (the instrument base CSS
also keeps `.pttPulse` and `.pttIcon` themselves hidden by default - it
signals recording state via the button's own transform/shadow/border-color
and the `.ptt-talk-label` text instead; see `#ptt`/`.ptt-talk-label` under
"Structural hooks"). Intended for PTT-button decoration that isn't the icon
itself: a keycap indicator slot, a recessed ring, a sigil, a status lamp. Key
its appearance off `#ptt.recording` to give recording state a second visual
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

**`#fxBack`** - one empty, inert `<div>`, the first `<div>` child of `body`
(the actual first child is the inert zero-size `<svg>` holding `#vaMist`,
just below), `display:none` by default. When a theme opts in it's typically
`position: fixed; inset: 0` with a negative `z-index` so it sits behind
literally everything (header, cards, rails) as a full-viewport living
background layer — the "canvas" background propagated from `body`'s own
`background` still shows through underneath it, so themes can layer #fxBack
over a simple flat `body { background: var(--bg); }` fallback rather than
maintaining two competing gradient systems.

**`#bannerMount`** - one empty, inert `<div>`, a top-level sibling of
`#fxBack`/`#gateBanner`/`#firstrunOverlay`/`#settingsOverlay`/`.device`,
positioned in DOM order right after `#gateBanner` and before
`#firstrunOverlay`, `display:none` by default (covered by the same shared
mount rule as `#meterChrome`/`.panelFrame`/`#pttDeco`/`#fxBack`/`.statCol` -
nothing special about its default state). Intended for a full-width
notification/banner treatment; give it a real height and content via
`::before`/`::after` or background in your scoped rule. In immersive avatar
mode (`html.avatar-immersive`) the base stylesheet already lifts it to
`position: relative; z-index: 10` so it floats above the avatar canvas if a
theme makes it visible there too.

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
(same convention `.panelFrame`'s parents use) and make `.statCol`
`position: absolute`, placed off `#ptt`'s own `--ptt-size` so it stays
correctly offset regardless of that token's value.

**Not a mount point, but related:** `.wbar` (the 7 existing waveform bars,
inside `#waveform` - hidden by default in the instrument look, see
`#meterChrome` above) can be restyled directly - border-radius, size,
background, even a `repeating-linear-gradient` to fake discrete LED/segment
blocks - without any new markup. Prefer restyling `.wbar` over `#meterChrome`
when your design still wants a 7-bar reading, just styled differently; reach
for `#meterChrome` when the design wants a genuinely different instrument (a
needle, a single fill bar) - or reach for the instrument faceplate's own
`.zone-meter` gauge hooks (documented under "Structural hooks" above) when
you want the current shipped gauge's exact look, just recolored.

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
9. `index.html` was not touched - diff it against its pre-theme-work state and confirm zero changes outside what boot/loader wiring already required. This still binds on both tiers: a **skin** ships no code at all, and a **rig** ships its code in its own `themes/<id>.mjs`, never in the page. What changed on 2026-08-16 is only *where* a theme's JavaScript may live, not whether it may edit the host. This rule is about your theme's own work: it doesn't cover the local-overlay loader or the rig loader themselves, which are deliberate, one-time changes to `index.html`'s boot logic - that's infrastructure, not something an individual theme is allowed to redo. **If you shipped a rig,** you additionally owe the teardown and reaper gates in `webclient/test_webclient.py`: switch away from your theme and back, and confirm zero leaked WebGL contexts, no still-advancing rAF loop, and no net-new listeners.
10. If you added a font: `ls -la themes/fonts/*.woff2` shows each file ≤25KB, the license file is present alongside, `curl -s -o /dev/null -w '%{http_code}' http://<host>:8770/themes/fonts/<file>.woff2` → `200`, and your `@font-face` blocks use `font-display: swap`
11. If you used `#fxBack`/`#bannerMount`/`.statCol`/the `#pttDeco` faces: confirm they're invisible on every *other* theme (the shared base rule staying `display:none` for them) and that any keyframe name you added doesn't collide with one in another theme file or in `index.html` — a quick cross-file `grep -oh '@keyframes [A-Za-z0-9_-]*'` over `index.html themes/*.css` should show every name exactly once

## Built custom themes (reference)

`hacker` (terminal/green-on-black) is a shipped custom theme -
`webclient/themes/hacker.css`, registered in `webclient/themes/themes.json`.
Mostly a straight 23-token override plus a scanline overlay, so most of it is
a valid worked example - but a couple of its rules (`.sessionRow`,
`.sessionKey`, and an `#ptt.recording ~ #waveform` sibling-combinator rule)
were written against the pre-port DOM and are now dead selectors: harmless
no-ops, not something to copy. Check `webclient/themes/themes.json` (and,
if present, `webclient/themes/local/themes.json`) for the live list rather
than trusting a name list in this doc - themes get added, removed, or moved
to the local overlay over time.
