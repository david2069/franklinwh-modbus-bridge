#!/usr/bin/env python3
"""Generate the REST API reference from the bridge's own OpenAPI schema.

Imports the FastAPI app in-process (nothing is started, no gateway or broker is
contacted), calls ``app.openapi()`` and writes:

- ``docs/dev/openapi.json``    the schema, as served at ``/openapi.json``
- ``docs/dev/api-reference.md`` a Markdown page, one section per tag

The access column is read from each route's dependencies (session, admin
writes, the ``control`` capability, an admin-only role, a feature module), so
it follows the code. Run it after any change to a route:

    python docs/dev/_gen_api_reference.py

No extra dependency: it needs only the bridge's own requirements.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))
# Importing the app builds its session middleware, which would otherwise read or
# create ./data/.session_secret. A throwaway key keeps the import side-effect free.
os.environ.setdefault("SECURITY_SECRET_KEY", "docs-generator-not-a-secret")

from fastapi.routing import APIRoute  # noqa: E402

from franklinwh_bridge.main import app  # noqa: E402

TAG_TITLES = {
    "health": "Health and status",
    "auth": "Authentication and sessions",
    "admin": "Admin: points, metrics, logs, sequences, backup",
    "mqtt": "MQTT publishing",
    "groups": "Publishing groups",
    "gateways": "Site, services and gateways",
    "setup": "Setup wizard",
    "modules": "Feature modules",
    "energy": "Energy",
    "point-history": "Point history",
    "solar": "Solar forecast and location",
    "tariff": "Tariffs and prices",
    "disclaimer": "Legal notice",
    "users": "Users",
    "support": "Support",
    "schedules": "Schedules",
    "scheduler": "Scheduler sensors and conditions",
    "home-assistant": "Home Assistant instances and entities",
    "ui": "Web UI and legacy command",
}

READ = {"GET", "HEAD", "OPTIONS"}


def _closure(fn) -> dict:
    names = getattr(getattr(fn, "__code__", None), "co_freevars", ())
    cells = getattr(fn, "__closure__", None) or ()
    return {n: c.cell_contents for n, c in zip(names, cells, strict=False)}


def _api_routes(routes) -> list:
    """Every API route with its effective (include-time) dependencies.

    Older FastAPI copies each included route into ``app.routes`` as an
    ``APIRoute``. Newer FastAPI keeps an included-router node whose
    ``effective_candidates()`` carry the merged path and dependencies.
    """
    out = []
    for r in routes:
        if hasattr(r, "effective_candidates"):
            out += _api_routes(r.effective_candidates())
        elif isinstance(r, APIRoute) or getattr(r, "dependant", None) is not None:
            out.append(r)
    return out


def _gates(route) -> dict:
    """Walk a route's dependency tree and name the access checks in it."""
    found: dict = {"auth": False, "admin_writes": False, "roles": set(), "caps": set(),
                   "module": None}
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        q = getattr(dep.call, "__qualname__", "")
        if q == "require_auth":
            found["auth"] = True
        elif q == "require_admin_for_writes":
            found["admin_writes"] = True
        elif q.startswith("require_role."):
            found["roles"] |= set(_closure(dep.call).get("roles", ()))
        elif q.startswith("require_capability."):
            found["caps"].add(_closure(dep.call).get("capability"))
        elif q.startswith("require_module."):
            found["module"] = _closure(dep.call).get("module_id")
        stack.extend(dep.dependencies)
    return found


def _access(method: str, g: dict) -> str:
    if g["roles"]:
        who = " or ".join(sorted(g["roles"]))
        base = f"{who} role"
    elif g["caps"]:
        base = " + ".join(f"`{c}` capability" for c in sorted(g["caps"]))
    elif g["admin_writes"] and method not in READ:
        base = "admin role"
    elif g["auth"] or g["admin_writes"]:
        base = "any signed-in role"
    else:
        base = "open"
    if g["module"]:
        base += f"; module `{g['module']}`"
    return base


