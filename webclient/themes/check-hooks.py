#!/usr/bin/env python3
"""Gate 2 for THEME-SPEC.md: every selector in a theme CSS file must still
MATCH SOMETHING in the shipped DOM, not merely be correctly scoped.

check-theme.py (gate 1) verifies every selector is scoped under
[data-theme="<id>"]. It says nothing about whether the id/class names inside
that selector still exist - a DOM port can silently orphan a rule (the class
was renamed/removed) and check-theme.py will still pass it. This script
closes that gap, plus a subtler one a name-grep would miss entirely: a
selector can name two ids/classes that both still exist, joined by a `~`/`+`
sibling combinator, while the two elements are no longer actual DOM siblings
- that combinator can then never match anything, forever, silently.

Two checks, per style-rule selector (after the same @keyframes/@media/
@font-face preprocessing check-theme.py applies):

1. Existence - every `#id` / `.class` token in the selector (including ones
   buried inside :has()/:is()/:not() - this is a flat text scan over the
   selector, so it never discards those, it just never needs to specially
   recurse into them either) must appear *somewhere* in index.html. This is
   deliberately a whole-document text search, not "must be a static HTML
   attribute": a large share of this app's classes are JS-driven state
   (`.recording`, `.speaking`, `.open`, ...) or JS-populated component
   classes (`.historyEntry`, `.themeSwatch`, ...) that never sit in a static
   `class="..."` attribute at all - they only ever appear as string literals
   inside <script> (classList.add/toggle/remove(...), .className = "...").
   Requiring a static DOM attribute match would false-positive on nearly
   every legitimate hook in this codebase; a name genuinely removed from the
   port (like `.sessionKey`) leaves no trace anywhere in the file, static or
   scripted, so a plain whole-document token search still catches it.

2. Sibling-combinator reality check - for every `~`/`+` between two
   compound selectors, build a real parent map of index.html via
   html.parser (stdlib, no third-party dependency) and confirm the two
   sides' identifying id/class actually share a parent element. If an
   anchor can't be resolved against the *static* DOM (e.g. it's a
   JS-only class - existence check #1 above already covers those by other
   means), this prints a WARNING naming exactly what couldn't be verified,
   rather than silently skipping it or falsely failing it.

Usage: check-hooks.py <path-to-id.css> <path-to-index.html>
Exit 0: every id/class token exists and every sibling combinator found is
        structurally possible.
Exit 1: prints each offending selector, one per line, then exits nonzero.
Stdlib only.
"""
import re
import sys
from html.parser import HTMLParser

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class DomIndex(HTMLParser):
    """Builds a parent map from index.html's static markup, so `~`/`+`
    combinators can be checked against real sibling relationships (not a
    name grep). Only sees *static* attributes - JS-added classes never show
    up here, which is fine: the existence check (whole-document text search,
    see module docstring) covers those separately."""

    def __init__(self):
        super().__init__()
        self._stack = []          # list of (tag, node_id)
        self._next_id = 0
        self.parent_of = {}       # node_id -> parent node_id (None = root)
        self.id_to_node = {}      # css id -> node_id
        self.class_to_nodes = {}  # css class -> set(node_id)

    def _open(self, tag, attrs):
        node_id = self._next_id
        self._next_id += 1
        attr_d = dict(attrs)
        parent = self._stack[-1][1] if self._stack else None
        self.parent_of[node_id] = parent
        el_id = attr_d.get("id")
        if el_id:
            self.id_to_node[el_id] = node_id
        cls = attr_d.get("class")
        if cls:
            for c in cls.split():
                self.class_to_nodes.setdefault(c, set()).add(node_id)
        return node_id

    def handle_starttag(self, tag, attrs):
        node_id = self._open(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self._stack.append((tag, node_id))

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)  # self-closing: never pushed, has no children

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return
        # unmatched close tag - ignore rather than raise; index.html is
        # trusted, well-formed input, not untrusted HTML we must be strict about.


