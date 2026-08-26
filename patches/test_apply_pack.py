"""Guards against a patch module shipping with an import apply.sh can't satisfy.

The incident this guards against: v2.6.0 added patches/brain_lanes.py and
brain_control.py started importing it (`from speech_to_speech import
brain_lanes`), but apply.sh's FILES array -- the explicit list of what gets
copied into the installed package -- never gained an entry for it. Every
unit test stayed green because they import from patches/ directly via
sys.path, never crossing the apply.sh boundary; only a real install crossed
it, and that install could not boot.

Two independent checks, run separately so a missing install doesn't hide the
part that needs no install:

* test_own_module_imports_are_shipped -- for every `speech_to_speech.<name>`
  (or `from speech_to_speech import <name>`) a patch module references, if
  `<name>` matches the filename of one of OUR OWN patches/*.py modules, then
  that exact dotted path must appear in apply.sh's FILES array. This needs no
  install and never skips -- it is the half that would have caught the
  brain_lanes omission by itself.
* test_upstream_imports_resolve -- the same scan, but for references that do
  NOT match one of our own module names (i.e. presumed upstream
  speech-to-speech internals). These are checked against a real checkout;
  the test skips cleanly when no checkout is present.

A third, blunter check (test_all_patch_modules_are_shipped) asserts every
non-test patches/*.py file is listed in FILES at all, with an explicit
exclusion set for any file that legitimately should not ship.

Run from repo root: python3 -m pytest patches/test_apply_pack.py -v
"""

from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest

HERE = pathlib.Path(__file__).resolve().parent
APPLY_SH = HERE / "apply.sh"

# Non-test patches/*.py files that intentionally do NOT ship via apply.sh's
# FILES array. Empty today -- every module patches/ carries is meant to
# reach the installed package. Add an entry here (with a reason) the day
# that stops being true, so the next omission is a deliberate act rather
# than a silent gap like the one this file guards against.
NOT_SHIPPED = {
    # "example_module.py": "reason it deliberately stays out of FILES",
}


def _parse_files_array(apply_sh_path):
    """Extract apply.sh's FILES=( "src|dst" ... ) array as [(src, dst), ...].

    Uses plain text scanning rather than sourcing the script: this needs to
    work without bash, and a `"src|dst"` line is unambiguous enough that a
    regex is not fragile here.
    """
    text = apply_sh_path.read_text(encoding="utf-8")
    m = re.search(r"^FILES=\(\n(.*?)^\)\n", text, re.DOTALL | re.MULTILINE)
    assert m, "could not find a FILES=( ... ) array in apply.sh"
    body = m.group(1)
    pairs = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entry_m = re.match(r'^"([^"|]+)\|([^"]+)"$', line)
        assert entry_m, f"unparseable FILES entry in apply.sh: {line!r}"
        pairs.append((entry_m.group(1), entry_m.group(2)))
    return pairs


def _dotted(dst_or_src_path):
    """'LLM/lm_output_processor.py' -> 'LLM.lm_output_processor'."""
    p = dst_or_src_path[:-3] if dst_or_src_path.endswith(".py") else dst_or_src_path
    return p.replace("/", ".").replace("\\", ".")


def _own_patch_stems():
    """Filenames (minus .py) of every non-test module living in patches/."""
    return {
        f.stem
        for f in HERE.glob("*.py")
        if f.name != "__init__.py" and not f.name.startswith("test_")
    }


def _iter_speech_to_speech_refs(tree):
    """Yield (dotted_ref, lineno) for every `speech_to_speech...` import.

    dotted_ref is always normalized to 'speech_to_speech.<submodule.path>',
    regardless of whether the source wrote `from speech_to_speech import X`,
    `from speech_to_speech.a.b import Y`, or `import speech_to_speech.a.b`.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "speech_to_speech":
                for alias in node.names:
                    if alias.name != "*":
                        yield f"speech_to_speech.{alias.name}", node.lineno
            elif node.module.startswith("speech_to_speech."):
                yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("speech_to_speech."):
                    yield alias.name, node.lineno


def _non_test_patch_files():
    return sorted(
        f
        for f in HERE.glob("*.py")
        if f.name != "__init__.py" and not f.name.startswith("test_")
    )


def _install_dir():
    default = str(pathlib.Path.home() / "speech-to-speech-main")
    return pathlib.Path(os.environ.get("INSTALL_DIR", default))


class TestApplyPackFiles:
    def setup_method(self):
        self.files = _parse_files_array(APPLY_SH)
        self.shipped_dotted = {f"speech_to_speech.{_dotted(dst)}" for _src, dst in self.files}
        self.own_stems = _own_patch_stems()

    def test_own_module_imports_are_shipped(self):
        """Never skips: catches an own-module reference apply.sh doesn't ship."""
        missing = []
        for path in _non_test_patch_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for ref, lineno in _iter_speech_to_speech_refs(tree):
                submodule_path = ref[len("speech_to_speech."):]
                last_component = submodule_path.rsplit(".", 1)[-1]
                if last_component in self.own_stems and ref not in self.shipped_dotted:
                    missing.append(f"{path.name}:{lineno} imports {ref!r}, not in apply.sh's FILES")
        assert not missing, (
            "patch module(s) import one of our own modules by a name apply.sh "
            "does not ship:\n" + "\n".join(missing)
        )

    def test_upstream_imports_resolve(self):
        """Skips cleanly with no install; otherwise verifies against a real checkout."""
        pkg_dir = _install_dir() / "src" / "speech_to_speech"
        if not pkg_dir.is_dir():
            pytest.skip(f"no speech-to-speech checkout at {pkg_dir} -- run setup.sh to get one")

        unresolved = []
        for path in _non_test_patch_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for ref, lineno in _iter_speech_to_speech_refs(tree):
                submodule_path = ref[len("speech_to_speech."):]
                last_component = submodule_path.rsplit(".", 1)[-1]
                if last_component in self.own_stems:
                    continue  # covered by test_own_module_imports_are_shipped
                parts = submodule_path.split(".")
                as_module = pkg_dir.joinpath(*parts).with_suffix(".py")
                as_package = pkg_dir.joinpath(*parts, "__init__.py")
                if not as_module.is_file() and not as_package.is_file():
                    unresolved.append(f"{path.name}:{lineno} imports {ref!r}, not found under {pkg_dir}")
        assert not unresolved, (
            "patch module(s) import an upstream submodule that does not exist "
            "in the checkout:\n" + "\n".join(unresolved)
        )

    def test_all_patch_modules_are_shipped(self):
        """Blunter invariant: every non-test patches/*.py file is in FILES."""
        shipped_srcs = {src for src, _dst in self.files}
        present = {f.name for f in _non_test_patch_files()}
        expected_shipped = present - set(NOT_SHIPPED)
        missing = sorted(expected_shipped - shipped_srcs)
        assert not missing, (
            "patch module(s) exist in patches/ but are not listed in "
            f"apply.sh's FILES array: {missing}"
        )
