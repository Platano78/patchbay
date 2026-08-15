# Persona / brain-selector patch pack

Task #12: in-UI persona + brain-selector panel for the voice agent.

Task #8 (2026-07-03): reinstalled from `github.com/huggingface/speech-to-speech`
main into a fresh venv, pinned at commit `1e63f7e9343e491809d0d60e64f7ea551dbe845a`
(2026-07-01), and ported this pack onto it. The live service now runs the
`chat-completions` LLM backend (`speech_to_speech/LLM/chat_completions_language_model.py::ChatCompletionsApiModelHandler`,
subclass of the new `LLM/base_openai_compatible_language_model.py::BaseOpenAICompatibleHandler`
shared by both OpenAI-style handlers) instead of `responses-api`. The old
0.2.10 install at `$HOME/speech-to-speech/.venv` is untouched and
kept as an instant rollback (see below).

## Install

- Repo: `$INSTALL_DIR` (default `$HOME/speech-to-speech-main`) — editable install,
  `.venv` there, python3.10, CPU-only torch/torchaudio 2.11.0 — no CUDA on this
  box outside the external :8084 model.
- `faster-qwen3-tts[ggml]` is a base (non-Darwin) dependency and its
  `qwentts-cpp-python` PyPI wheel is CUDA-only; the CPU wheel had to be
  fetched by hand from
  `https://huggingface.co/datasets/andito/qwentts-cpp-python-wheels/resolve/main/whl/cpu/qwentts_cpp_python-0.3.0+cpu-py3-none-manylinux_2_34_x86_64.manylinux_2_35_x86_64.whl`
  and installed before `pip install -e ".[kokoro]"` would resolve. We don't
  use qwen3 TTS (we run `pocket`), but the base dependency still has to
  import cleanly.
- Extras installed: `.[kokoro]` (base install command) + `.[pocket,websocket]`
  (pocket TTS + the websocket-mode extra) — `nano-parakeet` (STT) was
  already a base dependency.

## Files