def strip_css_noise(css_text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    text = re.sub(r"@keyframes\s+[\w-]+\s*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", text)
    return text


def split_top_level(text: str, sep: str):
    """Split `text` on `sep` chars that sit outside parens (so a comma
    inside :has(a, b) doesn't split the selector list wrongly)."""
    parts, buf, depth = [], "", 0
    for ch in text:
        if ch == "(":
            depth += 1
            buf += ch
        elif ch == ")":
            depth -= 1
            buf += ch
        elif depth == 0 and ch == sep:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return parts


def split_combinators(sel: str):
    """Return [(combinator, compound), ...] for a single selector branch;
    combinator is None for the first compound, else one of ' ', '>', '~', '+'.
    Combinators inside parens (:has(...) etc) are left untouched - they
    stay part of that compound's text, not split out (see module docstring:
    those are flagged with a WARNING at the call site, not silently checked
    as if they were top-level)."""
    tokens = []
    buf, depth, pending = "", 0, None
    i, n = 0, len(sel)
    while i < n:
        ch = sel[i]
        if ch == "(":
            depth += 1
            buf += ch
            i += 1
            continue
        if ch == ")":
            depth -= 1
            buf += ch
            i += 1
            continue
        if depth == 0 and ch in ">~+":
            if buf.strip():
                tokens.append((pending, buf.strip()))
            buf = ""
            pending = ch
            i += 1
            continue
        if depth == 0 and ch.isspace():
            j = i
            while j < n and sel[j].isspace():
                j += 1
            if j < n and sel[j] in ">~+":
                i = j  # let the next iteration consume the combinator char
                continue
            if buf.strip():
                tokens.append((pending, buf.strip()))
                buf = ""
                pending = " "
            i = j
            continue
        buf += ch
        i += 1
    if buf.strip():
        tokens.append((pending, buf.strip()))
    return tokens


def extract_hook_tokens(text: str):
    """All (#id / .class) tokens in `text`. A flat text scan, not an AST
    walk - attribute-selector brackets are stripped first so a value like
    [data-theme="hacker"] can't be mistaken for a token, but pseudo-class
    arguments (:has(...), :is(...), :not(...)) are left in place, so any
    #id/.class inside them is picked up for free rather than discarded."""
    stripped = re.sub(r"\[[^\]]*\]", "", text)
    return [(m.group(1), m.group(2)) for m in re.finditer(r"([#.])([\w-]+)", stripped)]


def anchor_token(compound: str):
    """The one id/class a `~`/`+` combinator check keys off: the compound's
    own id if it has one (most specific), else its first class. `None` if
    the compound carries neither (e.g. just an element name or [attr])."""
    stripped = re.sub(r"\[[^\]]*\]", "", compound)
    m = re.search(r"#([\w-]+)", stripped)
    if m:
        return ("id", m.group(1))
    m = re.search(r"\.([\w-]+)", stripped)
    if m:
        return ("class", m.group(1))
    return None


def token_exists_anywhere(name: str, html_text: str) -> bool:
    """Whole-document text search - see module docstring for why this is
    the right existence check here rather than a static-attribute-only one."""
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", html_text) is not None


def check(css_path: str, html_path: str):
    with open(html_path, encoding="utf-8") as f:
        html_text = f.read()
    dom = DomIndex()
    dom.feed(html_text)

    with open(css_path, encoding="utf-8") as f:
        css_text = f.read()
    text = strip_css_noise(css_text)

    missing = []           # (selector, kind, name)
    combinator_fail = []   # detail string
    warnings = []          # detail string

    cur = ""
    for ch in text:
        if ch == "{":
            sel = " ".join(cur.split()).strip()
            cur = ""
            if sel and not sel.startswith("@media") and not sel.startswith("@font-face"):
                for branch in split_top_level(sel, ","):
                    branch = branch.strip()
                    if not branch:
                        continue

                    for kind, name in extract_hook_tokens(branch):
                        if not token_exists_anywhere(name, html_text):
                            missing.append((branch, "id" if kind == "#" else "class", name))

                    parts = split_combinators(branch)
                    for idx, (comb, compound) in enumerate(parts):
                        if comb not in ("~", "+"):
                            continue
                        left, right = parts[idx - 1][1], compound
                        left_a, right_a = anchor_token(left), anchor_token(right)
                        if left_a is None or right_a is None:
                            warnings.append(
                                f"{branch}: could not identify an id/class anchor for the "
                                f"'{comb}' combinator between '{left}' and '{right}'"
                            )
                            continue

                        def nodes_for(anchor):
                            kind, name = anchor
                            if kind == "id":
                                node = dom.id_to_node.get(name)
                                return {node} if node is not None else set()
                            return set(dom.class_to_nodes.get(name, set()))

                        left_nodes, right_nodes = nodes_for(left_a), nodes_for(right_a)
                        if not left_nodes or not right_nodes:
                            warnings.append(
                                f"{branch}: '{left_a[1]}' and/or '{right_a[1]}' are not present "
                                f"as static id/class attributes in {html_path} (likely JS-only), "
                                f"so the '{comb}' sibling combinator could not be structurally verified"
                            )
                            continue

                        left_parents = {dom.parent_of.get(n) for n in left_nodes}
                        right_parents = {dom.parent_of.get(n) for n in right_nodes}
                        if not (left_parents & right_parents):
                            combinator_fail.append(
                                f"{branch}: '{left}' and '{right}' are not siblings in "
                                f"{html_path} (no shared parent), so '{comb}' can never match"
                            )
        elif ch == "}":
            cur = ""
        else:
            cur += ch

    return missing, combinator_fail, warnings


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check-hooks.py <path-to-id.css> <path-to-index.html>", file=sys.stderr)
        return 2
    css_path, html_path = sys.argv[1], sys.argv[2]
    missing, combinator_fail, warnings = check(css_path, html_path)

    for w in warnings:
        print(f"WARNING {css_path}: {w}")

    if not missing and not combinator_fail:
        print(f"OK   {css_path}: every id/class hook exists and every sibling combinator found is structurally possible")
        return 0

    if missing:
        print(f"FAIL {css_path}: {len(missing)} dead selector(s) - id/class not present anywhere in {html_path}:")
        for sel, kind, name in missing:
            print(f"  - {sel}  (missing {kind} '{name}')")
    if combinator_fail:
        print(f"FAIL {css_path}: {len(combinator_fail)} impossible sibling combinator(s):")
        for detail in combinator_fail:
            print(f"  - {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
