"""Import Sequencer sequences from GitHub, a URL or pasted text (#294).

An import only fetches and checks a sequence; it never runs it. The caller
shows the preview and saves it under a name the user picks. Running it later
goes through the Sequencer's normal Execute, with its confirmations.

The checks are structural, matching what the library's sequencer accepts: a
list of step objects, or ``{"steps": [...], "cleanup": [...]}``. Point names
and values are not checked here; the aGate's own model decides those at run
time. Keys the library ignores are allowed.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPO = "david2069/franklinwh-modbus-bridge"
SAMPLES_PATH = "src/franklinwh_bridge/sequences"
MAX_BYTES = 256 * 1024
TIMEOUT_S = 10.0

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_BLOB_RE = re.compile(r"^/([^/]+)/([^/]+)/blob/(.+)$")


class SequenceImportError(Exception):
    """A sequence that can't be imported; the message is shown to the user."""


def valid_name(name: str) -> bool:
    """Letters, digits, ``_`` and ``-`` only, so a name stays in its folder."""
    return bool(_NAME_RE.match(name or ""))


def git_blob_sha(data: bytes) -> str:
    """The SHA GitHub's contents API gives a file, to compare with a local copy."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def raw_url(url: str) -> str:
    """https only; a github.com page link becomes its raw-file link."""
    u = urlparse(url.strip())
    if u.scheme != "https" or not u.netloc:
        raise SequenceImportError("Only https:// links can be imported")
    if u.netloc == "github.com":
        m = _BLOB_RE.match(u.path)
        if m:
            owner, repo, rest = m.groups()
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return u.geturl()


def _step_problems(steps: Any, where: str) -> list[str]:
    if not isinstance(steps, list):
        return [f"{where} must be a list of steps"]
    problems: list[str] = []
    for i, st in enumerate(steps, 1):
        label = f"{where} {i}"
        if not isinstance(st, dict):
            problems.append(f"{label} is not an object")
            continue
        if "writes" in st and not isinstance(st["writes"], dict):
            problems.append(f'{label}: "writes" must be an object')
        if "wait_for" in st and not isinstance(st["wait_for"], dict):
            problems.append(f'{label}: "wait_for" must be an object')
        if "reads" in st and not isinstance(st["reads"], list):
            problems.append(f'{label}: "reads" must be a list')
        for key in ("sleep_ms", "post_sleep_ms", "verify_timeout_ms"):
            v = st.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                problems.append(f'{label}: "{key}" must be a number')
    return problems


def check(text: str) -> dict[str, Any]:
    """Parse and check a sequence; return the preview the dialog shows."""
    if len(text.encode()) > MAX_BYTES:
        raise SequenceImportError(f"Larger than {MAX_BYTES // 1024} KB")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SequenceImportError(f"Invalid JSON: {e}") from e

    if isinstance(data, dict) and "steps" in data:
        steps, cleanup = data.get("steps"), data.get("cleanup", [])
        problems = _step_problems(steps, "Step") + _step_problems(cleanup, "Cleanup step")
    elif isinstance(data, list):
        steps, cleanup = data, []
        problems = _step_problems(steps, "Step")
    else:
        raise SequenceImportError('Expected a list of steps, or {"steps": [...]}')
    if problems:
        raise SequenceImportError("; ".join(problems[:5]))
    if not steps:
        raise SequenceImportError("The sequence has no steps")

    preview = []
    writes: list[str] = []
    for kind, group in (("step", steps), ("cleanup", cleanup)):
        for i, st in enumerate(group, 1):
            w = [f"{k}={v}" for k, v in (st.get("writes") or {}).items()]
            writes += w
            preview.append({
                "kind": kind,
                "name": str(st.get("name", st.get("step", f"Step {i}"))),
                "writes": w,
                "reads": len(st.get("reads") or []),
                "wait": bool(st.get("wait_for") or st.get("sleep_ms") or st.get("post_sleep_ms")),
                "note": str(st.get("note", "")),
            })
    return {"content": text, "steps": preview, "writes": writes}


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> str:
    """Download a sequence (size-capped). Raises :class:`SequenceImportError`."""
    target = raw_url(url)
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
    try:
        async with client.stream("GET", target) as r:
            if str(r.url).split(":", 1)[0] != "https":
                raise SequenceImportError("The link redirected away from https")
            if r.status_code != 200:
                raise SequenceImportError(f"The server answered {r.status_code}")
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise SequenceImportError(f"Larger than {MAX_BYTES // 1024} KB")
    except httpx.HTTPError as e:
        raise SequenceImportError(f"Couldn't fetch the link: {e or type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()
    try:
        return body.decode()
    except UnicodeDecodeError as e:
        raise SequenceImportError("Not a text file") from e


def latest_tag(names: list[str]) -> str:
    """The highest ``vX.Y.Z`` tag (the API's order isn't by version)."""
    vers = [(tuple(map(int, m.groups())), n) for n in names
            if (m := _TAG_RE.match(n))]
    return max(vers)[1] if vers else ""


async def list_samples(
    local_dir: Path, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """The latest version's sample sequences, each marked new, changed or same."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
    headers = {"Accept": "application/vnd.github+json"}
    try:
        # Versions are released as tags; GitHub "releases" stop at v0.2.14.
        rel = await client.get(
            f"https://api.github.com/repos/{REPO}/tags", params={"per_page": 100},
            headers=headers)
        if rel.status_code != 200:
            raise SequenceImportError(f"GitHub answered {rel.status_code} for the versions")
        tag = latest_tag([t.get("name", "") for t in rel.json()])
        if not tag:
            raise SequenceImportError("No released version found on GitHub")
        r = await client.get(
            f"https://api.github.com/repos/{REPO}/contents/{SAMPLES_PATH}",
            params={"ref": tag}, headers=headers)
        if r.status_code != 200:
            raise SequenceImportError(f"GitHub answered {r.status_code} for the samples folder")
        items = r.json()
    except httpx.HTTPError as e:
        raise SequenceImportError(f"Couldn't reach GitHub: {e or type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()

    samples = []
    for it in items if isinstance(items, list) else []:
        name = str(it.get("name", ""))
        if it.get("type") != "file" or not name.endswith(".json"):
            continue
        stem = name[:-5]
        local = local_dir / name
        if not local.exists():
            status = "new"
        elif git_blob_sha(local.read_bytes()) == it.get("sha"):
            status = "same"
        else:
            status = "changed"
        samples.append({"name": stem, "url": it.get("download_url"), "status": status})
    return {"tag": tag, "samples": samples}
