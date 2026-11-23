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
    problems = bool(missing_css or ghosts or orphan)
    print()
    if problems:
        print(" REVIEW something above needs a reason or a removal")
    else:
        print(" PASS no orphaned ids, classes or handlers")
    print(" NOTE this cannot judge layout, spacing or colour. That needs eyes.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
