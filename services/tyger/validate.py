#!/usr/bin/env python3
"""Run upstream's scripts/validate/ears_batched.py (batched engine vs a plain single-session reference) against the
local bundle. Two things upstream's script assumes that a fresh install does not have are patched in memory, and the
run refuses to start if the pinned script no longer contains the lines being patched:
  * bundle.fetch wants an HF_TOKEN though the bundle is public (upstream I-28): it returns the local directory;
  * the test clips are decoded by `datasets` through torchcodec: decode them with soundfile instead.
usage: validate.py <install dir>      expect "events match: True" for every session
"""
import sys
from pathlib import Path

home = Path(sys.argv[1]).expanduser().resolve()
upstream, bundle = home / "tyger-s2s", home / "bundle"
script = upstream / "scripts" / "validate" / "ears_batched.py"
sys.path.insert(0, str(upstream))

import app.plugins.ears.tyger.bundle as upstream_bundle  # noqa: E402
upstream_bundle.fetch = lambda repo, revision=None: bundle

PATCHES = [(
    'clip = lambda i: np.asarray(ds[i]["audio"]["array"], np.float32)',
    'import io, soundfile as sf\n'
    'from datasets import Audio\n'
    'ds = ds.cast_column("audio", Audio(decode=False))\n'
    'clip = lambda i: np.asarray(sf.read(io.BytesIO(ds[i]["audio"]["bytes"]))[0], np.float32)',
)]
src = script.read_text()
for old, new in PATCHES:
    if old not in src:
        sys.exit(f"{script}: the line to patch is gone (upstream moved?): {old}")
    src = src.replace(old, new)
sys.argv = [str(script)]
exec(compile(src, str(script), "exec"), {"__name__": "__main__", "__file__": str(script)})
