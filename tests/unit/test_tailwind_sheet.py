"""The compiled stylesheet has every class the pages use (#513).

tailwind.css is built from the templates and JS by tools/build_css.py and
committed. If a page gains a class and the sheet isn't rebuilt, the class
silently does nothing: the {x} Insert a value dialog lost its height limit
that way and couldn't be closed on a wide screen. This needs no node: it
checks each class in a template's class="…" against the committed sheets and
the templates' own <style> blocks.
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "franklinwh_bridge"
TEMPLATES = sorted((PKG / "templates").rglob("*.html"))

# Classes Tailwind 3 can't generate, so they can't be in the sheet.
# border-current/40: no opacity modifier on currentColor (index.html).
CANNOT_BUILD = {"border-current/40"}


def _css() -> str:
    text = (PKG / "static/css/tailwind.css").read_text()
    text += (PKG / "static/css/design-system.css").read_text()
    for f in TEMPLATES:
        text += "".join(re.findall(r"<style[^>]*>(.*?)</style>", f.read_text(), re.S))
    return text


def _selector(cls: str) -> str:
    """The class as Tailwind writes it in a selector."""
    return re.sub(r"([:/\[\]\.%!#()&>+*='\"])", r"\\\1", cls).replace(",", "\\2c ")


def test_every_template_class_is_in_the_stylesheet():
    css = _css()
    missing = {}
    for f in TEMPLATES:
        for m in re.finditer(r'\sclass="([^"]*)"', f.read_text()):
            for cls in m.group(1).split():
                if "{" in cls or cls in CANNOT_BUILD:
                    continue
                if not re.search(r"\." + re.escape(_selector(cls)) + r"(?![\w-])", css):
                    missing.setdefault(cls, f.name)
    assert not missing, (
        f"classes not in the stylesheet — run `python tools/build_css.py`: {missing}")


def test_insert_a_value_dialog_keeps_its_size_limits():
    css = _css()
    for cls in ("sm:max-h-[80dvh]", "sm:max-w-md", "sm:p-4"):
        assert "." + _selector(cls) in css, cls
