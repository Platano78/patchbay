# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.8.0] - 2026-09-24

### Added

- `examples/smoke_turn.py`: verifies a real spoken turn (heard → answer →
  reply audio) end to end without a microphone; on failure it prints one
  sentence naming what didn't arrive and exits `1`.
- Host-header allowlist against DNS rebinding (see Security) and tests for it:
  `webclient/test_host_guard.py`, plus `patches/test_ws_host_default.py` for
  the loopback default.
- This changelog.

### Changed

- setup.sh: skips the optional kokoro TTS extra on Python 3.13+ (it cannot
  resolve there and failed the whole install on a stock Python 3.14); checks
  for `git` and the `venv` module before cloning anything; `--check` prints
  one line instead of a traceback and probes playwright inside the venv,
  where the tests run.
- User-facing install hints name the framework venv's own binaries — the
  `hf auth login` and `pip install soundfile` hints in the pipeline and in
  `patches/voice_clone.py`, and the remote-speech example — because a bare
  `pip` hits the system Python, which the pipeline does not use.
- Documentation rewritten for a stranger, or the agent installing for one:
  AGENTS.md is an install guide with exact commands, expected output and a
  troubleshooting table in the software's own words (CLAUDE.md imports it);
  SETUP.md gains prerequisites incl. `python3-venv` and a download table with
  sizes and licences; README/NOTICE list the twelve bundled avatar heads with their
  real licences and the HA routes `serve.py` actually exposes; the WS protocol
  contract, THEME-SPEC and patches/README were corrected against the code.
- Phone location: the settings panel, README, SETUP and SECURITY.md now state
  before the opt-in that the server sends the coordinates to OpenStreetMap's
  Nominatim for the place lookup, and what reaches the brain.
- Docs state where untracked theme files under `webclient/themes/local/`
  actually go: they are served to anyone who can reach the cockpit port.
- Tests run outside the framework venv now stop with one sentence naming the
  venv command (root `conftest.py`) instead of crashing with import
  tracebacks; inside the venv nothing changes.
- The level-bus and level-fast perf gates compare against a control measured
  in the same run instead of fixed millisecond thresholds tuned on one
  machine.
- AGENTS.md rule: an installing agent must ask before installing the systemd
  service; a foreground run plus the smoke check is a finished setup.

### Fixed

- Wake word selected without `openwakeword` installed used to be accepted,
  then fail open and log `wake word detected (score=None)` while every
  utterance reached the brain. It is now refused with the reason, and the
  toggle and ARM are greyed out with that reason; a fail-open is logged as
  one.
- The cockpit status no longer stays "processing…" (THINK lamp, THINKING
  needle) after push-to-talk turns — including silent holds: saying nothing
  produces no server frames at all, so a 10 s "nothing heard" watchdog now
  ends the state; any real sign of life (partial, transcript, answer,
  assistant audio, failure, a new hold, socket close) cancels it.
- SETUP's phone-context check relied on a `config_get` field that does not
  exist; replaced with checks a user can see.
- `examples/smoke_turn.py` reports a connection dropped mid-turn in one
  sentence instead of a traceback.

### Security

- Safe-by-default bindings: `serve.py` binds `127.0.0.1`, and the patched
  pipeline overrides the framework's `ws_host` default of `0.0.0.0` to
  `127.0.0.1` on both the CLI and the JSON-config parse paths. An explicit
  `--ws_host` / `"ws_host"` still wins; the systemd template and README pass
  `0.0.0.0` explicitly for LAN access.
- DNS-rebinding guard: the cockpit HTTP server (`webclient/host_guard.py`)
  and the pipeline WebSocket now reject requests whose `Host` header is not
  an IP literal, `localhost`, one of this machine's own names (hostname,
  FQDN, `.local`, Tailscale MagicDNS), a hostname named in `VOICE_WS_ORIGINS`,
  or a name the operator allowed via `VOICE_ALLOWED_HOSTS` (`*` disables it). A rebinding page carries the attacker's hostname,
  which no same-origin check can see.
- `POST /ha/intent` requires a JSON body and a same-host Origin, so a web
  page on another origin can no longer drive Home Assistant through the
  cockpit.

## [2.7.0] - 2026-09-13

Earlier releases are described on the GitHub releases page:
<https://github.com/Platano78/patchbay/releases>.
