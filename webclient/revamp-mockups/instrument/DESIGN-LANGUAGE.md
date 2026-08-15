# Analog Studio Instrument - Design Language

Reference direction for the eventual real-client port. Owner-approved on `main/index.html`
(2026-08-13). This document is the durable artifact; the mockups are throwaway HTML.

## Tokens

```css
--room: #0d0b09;        /* warm-dark background floor, never pure black */
--metal-hi: #a89c88;    /* brushed metal highlight */
--metal-mid: #7c7263;   /* brushed metal midtone */
--metal-low: #4d473c;   /* brushed metal shadow */
--metal-shadow: #2c2820;

--ink: #241f1a;          /* engraved nomenclature color, on metal */
--ink-dim: rgba(36,31,26,0.58);
--cream: #ece4d2;        /* ivory meter face / LCD text */

--amber: #ff9e3d;        /* THE single accent - lamps, needle, active state */
--amber-deep: #d94a1f;   /* red-zone / fault variant of the accent */
--lamp-off: #4a453b;     /* unlit lamp/bulb */

--r: 4px;                /* machined radius - chassis, buttons, panels */
--r-glass: 6px;           /* the one exception - meter glass curve */

--ease: cubic-bezier(.2, 1.4, .4, 1);   /* shared spring-ish easing for all state changes */
--mono: ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Consolas, "Liberation Mono", monospace;
--grotesk: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
```

Single accent rule: amber (`--amber`) and its red-zone/fault sibling (`--amber-deep`) are the
ONLY color family used for state, emphasis, or interaction. No blue/purple, no gradient text.

Machined radii rule: `--r: 4px` everywhere structural (chassis, panels, buttons, LCD windows).
The meter glass curve (`--r-glass: 6px`) is the one softening exception. Round controls (knobs,
lamps, the mobile power key) may be circular (`border-radius: 50%`) - that's a physical shape,
not a "rounded card."

## Primitives

- **Faceplate / chassis** (`.device`): brushed-metal gradient stack (repeating linear-gradient
  for the brushed texture + a diagonal metal-hi/mid/low/shadow gradient) with inset highlight/
  shadow box-shadows and a drop shadow into the room. `--r` radius, never more.
- **Corner screws** (`.chassis-screw`): four 8-9px circles, radial-gradient metal, each with a
  1px slot rotated to a slightly different angle (not uniform - reads as hand-set hardware).
- **Engraved nomenclature** (`.nom`): mono, uppercase, `0.14em` letter-spacing, ink color with a
  dark-below/light-above text-shadow pair simulating light catching an engraved groove in metal.
  Used for anything printed directly on the metal (labels, brand, footer).
- **Backlit LCD label** (`.lcd-label`): same mono/uppercase treatment but dim cream-on-black,
  for anything printed on a powered dark surface (LCD windows, jack fields) instead of engraved
  into metal - no text-shadow groove, because it's lit from behind, not carved.
- **VU meter** (`.meter-housing` / `.meter-face` / SVG + needle): letterboxed ivory face,
  `aspect-ratio: 2.35/1`, recessed into a metal housing bay (housing `justify-content: center`
  so the short face floats centered in its bay rather than stretching). Arc + ticks are an SVG
  half-circle (`viewBox 0 0 280 120`, pivot `140,104`, `r:94`) reaching near the face's left/right
  and top edges so the printed scale fills the window instead of leaving empty ivory. Needle is a
  separate absolutely-positioned div (`#needleWrap`/`#needle`/`#needlePivot`), rotated
  `-45deg + level*90deg` from `bottom:12%`, `transform-origin:50% 100%`, eased with `--ease`. A
  `.meter-glass` overlay (diagonal white-to-transparent, `mix-blend-mode:screen`) sits on top for
  the glass-curve reflection. This is the primitive most worth precision-porting: reuse the exact
  viewBox/pivot/radius numbers, don't re-derive them.
