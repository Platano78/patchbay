"""Drives the real setup.sh in a subprocess against stub python3/git/pip.

Nothing here touches the network or clones anything. setup.sh is run
byte-for-byte as shipped, from a scratch copy of the repo layout it expects
(so its `cp brains.json.example brains.json` lands in the scratch dir, not
this checkout); only the tools it calls are stubbed:

* ``python3`` -- one Python shim used as BOTH the system interpreter and the
  venv's ``bin/python3``. It runs ``-c`` code on the real interpreter (with no
  site-packages, i.e. an empty venv) with ``sys.version_info`` overridden
  (``FAKE_PY_VERSION``), fakes a missing
  venv/ensurepip module (``FAKE_HAS_VENV=0``) and answers the playwright
  probe per interpreter role (``FAKE_PLAYWRIGHT_SYSTEM`` /
  ``FAKE_PLAYWRIGHT_VENV``), so the version gate and the probes are the
  script's own code running for real.
* ``pip`` -- records its arguments, and refuses the kokoro extra on Python
  >= 3.13 the way the real resolver does (kokoro's Requires-Python <3.13).
* ``git`` -- ``clone`` just creates the target directory.

Run from repo root: python3 -m pytest patches/test_setup_sh.py -v
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

_PYTHON_SHIM = f"""#!{sys.executable}
import os, sys
role = "venv" if "/.venv/" in os.path.abspath(__file__) else "system"
with open(os.environ["SHIM_LOG"], "a") as log:
    log.write(role + " python3 " + repr(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:1] == ["-c"]:
    code = args[1]
    if "import venv, ensurepip" in code:
        sys.exit(0 if os.environ.get("FAKE_HAS_VENV", "1") == "1" else 1)
    if "playwright" in code:
        sys.exit(0 if os.environ.get("FAKE_PLAYWRIGHT_" + role.upper()) == "1" else 1)
    major, minor = (int(x) for x in os.environ.get("FAKE_PY_VERSION", "3.12").split("."))
    prelude = "import sys; sys.version_info = (%d, %d, 0, 'final', 0)\\n" % (major, minor)
    # -S: no site-packages, so this is a venv with nothing installed in it --
    # whatever the interpreter running the tests happens to have (the real
    # speech_to_speech, say) must not leak into setup.sh's import probes.
    os.execv(sys.executable, [sys.executable, "-S", "-c", prelude + code] + args[2:])
if args[:2] == ["-m", "venv"]:
    if os.environ.get("FAKE_HAS_VENV", "1") != "1":
        print("The virtual environment was not created successfully because ensurepip is not available.")
        sys.exit(1)
    bin_dir = os.path.join(args[2], "bin")
    os.makedirs(bin_dir)
    with open(os.path.abspath(__file__)) as src, open(os.path.join(bin_dir, "python3"), "w") as dst:
        dst.write(src.read())
    with open(os.environ["PIP_SHIM_SRC"]) as src, open(os.path.join(bin_dir, "pip"), "w") as dst:
        dst.write(src.read())
    os.chmod(os.path.join(bin_dir, "python3"), 0o755)
    os.chmod(os.path.join(bin_dir, "pip"), 0o755)
    sys.exit(0)
sys.exit(0)  # a script path (brain_discovery.py): nothing to discover here
"""

_PIP_SHIM = f"""#!{sys.executable}
import os, sys
with open(os.environ["SHIM_LOG"], "a") as log:
    log.write("pip " + " ".join(sys.argv[1:]) + "\\n")
version = tuple(int(x) for x in os.environ.get("FAKE_PY_VERSION", "3.12").split("."))
if any("[kokoro]" in a for a in sys.argv[1:]) and version >= (3, 13):
    print("ERROR: Package 'kokoro' requires a different Python: %d.%d.0 not in '<3.13,>=3.10'" % version)
    sys.exit(1)
"""

_GIT_SHIM = """#!/bin/sh
echo "git $*" >> "$SHIM_LOG"
if [ "$1" = "clone" ]; then mkdir -p "$3"; fi
exit 0
"""

# What setup.sh and apply.sh need from coreutils. A PATH of only these (plus
# the shims) is what lets a test take `git` away.
_TOOLS = ("bash", "sh", "dirname", "basename", "cp", "cat", "grep", "tail", "mkdir", "cmp", "diff")


def _write_exec(path: pathlib.Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _layout(tmp_path: pathlib.Path, *, with_git: bool = True, real_apply: bool = False) -> dict:
    """Scratch repo copy + stub bin dir. Returns paths and the subprocess env."""
    tree = tmp_path / "tree"
    (tree / "patches").mkdir(parents=True)
    shutil.copy(REPO / "setup.sh", tree / "setup.sh")
    shutil.copy(REPO / "brains.json.example", tree / "brains.json.example")
    if real_apply:
        shutil.copy(REPO / "patches" / "apply.sh", tree / "patches" / "apply.sh")
    else:
        _write_exec(tree / "patches" / "apply.sh", "#!/bin/sh\nexit 0\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "python3", _PYTHON_SHIM)
    pip_src = tmp_path / "pip-shim"
    _write_exec(pip_src, _PIP_SHIM)
    if with_git:
        _write_exec(bin_dir / "git", _GIT_SHIM)
    for tool in _TOOLS:
        if shutil.which(tool):
            (bin_dir / tool).symlink_to(shutil.which(tool))

    log = tmp_path / "shim.log"
    log.write_text("")
    return {
        "tree": tree,
        "install": tmp_path / "install",
        "log": log,
        "env": {
            "PATH": str(bin_dir),
            "HOME": str(tmp_path),
            "INSTALL_DIR": str(tmp_path / "install"),
            "SHIM_LOG": str(log),
            "PIP_SHIM_SRC": str(pip_src),
        },
    }


def _fake_venv(lay: dict) -> None:
    venv_bin = lay["install"] / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    shutil.copy(pathlib.Path(lay["env"]["PATH"]) / "python3", venv_bin / "python3")


def _run(lay: dict, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(lay["tree"] / "setup.sh"), *args],
        env={**lay["env"], **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _pip_calls(lay: dict) -> list[str]:
    return [line for line in lay["log"].read_text().splitlines() if line.startswith("pip ")]


# ── fix 1: the kokoro extra only where kokoro can install ────────────────


def test_python_314_skips_kokoro_and_completes(tmp_path):
    lay = _layout(tmp_path)
    r = _run(lay, FAKE_PY_VERSION="3.14")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        "Skipping the optional kokoro TTS extra: it does not support Python 3.14 yet "
        "(pocket, the default TTS, is unaffected)." in r.stdout.splitlines()
    )
    assert not any("[kokoro]" in c for c in _pip_calls(lay)), _pip_calls(lay)
    assert any(c.endswith("[pocket,websocket]") for c in _pip_calls(lay)), _pip_calls(lay)
    assert "SETUP OK" in r.stdout


def test_python_312_still_installs_kokoro(tmp_path):
    lay = _layout(tmp_path)
    r = _run(lay, FAKE_PY_VERSION="3.12")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Skipping the optional kokoro" not in r.stdout
    assert any(c.endswith("[kokoro]") for c in _pip_calls(lay)), _pip_calls(lay)
    assert any(c.endswith("[pocket,websocket]") for c in _pip_calls(lay)), _pip_calls(lay)


# ── fix 2: preflight before anything is created ──────────────────────────


def test_missing_venv_module_stops_before_cloning(tmp_path):
    lay = _layout(tmp_path)
    r = _run(lay, FAKE_HAS_VENV="0")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "python3 cannot create virtual environments on this system" in r.stderr
    assert "sudo apt install python3-venv" in r.stderr
    assert "Traceback" not in r.stdout + r.stderr
    # Nothing created, so a re-run after installing python3-venv just works.
    assert not lay["install"].exists()
    assert "git clone" not in lay["log"].read_text()


def test_missing_git_stops_before_anything(tmp_path):
    lay = _layout(tmp_path, with_git=False)
    r = _run(lay)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "git is not installed" in r.stderr
    assert not lay["install"].exists()


# ── fix 3: --check speaks in plain words and probes the venv ─────────────


def test_check_reports_apply_failure_as_one_line(tmp_path):
    """The real apply.sh against a venv where speech_to_speech isn't
    importable: its dry-run dies with a full traceback, and --check must
    carry only the line that names the problem."""
    lay = _layout(tmp_path, real_apply=True)
    _fake_venv(lay)
    r = _run(lay, "--check")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Traceback" not in r.stdout
    lines = [line for line in r.stdout.splitlines() if line.startswith("CHECK patches-applied:")]
    assert len(lines) == 1, r.stdout
    assert "ModuleNotFoundError: No module named 'speech_to_speech'" in lines[0]
    # Every line of --check output is a CHECK line -- no traceback spill-over.
    assert all(line.startswith("CHECK ") for line in r.stdout.splitlines()), r.stdout


def test_check_playwright_probes_the_venv_not_system_python(tmp_path):
    lay = _layout(tmp_path)
    _fake_venv(lay)

    # System python has playwright, the venv (where the tests run) does not.
    r = _run(lay, "--check", FAKE_PLAYWRIGHT_SYSTEM="1", FAKE_PLAYWRIGHT_VENV="0")
    line = next(x for x in r.stdout.splitlines() if x.startswith("CHECK playwright"))
    assert "FAIL" in line and "venv" in line, line

    r = _run(lay, "--check", FAKE_PLAYWRIGHT_SYSTEM="0", FAKE_PLAYWRIGHT_VENV="1")
    line = next(x for x in r.stdout.splitlines() if x.startswith("CHECK playwright"))
    assert "PASS" in line and "venv" in line, line

    probes = [x for x in lay["log"].read_text().splitlines() if "playwright" in x]
    assert probes and all(x.startswith("venv python3") for x in probes), probes