- `brain_control.py` — `speech_to_speech/brain_control.py`. Defines
  `BrainControl`, which handles `config_get`/`config_set` control messages:
  swapping the LLM backend (`client`/`model_name`/`_extra_body` — now on
  `BaseOpenAICompatibleHandler`, shared by both OpenAI-style handlers) live,
  updating the persona (`runtime_config.session.instructions`), and resetting
  chat history. Loads brain definitions from
  `$HOME/speech-to-speech/brains.json`. Ported for #8: `_extra_body`
  is now recomputed via `BaseOpenAICompatibleHandler._build_extra_body(base_url,
  True, None)` (mirrors the base class's own `setup()` rule instead of
  reimplementing it) and a brain's API key can come from a literal `api_key`
  or be read lazily from `api_key_file` (env-style `VAR=value` lines) +
  `api_key_var` — used for the Hermes shim's bearer token
  (`~/.hermes/shim.env`, `HERMES_SHIM_TOKEN`). The token is never
  logged; `_resolve_model`'s `GET {base_url}/models` sends it as
  `Authorization: Bearer <key>` when present.

  **Persona persistence (tiered).** Personas set from the settings panel are
  written to `$HOME/speech-to-speech/persona.json` (override with
  `VOICE_PERSONA_FILE`) — outside the package, next to `brains.json` and the
  `voices/` sidecar, so a `pip install --upgrade` can't erase it.

  The effective persona is resolved per brain, most specific tier first:

  1. **per-brain override** — what the user typed for *this* brain
  2. **per-brain preset** — a shipped, brain-tuned persona (`BRAIN_PRESETS`),
     applied only to a brain the user explicitly put in preset mode
  3. **global persona** — what the user typed once, for every brain
  4. **shipped default** — the CLI `--init_chat_prompt` value

  Presets are offered, never imposed: a brain the user never configured
  resolves to their global persona, because auto-preferring a preset would
  silently override words the user wrote. Preset mode stores the *choice*, not
  a copy of the text, so an improved preset reaches the users who selected it.
  Resolution re-runs on every brain switch, and a changed effective persona
  resets chat history (the earlier turns were produced under a different system
  prompt). `BRAIN_PRESETS` has entries for `coder`, `local`, `frontier` and
  `hermes` -- each states what that lane is *for* (its role in the cockpit),
  never a claim about what a model behind it can or can't do, since the model
  running any given lane varies per deployment. An unrecognized or unkeyed
  brain (e.g. a renamed or custom lane) has no preset and simply inherits
  global → default.

  On-disk shape (`version: 2`):

  ```json
  {"version": 2, "global": "…",
   "brains": {"hermes": {"mode": "preset"},
              "coder": {"mode": "custom", "text": "…"}}}
  ```

  Clearing is defined at every level: clearing a per-brain override removes
  that brain's entry and falls back to global → default; clearing the global
  falls back to the shipped default; when no tier holds anything the file is
  deleted. Writes are atomic (temp file + `os.replace`). The load is fail-safe:
  a missing, empty, unreadable or corrupt file logs a warning and yields an
  empty store rather than raising — this runs at pipeline startup, so it must
  never take the assistant down — and a single malformed brain entry is dropped
  without costing the user the other tiers. The older `version: 1` global-only
  shape (`{"version": 1, "persona": "…"}`) still loads, as the global persona.
  Submitted personas are bounded at `PERSONA_MAX_CHARS` (8000) and rejected if
  they contain control characters other than newline/tab/CR.

  Control protocol: `config_set` takes `persona` plus an optional
  `persona_scope` (`"global"`, the default, or `"brain"`) and, for brain scope,
  `persona_mode` (`"custom"`, `"preset"`, `"inherit"`). `config_state` and
  `config_ack` carry a `persona_tiers` block telling the panel which tier is in
  force and what the other tiers hold.

  Note `voice_rules.py` mitigates the qwen-class answer-length collapse
  globally, for every brain, though the problem is really specific to the
  `local` router's loaded model. Per-brain personas make that a candidate for
  narrowing later — **not** done here: `voice_rules.py` is load-bearing and a
  persona is user-editable, so a pipeline invariant must not be moved into one.

  **Per-brain model selector.** A brain's endpoint can serve many models (an
  NVIDIA NIM endpoint serves hundreds; a llama.cpp router a handful), but
  `brains.json`'s `model` field is fixed at edit time. Overrides set from the
  panel are written to `$HOME/speech-to-speech/model_overrides.json` (override
  with `VOICE_MODEL_OVERRIDES_FILE`; default location is next to the persona
  file) — same sidecar shape/atomicity/fail-safe-load contract as the persona
  store: `{"version": 1, "brains": {"frontier": "deepseek-ai/deepseek-v3.2"}}`,
  atomic writes, and a missing/corrupt file or a malformed per-brain entry
  never takes the pipeline down. An override value must be non-empty, at most
  `MODEL_OVERRIDE_MAX_CHARS` (200) characters, free of control characters, and
  not the literal `"auto"` — `"auto"` is how a brain says "resolve whatever
  the endpoint reports as loaded," so an override that read `"auto"` would be
  indistinguishable from no override at all; clearing the override is the way
  back to it.

  `_effective_model(name, entry)` resolves the override-if-set, else the
  entry's configured `model` (default `"auto"`) — this is what `_set_brain`
  and the background reachability sweep both probe, so a probe now reflects
  the override, not just brains.json. A brain entry MAY also carry a curated
  `"models": ["id", ...]` array; `config_state`'s per-brain block reports it
  (curated wins) or, absent that, whatever model ids the last successful
  `/models` probe saw (parsed defensively out of the SAME response
  `_resolve_model` already fetches — no second request — capped at 500 ids),
  plus the current `model_override` ("" when none).

  Control protocol: `config_set` takes `brain_model: {"brain": "<name>",
  "model": "<id>"}`; an empty `model` clears the override, an unknown `brain`
  acks an error. Setting the override for the ACTIVE brain re-runs the switch
  (`_set_brain`) so the new effective model is probed for real BEFORE the
  override is persisted — a failed probe rolls the candidate back and leaves
  the on-disk store untouched, same probe-before-persist discipline as a
  normal brain switch — and a successful change resets chat history (the
  turns so far were produced by a different model). Setting the override for
  a brain that ISN'T active persists it unprobed; it probes honestly the next
  time that brain is switched to. Either way, a successful change broadcasts
  a fresh `config_state` to every connected screen (the requester itself
  resyncs via its own follow-up `config_get`, same as every other config_set
  branch) — same reasoning as the voice-clone/voice-delete broadcasts below.

- `websocket_streamer.py` — `speech_to_speech/connections/websocket_streamer.py`.
  Adds a `control_callback` constructor kwarg and a text-frame branch in
  `_handle_client`'s message loop: JSON text frames with
  `type in ("config_get", "config_set")` are dispatched to the callback via
  `asyncio.to_thread` (keeps network I/O off the audio loop) and the reply is
  sent back to the requesting client only. Binary (audio) frame handling is
  unchanged. Unmodified on main — diffed byte-for-byte before reapplying,
  zero drift.

