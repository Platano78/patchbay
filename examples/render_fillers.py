#!/usr/bin/env python3
"""Pre-render Patchbay's filler phrases, one clip per persona voice.

The phrases are the ones the pipeline already speaks: the tool-call pool
(`VOICE_TOOL_FILLERS`, else the built-ins in patches/lm_output_processor.py)
plus the turn-end pool (`VOICE_TURN_FILLERS`, which defaults to the tool pool).
Run it with the SAME environment as the service, so it renders what the service
would pick.

Each phrase goes through the speech server (POST /v1/audio/speech, raw pcm,
header X-Voicebox-Priority: interactive), is resampled to the pipeline output
rate, trimmed of leading/trailing silence, and written to
    <dir>/<voice>/<slug>.pcm       (raw s16le mono)
where <dir> is --dir, else $VOICE_FILLER_DIR, else ~/.cache/patchbay/fillers.
At runtime `patches/filler_clips.py` plays a clip when its phrase is picked and
a clip exists for the active voice; otherwise the phrase is spoken as today.

--rate MUST equal the pipeline output rate: the TTS handler's
`remote_speech_sample_rate` (default 16000; the service passes no override).
--source-rate is what the server returns: the service's
`remote_speech_source_sample_rate` (48000 for voxcpm2).

Idempotent: a clip that already exists is skipped (--force re-renders).

    python3 examples/render_fillers.py --base-url http://HOST:PORT --dry-run
    python3 examples/render_fillers.py --base-url http://HOST:PORT --voice ryan
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
MIN_CLIP_S, MAX_CLIP_S = 0.15, 3.0


def _load_filler_clips():
    spec = importlib.util.spec_from_file_location("filler_clips_for_render", PATCHES / "filler_clips.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default_pool() -> tuple[str, ...]:
    """_DEFAULT_TOOL_FILLERS read from lm_output_processor.py without importing it
    (it needs the whole framework); one source of truth, no copy to drift."""
    tree = ast.parse((PATCHES / "lm_output_processor.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_DEFAULT_TOOL_FILLERS":
            return tuple(ast.literal_eval(node.value))
    raise SystemExit("could not find _DEFAULT_TOOL_FILLERS in patches/lm_output_processor.py")


def parse_pool(env_name: str, fallback: tuple[str, ...], env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Mirror of lm_output_processor._parse_tool_fillers / _parse_turn_fillers
    (a test pins the two together)."""
    env = os.environ if env is None else env
    raw = env.get(env_name)
    if raw is None:
        return fallback
    if raw.strip().lower() == "off":
        return ()
    phrases = tuple(p.strip() for p in raw.split("|") if p.strip())
    return phrases or fallback


def active_phrases(env: dict[str, str] | None = None) -> list[str]:
    tool = parse_pool("VOICE_TOOL_FILLERS", _default_pool(), env)
    turn = parse_pool("VOICE_TURN_FILLERS", tool, env)
    seen: dict[str, None] = {}
    for p in (*tool, *turn):
        seen.setdefault(p)
    return list(seen)


def _load_voice_design():
    spec = importlib.util.spec_from_file_location("voice_design_for_render", PATCHES / "voice_design.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# One copy of the clip pipeline, shared with the running service (a voice
# designed mid-conversation renders its own fillers through the same code).
_vd = _load_voice_design()
trim_silence = _vd.trim_silence
to_clip = _vd.to_clip


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310 (operator-supplied URL)
        return json.loads(r.read())


def render(base: str, model: str, voice: str, phrase: str, timeout: float) -> bytes:
    body = json.dumps({"model": model, "input": phrase, "voice": voice, "response_format": "pcm"}).encode()
    req = urllib.request.Request(
        base + "/v1/audio/speech",
        data=body,
        headers={"Content-Type": "application/json", "X-Voicebox-Priority": "interactive"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", default=os.environ.get("VOICE_FILLER_BASE_URL"), help="speech server, e.g. http://host:27419 (or $VOICE_FILLER_BASE_URL)")
    ap.add_argument("--model", default="voxcpm2")
    ap.add_argument("--voice", action="append", help="limit to this voice (repeatable); default: every voice the server lists")
    ap.add_argument("--rate", type=int, default=16000, help="pipeline output rate (remote_speech_sample_rate); default 16000")
    ap.add_argument("--source-rate", type=int, default=48000, help="rate of the server's pcm (remote_speech_source_sample_rate); default 48000")
    ap.add_argument("--dir", help="cache dir (default $VOICE_FILLER_DIR or ~/.cache/patchbay/fillers)")
    ap.add_argument("--force", action="store_true", help="re-render clips that already exist")
    ap.add_argument("--dry-run", action="store_true", help="list voices x phrases and what would be rendered, write nothing")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    if not args.base_url:
        ap.error("--base-url is required (or set VOICE_FILLER_BASE_URL)")
    base = args.base_url.rstrip("/")
    fc = _load_filler_clips()
    root = Path(args.dir).expanduser() if args.dir else fc.cache_root()
    phrases = active_phrases()
    if not phrases:
        print("no filler phrases are active (VOICE_TOOL_FILLERS=off?); nothing to render")
        return 0

    voices = args.voice or _get_json(f"{base}/v1/audio/voices?model={urllib.parse.quote(args.model)}")["voices"]
    print(f"{len(voices)} voice(s) x {len(phrases)} phrase(s) -> {root}  (rate {args.rate} Hz)")
    failures = rendered = skipped = 0
    for voice in voices:
        for phrase in phrases:
            path = fc.clip_path(voice, phrase, root)
            exists = path.exists()
            if args.dry_run:
                print(f"  {'skip  ' if exists and not args.force else 'render'}  {voice:24s} {phrase!r:24s} -> {path}")
                continue
            if exists and not args.force:
                skipped += 1
                continue
            try:
                clip = to_clip(render(base, args.model, voice, phrase, args.timeout), args.source_rate, args.rate)
            except Exception as exc:
                print(f"  FAIL    {voice} {phrase!r}: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures += 1
                continue
            secs = len(clip) / 2 / args.rate
            if not MIN_CLIP_S <= secs <= MAX_CLIP_S:
                print(f"  REJECT  {voice} {phrase!r}: {secs:.2f}s is outside {MIN_CLIP_S}-{MAX_CLIP_S}s", file=sys.stderr)
                failures += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(clip)
            tmp.replace(path)
            rendered += 1
            print(f"  ok      {voice:24s} {phrase!r:24s} {secs:.2f}s")
    if not args.dry_run:
        print(f"rendered {rendered}, skipped {skipped} existing, failed {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
