"""Compile the Tailwind stylesheet that replaces the in-browser JIT.

The page used to ship `tailwind-3.4.1.min.js` (403 KB) and re-scan the DOM
through a MutationObserver on every mutation. With all eight tabs permanently
mounted that scan is mostly wasted work, paid for the life of the page.

The version is pinned to the JIT build it replaces, and that is not cosmetic.
Tailwind 3.4 rewrote Preflight's form reset to

    button, input:where([type=button]), ...

where `:where()` contributes ZERO specificity, so `.btn-secondary` in
design-system.css wins. Earlier Preflight used a bare `[type=button]` at
specificity (0,1,0) — a tie with `.btn-secondary`, broken by source order,
which silently flattens every button to `background-color: initial`. Compiling
with the wrong version reintroduces that. `--check` asserts the form.

Usage:
    python tools/build_css.py            # compile
    python tools/build_css.py --check    # verify the committed sheet
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "franklinwh_bridge" / "static" / "css"
OUT = STATIC / "tailwind.css"
SRC = STATIC / "tailwind.src.css"

# Preflight must match static/js/tailwind-3.4.1.min.js, the JIT build this
# replaces — NOT that file's name. The vendored Play/CDN bundle is newer than
# its filename claims: npm tailwindcss@3.4.1 still emits the bare `[type=...]`
# reset, while the vendored JS emits the `:where()` form introduced in 3.4.4.
# Compiling the name rather than the behaviour is what flattened the buttons.
VERSION = "3.4.17"

# The 3.4 Preflight marker. Its absence means a wrong version resolved.
PREFLIGHT_MARKER = "input:where([type=button])"


def compile_css() -> None:
    """Run the Tailwind CLI in a container so the build needs no local node."""
    rel_src = SRC.relative_to(ROOT)
    rel_out = OUT.relative_to(ROOT)
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{ROOT}:/w", "-w", "/w",
        "node:20-alpine",
        "npx", "--yes", f"tailwindcss@{VERSION}",
        "-c", "tailwind.config.js",
        "-i", str(rel_src),
        "-o", str(rel_out),
        "--minify",
    ]
    subprocess.run(cmd, check=True)


def check() -> int:
    if not OUT.exists():
        print(f"missing {OUT}", file=sys.stderr)
        return 1

    css = OUT.read_text()
    size_kb = len(css.encode()) / 1024

    if PREFLIGHT_MARKER not in css:
        print(
            f"FAIL: {OUT.name} was not built with Tailwind {VERSION}.\n"
            f"      Preflight lacks `{PREFLIGHT_MARKER}`, so its form reset\n"
            f"      carries specificity (0,1,0) and ties with .btn-secondary —\n"
            f"      buttons lose their background. Rebuild with build_css.py.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {OUT.name} {size_kb:.0f} KB, Tailwind {VERSION} Preflight")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify, don't rebuild")
    args = ap.parse_args()

    if not args.check:
        compile_css()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
