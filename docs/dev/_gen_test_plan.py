"""Build the Scheduler test plan from the behaviour markers in the user docs.

Every documented scheduler behaviour carries an HTML comment next to its text:

    <!-- @b TRG-01 ;; src: gateway/scheduler.py:120-126 ;; tested: tests/unit/test_x.py::test_y
         ;; write: no ;; b: what the code does ;; test: the test to add -->

Fields are separated by ``;;``. ``src`` is relative to ``src/franklinwh_bridge``.
The status is one of ``tested: <test>``, ``untested`` or ``unclear``. ``write``
says whether the behaviour can write to a battery (a force, release, mode or
reserve command reaching a gateway).

Run from the repository root:

    python docs/dev/_gen_test_plan.py

It rewrites the table between ``<!-- plan:begin -->`` and ``<!-- plan:end -->``
in ``docs/dev/scheduler-test-plan.md`` and prints the counts. ``--check`` exits
non-zero when the file is out of date or a marker is malformed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES = sorted((ROOT / "docs" / "user" / "scheduler").glob("*.md"))
PLAN = ROOT / "docs" / "dev" / "scheduler-test-plan.md"
MARKER = re.compile(r"<!--\s*@b\s+([A-Z]+-\d+)\s*;;(.*?)-->", re.S)
STATUS_ORDER = {"unclear": 0, "untested": 1, "tested": 2}


def parse() -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    seen: dict[str, str] = {}
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for m in MARKER.finditer(text):
            bid, body = m.group(1), " ".join(m.group(2).split())
            fields: dict[str, str] = {}
            status, test_ref = "", ""
            for part in (p.strip() for p in body.split(";;")):
                if not part:
                    continue
                if part in ("untested", "unclear"):
                    status = part
                    continue
                key, _, val = part.partition(":")
                key, val = key.strip(), val.strip()
                if key == "tested":
                    status, test_ref = "tested", val
                else:
                    fields[key] = val
            where = f"{page.name}: {bid}"
            if bid in seen:
                errors.append(f"{where} duplicates {seen[bid]}")
            seen[bid] = page.name
            for need in ("src", "write", "b"):
                if not fields.get(need):
                    errors.append(f"{where} has no {need}")
            if status not in STATUS_ORDER:
                errors.append(f"{where} has no tested/untested/unclear marker")
            if fields.get("write") not in ("yes", "no"):
                errors.append(f"{where}: write must be yes or no")
            rows.append({
                "id": bid, "page": page.name, "src": fields.get("src", ""),
                "status": status or "untested", "test_ref": test_ref,
                "write": fields.get("write", "no"), "b": fields.get("b", ""),
                "test": fields.get("test", ""),
            })
    return rows, errors


def risk(row: dict) -> tuple[str, int]:
    write = row["write"] == "yes"
    s = STATUS_ORDER[row["status"]]
    if write and s < 2:
        return "high", 0
    if write or s < 2:
        return "medium", 1
    return "low", 2


def cell(text: str) -> str:
    return text.replace("|", "\\|")


def render(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: (risk(r)[1], STATUS_ORDER[r["status"]], r["id"]))
    n = len(rows)
    count = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_ORDER}
    by_risk = {k: sum(1 for r in rows if risk(r)[0] == k) for k in ("high", "medium", "low")}
    out = [
        f"**{n} behaviours**: {count['tested']} tested, {count['untested']} untested, "
        f"{count['unclear']} unclear. Risk: {by_risk['high']} high, {by_risk['medium']} medium, "
        f"{by_risk['low']} low.",
        "",
        "| # | Risk | Behaviour | Page | Code | Current test | Proposed test | Battery write |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        current = f"`{r['test_ref']}`" if r["status"] == "tested" else f"**{r['status']}**"
        page = f"[{r['page'][:-3]}](../user/scheduler/{r['page']})"
        src = ", ".join(f"`{s.strip()}`" for s in r["src"].split(",") if s.strip())
        out.append(
            f"| {r['id']} | {risk(r)[0]} | {cell(r['b'])} | {page} | {src} | {cell(current)} "
            f"| {cell(r['test'] or '—')} | {r['write']} |"
        )
    return "\n".join(out)


def main() -> int:
    rows, errors = parse()
    for e in errors:
        print("marker:", e, file=sys.stderr)
    text = PLAN.read_text(encoding="utf-8")
    begin, end = "<!-- plan:begin -->", "<!-- plan:end -->"
    head, _, rest = text.partition(begin)
    _, _, tail = rest.partition(end)
    new = f"{head}{begin}\n\n{render(rows)}\n\n{end}{tail}"
    count = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_ORDER}
    print(f"{len(rows)} behaviours: {count}")
    if "--check" in sys.argv:
        return 1 if (errors or new != text) else 0
    PLAN.write_text(new, encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
