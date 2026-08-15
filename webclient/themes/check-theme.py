#!/usr/bin/env python3
"""Gate 1 for THEME-SPEC.md: every style-rule selector in a theme CSS file
must be scoped under [data-theme="<id>"].

Usage: check-theme.py <path-to-id.css> <id>
Exit 0: every selector is scoped correctly.
Exit 1: prints each offending selector, one per line, then exits nonzero.

@keyframes bodies (from/to/N% are not element selectors) are stripped before
checking. @media prelude lines ("@media (min-width: 1024px)") are not
selectors themselves and are skipped, but selectors nested inside an @media
block ARE checked. @font-face blocks (a resource declaration, not a style
rule with a selector to scope) are skipped the same way. Comments are
stripped. Stdlib only.
"""
import re
import sys


def find_unscoped_selectors(css_text: str, theme_id: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    text = re.sub(r"@keyframes\s+[\w-]+\s*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", text)

    scope = f'[data-theme="{theme_id}"]'
    bad = []
    cur = ""
    for ch in text:
        if ch == "{":
            sel = " ".join(cur.split()).strip()
            cur = ""
            if sel and not sel.startswith("@media") and not sel.startswith("@font-face"):
                for branch in sel.split(","):
                    branch = branch.strip()
                    if branch and scope not in branch:
                        bad.append(branch)
        elif ch == "}":
            cur = ""
        else:
            cur += ch
    return bad


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check-theme.py <path-to-id.css> <id>", file=sys.stderr)
        return 2
    path, theme_id = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8") as f:
        css_text = f.read()

    bad = find_unscoped_selectors(css_text, theme_id)
    if bad:
        print(f'FAIL {path}: {len(bad)} selector(s) not scoped under [data-theme="{theme_id}"]:')
        for sel in bad:
            print(f"  - {sel}")
        return 1

    print(f'OK   {path}: all rule selectors scoped under [data-theme="{theme_id}"]')
    return 0


if __name__ == "__main__":
    sys.exit(main())