- `s2s_pipeline.py` — `speech_to_speech/s2s_pipeline.py`. In `build_pipeline`,
  the `RuntimeConfig` built for the transcription notifier is hoisted into a
  local variable (`runtime_config`) so it can be reused, and
  `websocket_streamer_ref` is captured when `mode == "websocket"` (main
  doesn't track this itself). After the pipeline handlers are built, for
  websocket mode with either OpenAI-style LLM backend
  (`llm_backend in ("responses-api", "chat-completions")` — main already
  guards `_lm_vars` selection the same way), the handler instance is located
  by `isinstance(h, BaseOpenAICompatibleHandler)` (was
  `isinstance(h, ResponsesApiModelHandler)`) and a `BrainControl` is wired
  onto `websocket_streamer.control_callback`. #15: `LMOutputProcessor`'s
  `setup_kwargs` now also passes `text_prompt_queue` (the queue feeding the
  LLM handler) so it can push a follow-up generation request after a tool
  call resolves.

- `voice_tools.py` (#15, new) — `speech_to_speech/voice_tools.py`. Defines
  `TOOL_DEFS` (weather / web search / QMD knowledge-base lookup, as
  Realtime-style function tool dicts) and `execute(name, kwargs) -> str`,
  which dispatches to the matching implementation under a hard per-tool
  timeout and always returns a short, TTS-safe plain-text string (never
  raises, never returns raw JSON). `BrainControl.__init__` arms these onto
  `runtime_config.session.tools`. Args are extracted by name (`kwargs.get`),
  not positionally, with a clean spoken refusal on a missing/empty required
  arg; failures are logged with the exception repr (`logger.warning`) and the
  spoken string differentiates timeout ("... timed out.") from other failures
  ("... failed."). `knowledge_lookup` sends QMD both a `lex` and a `vec`
  sub-query in one `searches` call — `lex` alone misses approximate/misheard
  queries (STT can turn "pi harness" into "pie harness"); QMD itself
  merges/ranks/dedupes both sub-queries into one result list server-side, so
  no client-side merge step is needed.

- `lm_output_processor.py` (#15) — `speech_to_speech/LLM/lm_output_processor.py`.
  `process()` gained a `_run_tool_calls` step: when an `LLMResponseChunk`
  carries `tools`, each call is executed via `voice_tools.execute`, its
  output is recorded with `chat.append_tool_output`, and (if at least one
  call resolved) a follow-up `GenerateResponseRequest` is pushed onto
  `text_prompt_queue` so the model can speak the result. Only the native
  OpenAI-style `tool_calls` channel is live for this deployment's
  `chat-completions` LLM backend (`BaseOpenAICompatibleHandler` /
  `ChatCompletionsApiModelHandler`) — the tag-based `<code>func()</code>`
  prompting in `LLM/language_model.py` belongs exclusively to the
  `transformers`/`mlx-lm` backend class hierarchy and is never invoked here,
  so there is no double-channel risk to guard against. Because a tool-call
  turn's `content` is empty on the native channel, `process()` also yields a
  short filler `TTSInput` (only when `response_wants_audio`) immediately
  before `_run_tool_calls` blocks the thread on the executor — otherwise the
  user hears silence for the tool's timeout budget. The filler is picked
  from a rotating pool (`_pick_filler`, default seven phrases such as "Let
  me check." and "One sec.") that avoids repeating the previous phrase
  back-to-back; the pool is overridable via the `VOICE_TOOL_FILLERS` env var
  (pipe-`|`-separated phrases, since a phrase may itself contain a comma),
  and the special value `off` disables the filler entirely.

- `wakeword_gate.py` (new) — `speech_to_speech/wakeword_gate.py`. Defines
  `WakewordGate`, an optional join-deaf gate (borrowed from
  `ShayneP/local-voice-ai`, see
  `knowledge-base/research-notes/shaynep-local-voice-ai-2026-07-12.md`).
  Env-driven, off by default: `VOICE_WAKE_WORD=1` (truthy: `1`/`true`/`yes`/`on`)
  arms it; `VOICE_WAKE_WORD_MODEL` (default `hey_jarvis`, any openWakeWord
  pretrained model name or a path to a custom `.onnx`) selects the model;
  `VOICE_WAKE_WORD_THRESHOLD` (default `0.5`) sets the score gate; scores at
  or above `0.25` that don't cross it log a "near miss" at INFO (visible at
  the live log level) so a real attempt that fell short still leaves journal
  evidence — anything above `0.05` still also logs at DEBUG for finer
  calibration. While
  armed and not yet awake, `websocket_streamer.py`'s binary branch routes
  mic audio through `WakewordGate.feed()` instead of `input_queue` — the
  pipeline stays deaf until the wake phrase scores above threshold, then
  wakes once for the rest of that session (`WakewordGate.reset()` re-arms it
  on the next session, called next to the `SESSION_END` put on last-client
  disconnect). Fail-open by design: any exception loading the model or
  scoring a frame is logged once and treated as an immediate, permanent
  wake — a broken detector must never brick the assistant. The module
  itself never imports `openwakeword` at module scope (lazy import inside
  `_load_model`), so it stays importable/testable with the dependency
  absent.

  Deploy prerequisite (only if `VOICE_WAKE_WORD=1` is actually used):
  `pip install openwakeword onnxruntime` into `$INSTALL_DIR/.venv`, then
  one-time model download:
  `$INSTALL_DIR/.venv/bin/python3 -c "import openwakeword.utils; openwakeword.utils.download_models()"`
  (openwakeword >=0.5 ships no models in the wheel; older 0.4.x bundles the
  pretrained onnx models already and this step is a no-op). Client-side,
  the webclient shows a `zzz — say "<phrase>" to wake` status while asleep
  and a status-bar flip + short two-tone chime on `wakeword_state: awake`.

  Runtime control (settings panel, added after the initial deploy):
  `VOICE_WAKE_WORD`/`VOICE_WAKE_WORD_MODEL` are boot defaults only — the
  settings panel's "Wake word" block (checkbox + phrase dropdown, hidden
  entirely when `config_state.wake_word` is `null`, i.e. no websocket
  streamer wired) lets a session flip the gate on/off and pick a different
  pretrained phrase without a restart. The checkbox sends
  `config_set {wake_word: bool}`; `true` also calls `WakewordGate.rearm()`
  so a previously fail-open detector gets one fresh load attempt. The
  dropdown sends `config_set {wake_word_model: name}`, validated by
  `WakewordGate.set_model()` against `available_models()` (or an existing
  `.onnx`/`.tflite` path) before swapping and re-arming. Either change
  broadcasts a fresh `wakeword_state` (now also emitted with `state: "off"`
  when disabled) to every connected client, so a toggle from one device is
  reflected on all of them. `WakewordGate.available_models()` lists
  openWakeWord's `resources/models` directory (same layout on 0.4.x and
  >=0.5), strips the `_vX.Y`/extension suffix, and excludes the non-wake-phrase
  files (`melspectrogram`, `embedding_model`, `silero_vad`, `timer`,
  `weather`) — this is the custom-model seam: drop a trained `.onnx` into
  that directory and it auto-appears in the dropdown next time the panel
  refreshes, no code change needed.

- `think_filter.py` (new) — `speech_to_speech/think_filter.py`. Defines
  `ThinkTagFilter`, a dependency-free (stdlib only) stateful streaming
  suppressor for literal `<think>...</think>` reasoning spans. Some local
  reasoning models (e.g. `reasoning-qwen36-27b-mtp` behind llama.cpp) emit
  their reasoning as `<think>...</think>` in the regular chat-completions
  `content` field even with `chat_template_kwargs.enable_thinking=false` —
  probed live: `content` was `"<think>\n\n</think>\n\nHi there friend"`.
  Without filtering, this reasoning text is spoken by TTS. `feed(text)`
  streams a chunk through and returns the safe-to-emit portion (a tag can
  straddle a chunk boundary, so a partial-tag prefix is buffered across
  calls); `flush()` is called once at stream end and returns any buffered
  prefix that turned out to be innocent text (an unclosed `<think>` is
  suppressed instead, since it was reasoning that never finished). Also
  swallows the leading newline run right after a closed think span so TTS
  doesn't see a leading blank chunk. No `speech_to_speech` import, so it (and
  `test_think_filter.py`) is importable/testable standalone.

