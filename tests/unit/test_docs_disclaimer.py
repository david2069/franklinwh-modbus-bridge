"""The docs home page carries the full disclaimer, word for word (disclaimer.FULL)."""

import re
from pathlib import Path

from franklinwh_bridge import disclaimer

INDEX = Path(__file__).resolve().parents[2] / "docs" / "index.md"


def _words(text: str) -> str:
    text = re.sub(r"[*<>]", "", text)  # markdown bold and autolinks
    text = re.sub(r"^\s*-\s+", " ", text, flags=re.M)  # list markers
    return " ".join(text.replace("— PLEASE READ", "— please read").split()).lower()


def test_docs_index_has_the_full_disclaimer():
    page = _words(INDEX.read_text())
    for para in disclaimer.FULL.split("\n\n"):
        for line in para.splitlines():
            line = _words(line)
            if line:
                assert line in page, f"docs/index.md is missing: {line!r}"
