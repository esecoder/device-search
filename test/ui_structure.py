"""Structural check of the UI: every element should have a reason to exist.

Not a visual review - it cannot see. It answers the mechanical half of "is
anything out of place": ids with no styling, classes nothing uses, and handlers
that point at elements that are not there. The visual half needs a human, and the
script says so rather than implying otherwise.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
css = (ROOT / "ui" / "style.css").read_text(encoding="utf-8")
# EVERY script that touches the DOM, not just the two obvious ones. The first
# version read app.js and setup.js, so classes defined in ai.js were reported as
# dead - a false positive that would train anyone reading this to ignore it.
js = "".join(
    (ROOT / "ui" / name).read_text(encoding="utf-8")
    for name in ("app.js", "setup.js", "ai.js")
)


def main() -> int:
    ids = re.findall(r'id="([^"]+)"', html)
    print(f" {len(ids)} ids in index.html")

    # An id can be styled by its ELEMENT as easily as by its id - `esc` is covered
    # by the generic `kbd` rule. Looking only for "#id" reported it as unstyled.
    def styled(ident: str) -> bool:
        if f"#{ident}" in css:
            return True
        tag = re.search(rf'<([a-zA-Z][\w-]*)[^>]*id="{ident}"', html)
        if not tag:
            return False
        # Crude but honest: does a rule target this tag at all?
        return bool(re.search(rf'(^|\n)\s*{tag.group(1)}\b[^{{]*\{{', css))

    missing_css = [i for i in ids if not styled(i)]
    missing_js = [i for i in ids if f'"{i}"' not in js]
    print(f" ids with no CSS rule : {missing_css or 'none'}")
    # Not a problem: an id styled only in CSS (`drag`, `box`, `foot`) is meant to
    # be static. Listed so the output is complete, not because it is wrong.
    print(f" ids not used in JS (style-only): {missing_js or 'none'}")

    # Classes defined in CSS that nothing renders.
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)\s*(?=[,{:])", css))
    used_raw = set(re.findall(r'class="([^"]+)"', html))
    used_raw |= set(re.findall(r'className = "([^"]+)"', js))
    used = set()
    for chunk in used_raw:
        used |= set(chunk.split())
    # Classes built at runtime by concatenation appear in the JS as words.
    orphan = sorted(c for c in defined
                    if c not in used and c not in js)

    print(f" CSS classes never used : {orphan[:15] or 'none'}")

    # Handlers pointing at ids that do not exist.
    handler_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
    ghosts = sorted(h for h in handler_ids if h not in ids and h not in
                    {"busy-overlay", "setup-msg", "code-cost", "code-actions",
                     "media-cost", "media-actions", "setup"})
    print(f" JS looks for ids that do not exist: {ghosts or 'none'}")

    # Only these two are failures. "Style-only id" is normal, and the class check
    # already excludes anything mentioned in a script.
    # UNDEFINED CSS VARIABLES.
    #
    # `var(--muted)` where the palette defines `--dim` is not an error: CSS silently
    # falls back to an inherited colour, so the rule works and looks wrong. Six of
    # these shipped before anyone noticed, because nothing fails - the text is just
    # the wrong colour, which reads as a design choice rather than a bug.
    defined = set(re.findall(r"^\s+(--[a-z0-9-]+)\s*:", css, re.M))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    undefined = sorted(used - defined)
    if undefined:
        print()
        for name in undefined:
            print(f"  undefined  {name} is used but never declared in :root")

    # FUNCTIONS CALLED BUT NEVER DEFINED.
    #
    # `openPath(...)` was called from two places and defined nowhere. Every click
    # threw ReferenceError, nothing opened, and nothing said why - the row looked
    # clickable and did nothing. This file already checks ids that do not exist;
    # it did not check names that do not exist, which is the same mistake one level
    # up.
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof", "function",
        "console", "JSON", "Object", "Array", "String", "Number", "Boolean", "Math",
        "Date", "Promise", "Set", "Map", "Error", "RegExp", "parseInt", "parseFloat",
        "setTimeout", "clearTimeout", "setInterval", "clearInterval", "fetch",
        "encodeURIComponent", "decodeURIComponent", "requestAnimationFrame",
        "isNaN", "alert", "confirm", "await", "async", "new", "of", "in", "do",
        "else", "try", "finally", "throw", "yield", "delete", "void", "instanceof",
    }
    # STRIP COMMENTS AND STRING LITERALS FIRST. Prose contains things that look
    # like calls - "a snippet() is..." in a comment - and every one of those was a
    # false positive that made the check useless.
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    code = re.sub(r"`(?:[^`\\]|\\.)*`", '""', code, flags=re.S)
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)
    code = re.sub(r"'(?:[^'\\]|\\.)*'", '""', code)

    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", code))
    calls = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code))
    unknown_calls = sorted(c for c in calls
                           if c not in defined and c not in builtins
                           and not c[0].isupper())
    # ADVISORY, NOT A FAILURE. The regex cannot tell a function defined in another
    # file, a local named binding, or a method shorthand from a genuine missing
    # function, and a check that cries wolf is one people learn to scroll past.
    # It exists to make `openPath` visible next time someone looks, not to gate.
    if unknown_calls:
        print(f" names called but not defined here : {unknown_calls[:6]}")

    problems = bool(missing_css or ghosts or orphan or undefined)
    print()
    if problems:
        print(" REVIEW something above needs a reason or a removal")
    else:
        print(" PASS no orphaned ids, classes or handlers")
    print(" NOTE this cannot judge layout, spacing or colour. That needs eyes.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