- **Machined key** (`.ptt-key`, `.power-key`): a raised metal button with a hard drop-shadow
  "step" (`0 5px 0 rgba(20,17,12,.65)`) instead of a soft blur, so it reads as a physical keycap.
  Press state is a real `translateY(3-4px) scale(0.97)` plus the drop-shadow collapsing to an
  inset shadow - the button visually sinks into the chassis, it doesn't just dim.
- **Throw switch** (`.toggle-btn`/`.toggle-track`/`.toggle-knob`, vertical; `.switch-h` family,
  horizontal): a recessed track with a sliding metal knob and a small lamp that lights amber at
  the "on"/armed position. The knob position is driven by a `data-armed`/`data-on` attribute, not
  a class, so it reads like reading a physical switch state.
- **Rotary knob position-switch** (`.rotary`/`.rotary-knob`/`.rotary-pointer`): a circular metal
  knob with a printed pointer line and two tick labels flanking it (e.g. TERSE/WARM), plus a
  backlit current-value readout below in amber. Used for 2-pole or gradient-style choices
  (persona warmth).
- **Rail position-switch** (`.rail`/`.rail-track`/`.rail-stop`/`.rail-pointer`): a horizontal
  detented rail with 3 stops, an amber pointer/flag above the active stop, and printed labels
  below (active one amber+bold). Used for discrete named choices (voice, brain).
- **LCD backlit window** (`.lcd-window`/`.lcd-label`/`.lcd-text`): dark recessed panel
  (near-black diagonal gradient, deep inset shadow) holding live text (assistant transcript,
  stat readouts, jack inputs). The `.jack` input variant is the same window shrunk to a single
  line with a lit lamp dot, used for anything the user types (WS URL, voice search).
- **Lamps** (`.lamp`, `.pwr-lamp`, `.lcd-lamp`, `.disc-lamp`, `.status-bulb`): small circles,
  `--lamp-off` when idle, amber (or green/`--amber-deep` for firstrun's connected/fault pair)
  with a soft glow `box-shadow` when lit. State changes are instant color/shadow swaps, not
  animated fades (matches real panel-mount LEDs).

## Interaction rules

- Press = real `scale(0.97)` (floor `0.95`) + inset shadow, never a bare opacity change.
- `transition:` always names explicit properties (`transform`, `box-shadow`, `background`,
  `top`/`left` for sliding knobs) - never `transition: all`.
- `:focus-visible` always gets a 2px amber outline with 2px offset - a control never loses a
  focus ring without a replacement.
- `font-variant-numeric: tabular-nums` on every readout that changes live (PEAK/RMS, uptime,
  latency, tok/s).
- `@media (prefers-reduced-motion: reduce)` freezes the needle transition and any looping
  jitter/lamp-cycle JS.
- ASCII hyphens only in copy (no em/en dashes); OS-native font stacks only (`--mono` for
  nomenclature/readouts, `--grotesk` for prose like the LCD transcript).
- Dark only - no light-mode variant exists yet.

## Hard bans (self-check before shipping any new screen)

- No centered glowing orb/blob.
- No symmetric two-rail dashboard skeleton.
- No soft-blur rounded-2xl cards (machined 0-4px radii; meter glass curve and physical round
  controls are the only exceptions).
- One accent family only - the warm amber/red-zone. No purple/blue AI glow, no gradient text.
- No pure black - warm-dark floor `#0a0806` / `--room`.
- No `transition: all`.

## Screens in this set

- `main/index.html` - desktop hero, full signal-flow faceplate (input / level / output).
- `mobile/index.html` - 390px portable field unit, same primitives reflowed vertically.
- `settings/index.html` - config as front-face controls + a disclosure "SERVICE PANEL" for
  advanced/rare settings (WS URL, wake word, phone context, voice cloning, theme).
- `firstrun/index.html` - one-time power-on calibration: connect, pick persona/voice, enter.
- `INDEX.html` - unit directory linking all of the above.