def _ref_name(schema: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "items" in schema:
        return f"list of {_ref_name(schema['items'])}"
    if "anyOf" in schema:
        return " or ".join(_ref_name(s) for s in schema["anyOf"])
    return schema.get("type", "object")


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _anchor(method: str, path: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in f"{method}-{path}".lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def main() -> None:
    schema = app.openapi()
    (HERE / "openapi.json").write_text(json.dumps(schema, indent=2) + "\n")

    access: dict[tuple[str, str], str] = {}
    for r in _api_routes(app.routes):
        g = _gates(r)
        for m in r.methods:
            access[(m, r.path)] = _access(m, g)

    by_tag: dict[str, list] = {}
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            tag = (op.get("tags") or ["other"])[0]
            by_tag.setdefault(tag, []).append((method.upper(), path, op))

    comps = schema.get("components", {}).get("schemas", {})
    out: list[str] = [
        "# REST API reference",
        "",
        "<!-- Generated by docs/dev/_gen_api_reference.py from the app's OpenAPI",
        "     schema. Do not edit by hand: change the route, then regenerate. -->",
        "",
        f"Generated from the OpenAPI schema of **{schema['info']['title']} "
        f"{schema['info']['version']}**: {sum(len(v) for v in by_tag.values())} "
        f"operations in {len(by_tag)} groups. The raw schema is "
        "[openapi.json](openapi.json); a running bridge serves the same at "
        "`/openapi.json`, with interactive docs at `/docs` and `/redoc`.",
        "",
        "**Access** is what the route itself enforces (see "
        "[Authentication](rest-api.md#authentication)). Under Home Assistant ingress "
        "every request is the ingress admin. A route behind a feature module "
        "answers 403 while that module is off.",
        "",
        "Most routes return plain JSON objects without a declared response model, "
        "so the schema lists their response as untyped. Request bodies are typed.",
        "",
    ]
    order = [t for t in TAG_TITLES if t in by_tag] + sorted(set(by_tag) - set(TAG_TITLES))
    out += ["| Group | Operations |", "|---|---|"]
    for tag in order:
        title = TAG_TITLES.get(tag, tag)
        anchor = _anchor("", title)
        out.append(f"| [{title}](#{anchor}) | {len(by_tag[tag])} |")
    out.append("")

    for tag in order:
        title = TAG_TITLES.get(tag, tag)
        out += [f"## {title}", "", "| Method | Path | Summary | Access |", "|---|---|---|---|"]
        for method, path, op in by_tag[tag]:
            out.append(
                f"| `{method}` | [`{path}`](#{_anchor(method, path)}) | "
                f"{_cell(op.get('summary', ''))} | {access.get((method, path), '?')} |")
        out.append("")
        for method, path, op in by_tag[tag]:
            out += [f"### `{method} {path}` {{ #{_anchor(method, path)} }}", ""]
            desc = (op.get("description") or "").strip()
            if desc:
                out += [desc, ""]
            out += [f"Access: {access.get((method, path), '?')}.", ""]
            params = op.get("parameters") or []
            if params:
                out += ["| Parameter | In | Type | Required |", "|---|---|---|---|"]
                for p in params:
                    out.append(
                        f"| `{p['name']}` | {p['in']} | {_ref_name(p.get('schema', {}))} | "
                        f"{'yes' if p.get('required') else 'no'} |")
                out.append("")
            body = op.get("requestBody")
            if body:
                content = body.get("content", {})
                ctype, spec = next(iter(content.items()))
                sch = spec.get("schema", {})
                name = _ref_name(sch)
                out += [f"Request body (`{ctype}`): **{name}**", ""]
                model = comps.get(name, {})
                props = model.get("properties") or {}
                req = set(model.get("required") or [])
                if props:
                    out += ["| Field | Type | Required | Default |", "|---|---|---|---|"]
                    for fname, f in props.items():
                        default = f.get("default", "")
                        default = json.dumps(default) if default != "" else ""
                        out.append(
                            f"| `{fname}` | {_cell(_ref_name(f))} | "
                            f"{'yes' if fname in req else 'no'} | {_cell(default)} |")
                    out.append("")
        out.append("")

    (HERE / "api-reference.md").write_text("\n".join(out).rstrip() + "\n")
    print(f"wrote {HERE / 'openapi.json'} and {HERE / 'api-reference.md'}")


if __name__ == "__main__":
    main()