- `chat_completions_language_model.py` (new) —
  `speech_to_speech/LLM/chat_completions_language_model.py`. Wires
  `ThinkTagFilter` into `ChatCompletionsApiModelHandler`: `_iter_stream_events`
  runs one filter instance per streamed response, feeding each delta's raw
  text through it and only accumulating the *filtered* piece into `raw_text`
  (the flush remnant is folded in the same way) before yielding a
  `TextDelta` — so `raw_text`, which becomes the `AssistantMessage` written
  back to conversation history, never contains a think span either; the
  model would otherwise re-read its own suppressed reasoning as context on
  every later turn, and an all-thinking response would silently store
  thinking while speaking nothing. The existing `if raw_text.strip():`
  guard then naturally skips emitting an `AssistantMessage` at all for a
  response that was 100% reasoning. `_iter_response_events` (non-streaming)
  runs the whole response's text through a one-shot `feed()` + `flush()`
  before yielding it, so both the `AssistantMessage` and `TextDelta` there
  are filtered identically. `_serialize` also wraps its return through
  `voice_rules.apply_system_rules` then `phone_context.apply_ambient` (both
  below) — the only other change to this file.

- `voice_rules.py` (new) — `speech_to_speech/voice_rules.py`. Pipeline
  invariant, not a persona setting: probed regression is qwen3.6 in no-think
  mode collapsing answer length as chat history grows (27 completion tokens
  for a 400-word ask vs 415 with a system-prompt line telling it to answer
  completely). The fix was first tried as a runtime persona edit, which is
  the wrong layer — a persona author has no way to know they need an
  anti-truncation instruction, and every persona (and every brain swap)
  would otherwise have to carry it individually. `apply_system_rules()`
  instead appends (or inserts) a short system-level instruction onto the
  serialised message list at request-assembly time in
  `ChatCompletionsApiModelHandler._serialize`, invisible to the persona
  editor and applied to every brain. `VOICE_SYSTEM_RULES` overrides the
  built-in `DEFAULT_RULES` text; the special value `off` (case-insensitive)
  disables it entirely; unset/blank falls back to `DEFAULT_RULES`. Never
  mutates the `Chat`-owned message list/dicts (copies before modifying);
  idempotent (skips re-appending if the rules text is already present in
  the system content) and dependency-free (stdlib only, no
  `speech_to_speech` import), like `think_filter.py`.

- `phone_context.py` (new) — `speech_to_speech/phone_context.py`. Ambient
  phone context: the webclient (shipped separately) sends opt-in
  `{"type":"phone_context", "lat":.., "lon":.., "accuracy":.., "tz":..,
  "battery_pct":.., "charging":..}` text frames over the existing
  WebSocket. Motivation: the model spun 14 tool rounds trying to guess the
  user's location for an air-quality question — this makes location
  ambient instead. `update(data)` validates each field independently
  (type/range checks; an invalid field is dropped, not fatal to the rest of
  the payload) and merges it onto a module-level last-known-state dict
  under a `threading.Lock` (the WebSocket thread writes, the LLM thread
  reads). `location(max_age_s=1800)` returns `(lat, lon)` or `None` if
  stale/unset; `snapshot()` returns a copy for future tools/UI;
  `ambient_line()` builds "The user's approximate location is {place};
  their local time is {HH:MM} ({tz})." — `{place}` is reverse-geocoded via
  Nominatim (`requests`, imported lazily inside the geocode helper only, so
  the module itself stays dependency-free like `voice_rules.py`), cached
  in-module by coords rounded to 3 decimals, and fails soft to "latitude
  X, longitude Y" on any network error; the time clause is omitted
  entirely if `tz` is unset or not a valid zoneinfo key. `apply_ambient()`
  mirrors `voice_rules.apply_system_rules`'s copy-don't-mutate,
  append-to-system-message contract and is wired into
  `ChatCompletionsApiModelHandler._serialize` right after
  `apply_system_rules`. `VOICE_PHONE_CONTEXT=off` (case-insensitive)
  disables the whole feature: `update()` becomes a no-op and every reader
  returns `None`, even for state stored before the toggle flipped.
  `websocket_streamer.py` routes `type == "phone_context"` frames straight
  to `update()` via `asyncio.to_thread` (fire-and-forget, no reply frame).
  `voice_tools.py`'s `get_weather` tool gained an optional `place`: an
  omitted/empty place now falls back to `phone_context.location()` and
  skips geocoding (goes straight to the forecast call); if neither a place
  nor a stored location is available, the runner returns the same "I need
  a place name for the weather." wording `execute()`'s generic
  missing-required-arg path used to produce, since `get_weather`'s
  dispatch-table entry is no longer `required` (that auto-refusal would
  otherwise fire before the runner's fallback ever runs).

- `transcript_buffer.py` (new) — `speech_to_speech/transcript_buffer.py`.
  Server-side history replay: a browser that reconnects (phone screen lock,
  backgrounded tab, reload) used to see an empty history rail even though
  another connected screen had the whole conversation (see "One
  conversation, many screens" in the top-level README). `TranscriptBuffer`
  mirrors the webclient's own `transcription_completed` /
  `assistant_text` pairing logic one event behind, keeping the last N
  completed turns (`{"user", "assistant", "ts"}`) in a
  `collections.deque(maxlen=N)` — held in server memory only, cleared on
  restart. `websocket_streamer.py`'s `_send_loop` feeds it every broadcast
  text payload right before the fan-out; `_handle_client` sends the buffer's
  `history_replay` frame to a newly-joined client only (not broadcast) right
  after it's added to `self.clients`, best-effort. Both run in the
  streamer's single asyncio event loop thread, so no lock is needed.
  `VOICE_HISTORY_REPLAY=off` (case-insensitive) disables the feature
  entirely; `VOICE_HISTORY_REPLAY_TURNS` sets N (default 50, matching the
  webclient's own `HISTORY_CAP`). On the client, `webclient/index.html`
  applies a `history_replay` frame only when `conversationHistory` is
  empty (fresh load / lost JS state) — a WS reconnect without a page
  reload already has the turns in memory, so this avoids duplicating them.
  Dependency-light like `voice_rules.py`/`phone_context.py` (no
  `speech_to_speech` import), importable/testable standalone.

## Why there is no shared helper module

Modules in this pack do not import each other, and there is deliberately no
`_utils.py` / `_env.py` for the small things several of them do alike. Three
reasons, in order of how expensive it is to learn them the hard way:

1. **The pack does not deploy to one place.** `apply.sh` copies these files
   into three different packages — `$PKG/`, `$PKG/connections/` and
   `$PKG/LLM/`. No relative import spans those. The only import that would is
   `from speech_to_speech import ...`, which brings us to:
2. **Several modules are deliberately dependency-light** — no
   `speech_to_speech` import at all, so the module and its tests can be
   imported standalone (the tests do exactly that, via `from patches import
   ...`). A shared module reachable only through the framework package would
   trade that away for a few saved lines.
3. **`apply.sh` deploys via an explicit `cp` list.** A new file that isn't
   added to it is silently omitted at deploy, and the first thing that imports
   it crashes the service on restart. That is not hypothetical: it happened to
   `voice_clone.py` and was fixed in `d6d840f`.

So the duplication is a decision, not an oversight. The clearest case is the
fail-open numeric env parser, which exists three times over:

| | signature | why it differs |
|---|---|---|
| `echo_gate._env_number` | `(name, default, cast)` | parses ints as well as floats, so the caller passes the cast |
| `brain_control._env_seconds` | `(name, default) -> float` | only ever reads seconds |
| `wakeword_gate._env_number` | `(name, default) -> float` | only ever reads a threshold |

Note that the first and third share a *name* but not a *signature* — they are
independent copies, not one function in two places, and nothing keeps them in
lockstep. **What they share is a contract, and that is what to keep
consistent:** an unparseable value logs a warning naming the variable and the
default it fell back to, and then carries on. It never raises. Every one of these is read where a raise would be
fatal — `EchoGate()` and `WakewordGate()` are both constructed unguarded in
`WebSocketStreamer.__init__`, so a typo in a hand-edited systemd drop-in must
not be able to stop the assistant from booting. Where a value can be parsed
later instead, do that (`_WSS_PORT_RAW` stays a string until it is inside the
TLS try-block).

Consolidating the HTTP clients was considered and rejected on the same
grounds: the `httpx` call sites are one-liners whose timeouts are chosen per
site — 5.0 s for a Hermes MCP call (`hermes_cockpit.py`), 2.0 s for a QMD
query and 1.5 s for a health probe (`voice_tools.py`), 3.0 s for a brain
reachability probe (`brain_control.py`), and a streamed completion that
enforces its own total-duration watchdog instead (`VOICE_STREAM_MAX_S` in
`chat_completions_language_model.py`). There is no shared behaviour underneath
them, so a common client would add a fourth deploy path to save nothing.

## Files OUTSIDE the package (survive reinstall — not part of this pack)

- `$HOME/speech-to-speech/brains.json` — brain registry (label,
  base_url, model, availability, notes, optional `api_key`/`api_key_file`+
  `api_key_var`). `hermes` flipped to `available: true` for #8.

  `available` is configured intent — the user turned this lane on in the
  registry — and `_config_set`/the panel never mutate it. `reachable` (`true`
  / `false`, or `null` before anything has ever checked) is the orthogonal,
  observed half: the last time a probe actually hit the endpoint, did it
  answer. It updates on every switch attempt (success or failure) and on a
  debounced background sweep (`VOICE_BRAIN_PROBE_DEBOUNCE_S`, default 20s)
  kicked off whenever the panel opens (`config_get`) — the sweep runs on its
  own thread so opening the panel never waits on the network. An unreachable
  brain stays selectable in the UI, just dimmed with the last probe error:
  the entry may be stale, or a real endpoint may have come back up, so the
  panel never blocks a click on it.
- `<patchbay-repo>/webclient/index.html` — settings panel UI
  (gear button, brain radio list, persona textarea, reset-chat button). No
  protocol change was needed for #8 — the ws control-message shape is
  identical on both backends.

## Remote access / HTTPS (mic requires a secure context)

Browsers only allow `getUserMedia` (microphone access) on a "secure context" —
`https://` or `http://localhost`. Loading the cockpit over plain `http://` from
another device on your LAN (by IP or hostname) will silently fail to get mic
permission. Both halves of the stack — the static webclient and the pipeline's
WebSocket — need to run over TLS to fix this.

1. **Generate a certificate.** [mkcert](https://github.com/FiloSottile/mkcert)
   is the easiest path and avoids browser warnings on any device that trusts
   its local CA:

   ```bash
   mkcert -install
   mkcert <lan-ip-or-hostname>   # e.g. mkcert 192.0.2.50
   ```

   This produces a `<name>.pem` (cert) and `<name>-key.pem` (key) in the
   current directory. If you'd rather not install mkcert, a self-signed cert
   with `openssl` works too (browsers will show a one-time warning to click
   through, on each device):

   ```bash
   openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
     -keyout key.pem -out cert.pem -subj "/CN=<lan-ip-or-hostname>"
   ```

2. **Serve the webclient over HTTPS** using the cert pair:

   ```bash
   python3 webclient/serve.py --certfile cert.pem --keyfile key.pem
   ```

3. **Point the pipeline's WebSocket at the same cert pair** via env vars
   before starting the `speech-to-speech` service:

   ```bash
   export VOICE_WS_CERTFILE=/path/to/cert.pem
   export VOICE_WS_KEYFILE=/path/to/key.pem
   # export VOICE_WSS_PORT=8443   # optional, this is the default
   ```

   When both are set, `websocket_streamer.py` starts a second, TLS-wrapped
   listener on `VOICE_WSS_PORT` alongside the existing plain-`ws` listener
   (which keeps running unchanged for `localhost`/http use) — the same cert
   pair from step 1 works for both the webclient and the WebSocket. If the
   cert fails to load, the service logs the error and continues serving
   plain `ws` only; a bad cert never takes down the voice pipeline.

4. **Make sure the client dials the port the server is listening on.** This
   is the one step that used to be silently missing, and it is the trap worth
   reading twice: **`VOICE_WSS_PORT` is server-side configuration the browser
   cannot see.** If the two disagree, the page loads fine, the mic permission
   prompt succeeds, and the socket simply never connects.

   With no configuration, the webclient derives the socket URL from its own
   address: `wss://<page-host>:8443` when the page is HTTPS, and
   `ws://<page-host>:8765` when it is HTTP. Those match the pipeline's
   defaults, so if you left `VOICE_WSS_PORT` alone there is nothing to do.

   If your socket is anywhere else — a different port, a different host, or
   behind a reverse proxy — tell the client explicitly. Two equivalent ways:

   - **Settings panel → "Server socket URL"**, e.g.
     `wss://192.0.2.50:9443`. Stored in that browser's `localStorage`;
     "Use default" clears it and returns to the derived URL.
   - **`?ws=` query parameter**, e.g.
     `https://192.0.2.50:8771/?ws=wss://192.0.2.50:9443`. Handy for a
     one-off or for sharing a working link; it is persisted the same way, so
     later visits without the parameter keep using it.

   A failed connection now names the URL it tried and the likely cause, both
   in the diagnostics log and next to that settings field. **There is
   deliberately no fallback from `wss://` to `ws://`**: an HTTPS page is
   forbidden by the browser's mixed-content rules from opening a plain `ws://`
   socket, so a fallback could only ever fail more slowly and with a worse
   error message.

5. **A reverse proxy is a fine alternative to native TLS.** If something
   already terminates TLS in front of the box — Tailscale Serve, Caddy,
   nginx — you can skip `VOICE_WS_CERTFILE`/`VOICE_WS_KEYFILE` entirely and
   have the proxy forward a public port to the plain listener on
   `localhost:8765`. The same rule applies: whatever port the proxy publishes
   is the port the client must dial, so either publish `8443` (the client's
   default for HTTPS pages) or set the socket URL as in step 4. For example,
   this pair serves the page at the root and the socket on the default port,
   with no certs configured on the pipeline at all:

   ```bash
   tailscale serve --bg --https 443 http://localhost:8770
   tailscale serve --bg --https 8443 http://localhost:8765
   ```

   **Pick one or the other on a given port — they collide.** If a proxy is
   already publishing `8443` and you also set `VOICE_WS_CERTFILE`/
   `VOICE_WS_KEYFILE`, the native listener cannot bind and you get exactly one
   line in the journal before the service carries on without it:

   ```
   ERROR - WebSocket TLS listener failed to start ([Errno 98] error while
   attempting to bind on address ('0.0.0.0', 8443): address already in use);
   continuing plain-only
   ```

   That is deliberate — a port clash must not take the voice pipeline down —
   but it is quiet, and the symptom shows up somewhere else entirely: pages
   reached *through* the proxy keep working (the proxy is still serving
   `8443`), while a device that reaches the box by an address the proxy does
   not answer on — its LAN IP, typically — dials `wss://<lan-ip>:8443` and
   finds nothing. Same page, same browser, different address, opposite result.
   If you want both, move the native listener with `VOICE_WSS_PORT` and tell
   the client where it went (step 4). **Check for this line after any change
   to TLS configuration**; nothing else reports it.

6. **`localhost` needs none of this** — plain `http`/`ws` on `localhost` is
   already a secure context, so the mic works there with no cert setup at
   all.

## Re-applying

```bash
bash <patchbay-repo>/patches/apply.sh
sudo systemctl restart voice-agent
```

`apply.sh` locates the current `speech_to_speech` package directory via the
active venv's Python (`import speech_to_speech; ... __file__`) — since
`$INSTALL_DIR` is an editable install, this resolves
straight to the repo's `src/speech_to_speech`, so a `pip install` of new deps
never wipes these edits (only a `git checkout` of those three files would).
Safe to re-run any number of times.

**A plain run deploys.** Because that resolution lands on the live source
tree, there is no separate staging step — `apply.sh` with no arguments writes
straight into the running install. Two flags exist so you never have to find
that out the hard way:

```bash
bash patches/apply.sh --dry-run              # preview; writes nothing
bash patches/apply.sh --dest /tmp/staging    # write somewhere else entirely
```

`--dry-run` prints one line per file marked `new`, `unchanged`, or
`OVERWRITE`, so you can see exactly what a real run would clobber before it
does. `--dest DIR` (or `DEST=DIR`) skips the venv resolution altogether and
writes to `DIR` — use it to stage a candidate pack anywhere that isn't
production. The two compose; `--dry-run --dest DIR` previews against an
arbitrary tree.

Both a real run and a dry run pre-flight the whole file list first — every
source file and every destination subdirectory — and abort having copied
**nothing** if any are missing. Without that, a missing file aborts partway
through and leaves a half-applied tree that looks fine until the next
restart.

## Rollback

The patch pack is version-controlled, so rolling back is a checkout plus a re-apply.
`apply.sh` overwrites the upstream files it manages, so re-running it from an older
checkout restores that older state:

```bash
cd <patchbay-repo>
git checkout <previous-tag-or-commit>   # whatever you deployed last
bash patches/apply.sh
sudo systemctl restart voice-agent
```

To go further and return the framework itself to pristine upstream, discard the overlay
in the install tree (`$INSTALL_DIR` is an editable checkout, so this is a plain revert):

```bash
cd "$INSTALL_DIR"                       # default ~/speech-to-speech-main
git checkout -- src/speech_to_speech
sudo systemctl restart voice-agent
```

Before a risky deploy, snapshot the two trees that a deploy actually touches, so you can
restore without a network fetch:

```bash
tar czf /tmp/pipeline-backup-$(date +%Y%m%d-%H%M%S).tar.gz -C "$INSTALL_DIR" src/speech_to_speech
tar czf /tmp/webclient-backup-$(date +%Y%m%d-%H%M%S).tar.gz -C <patchbay-repo> webclient
```

To restore one of those snapshots, extract it back over the same tree and restart:

```bash
tar xzf /tmp/pipeline-backup-<stamp>.tar.gz -C "$INSTALL_DIR"
sudo systemctl restart voice-agent

tar xzf /tmp/webclient-backup-<stamp>.tar.gz -C <patchbay-repo>
sudo systemctl restart voice-webclient    # only if you serve it as a unit
```

Note that `brains.json`, `persona.json`, `model_overrides.json` and `voices/` live in the
sidecar directory (see above) and are deliberately untouched by any of this — a rollback
never discards your configuration or your cloned voices.
