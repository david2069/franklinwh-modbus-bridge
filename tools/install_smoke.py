#!/usr/bin/env python3
"""Install smoke check for a standalone bridge: Docker Compose or a plain pip install.

Run against a bridge that has just been installed and started, with or without an
MQTT broker. It signs in, adds a demo gateway that publishes to Home Assistant,
and checks:

- the bridge answers ``/api/health`` with the expected version and environment;
- the demo gateway polls;
- with ``--mqtt HOST:PORT``: the broker is the configured one and connected, and
  every discovery topic the bridge lists for the demo is retained on the broker;
  without it: the bridge still runs. With no broker set it tries the default,
  localhost:1883 (``config/mqtt_broker.py``), so it reports that broker and logs
  the refused connection; the check prints those lines as NOTE;
- with ``--log FILE``: the bridge's log has no traceback and no ERROR line.

The API answers are saved to ``--out``. The generated admin password, when read
from the log, is never written out.

Exit status 0 when every check passes, 1 otherwise. Uses only the standard
library, plus ``aiomqtt`` for ``--mqtt`` (a dependency of the bridge).

    python tools/install_smoke.py --url http://127.0.0.1:8100 --version 0.2.49 \\
        --env docker --password-from-log app.log --mqtt 127.0.0.1:1886 \\
        --log app.log --out smoke-out
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GATEWAY = "smoke"
_PASSWORD_LINE = re.compile(r"GENERATED password: (\S+)")


class Bridge:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method,
                                     headers={"content-type": "application/json"})
        try:
            with self.opener.open(req, timeout=15) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode(errors="replace")


class Checks:
    def __init__(self, out: Path) -> None:
        self.out, self.failed = out, []

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail != "" else ""))
        if not ok:
            self.failed.append(name)
        return ok

    def save(self, name: str, answer: object) -> None:
        (self.out / f"{name}.json").write_text(json.dumps(answer, indent=1, default=str))


def wait_for(fn, timeout: float, interval: float = 2.0):
    """Call fn until it returns something truthy or the time runs out; return its last answer."""
    end = time.monotonic() + timeout
    while True:
        answer = fn()
        if answer or time.monotonic() > end:
            return answer
        time.sleep(interval)


def read_password(args) -> str:
    if args.password:
        return args.password
    found = _PASSWORD_LINE.findall(Path(args.password_from_log).read_text(errors="replace"))
    if not found:
        sys.exit(f"no generated admin password in {args.password_from_log}")
    return found[-1]


async def retained_topics(host: str, port: int, wanted: set[str], timeout: float) -> set[str]:
    """The topics in `wanted` that the broker holds retained."""
    import aiomqtt

    seen: set[str] = set()
    async with aiomqtt.Client(host, port, identifier=f"install-smoke-{int(time.time())}") as client:
        for topic in wanted:
            await client.subscribe(topic)
        try:
            async with asyncio.timeout(timeout):
                async for msg in client.messages:
                    if msg.retain:
                        seen.add(str(msg.topic))
                    if seen >= wanted:
                        break
        except TimeoutError:
            pass
    return seen


_NO_BROKER = re.compile(r"MQTT error: .*(Connection refused|Name or service not known)")


def log_problems(text: str, no_broker: bool) -> tuple[list[str], list[str]]:
    """(problems, notes): traceback and ERROR lines; with no broker, its refused
    connection is a note, not a problem."""
    problems, notes = [], []
    for line in text.splitlines():
        if "Traceback" in line or re.search(r"\bERROR\b", line):
            (notes if no_broker and _NO_BROKER.search(line) else problems).append(line)
    return problems, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--version", required=True, help="the version the bridge must report")
    ap.add_argument("--env", required=True, choices=["docker", "dev"],
                    help="the environment the bridge must report")
    ap.add_argument("--user", default="admin")
    pw = ap.add_mutually_exclusive_group(required=True)
    pw.add_argument("--password")
    pw.add_argument("--password-from-log", metavar="FILE",
                    help="read the generated admin password from the bridge's log")
    ap.add_argument("--mqtt", metavar="HOST:PORT",
                    help="the broker the bridge must publish to; omit for a bridge with none")
    ap.add_argument("--log", metavar="FILE", help="the bridge's log, read after the checks")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    c, b = Checks(args.out), Bridge(args.url)

    def healthy():
        try:
            status, body = b.call("GET", "/api/health")
        except OSError:
            return None
        return body if status == 200 else None

    health = wait_for(healthy, args.timeout)
    c.save("api_health", health)
    if not c.check("bridge answers /api/health", bool(health)):
        return 1
    c.check("reports the version under test", health.get("version") == args.version,
            health.get("version"))
    c.check(f"reports environment {args.env}", health.get("environment") == args.env,
            health.get("environment"))

    status, body = b.call("POST", "/api/auth/login",
                          {"username": args.user, "password": read_password(args)})
    if not c.check("admin signs in", status == 200, status):
        return 1

    status, body = b.call("POST", "/api/gateways", {
        "gateway_id": GATEWAY, "name": "Smoke demo", "mock": True, "publish_to_ha": True})
    c.save("api_gateways_post", body)
    c.check("demo gateway added", status == 201, status)

    def polled():
        status, body = b.call("GET", f"/api/status?gateway={GATEWAY}")
        poller = (body.get("components") or {}).get("poller") or {} if status == 200 else {}
        return body if poller.get("status") == "running" and poller.get("last_poll_ts") else None

    st = wait_for(polled, 60)
    c.save("api_status_smoke", st or b.call("GET", f"/api/status?gateway={GATEWAY}")[1])
    c.check("demo gateway polls", bool(st))

    broker = (healthy() or {}).get("mqtt_broker") or {}
    c.save("api_health_after", healthy())
    if args.mqtt:
        host, _, port = args.mqtt.rpartition(":")
        c.check("broker is the configured one", broker.get("source") == "configured", broker)

        def listed():
            status, body = b.call("GET", f"/api/mqtt/topics?gateway={GATEWAY}")
            return body if status == 200 and body.get("topics") else None

        topics = wait_for(listed, 60)
        c.save("api_mqtt_topics_smoke", topics)
        if c.check("bridge lists the demo's topics", bool(topics)):
            wanted = {t["discovery_topic"] for t in topics["topics"]}
            try:
                seen = asyncio.run(retained_topics(host, int(port), wanted, 30))
            except Exception as exc:
                c.check(f"checker reaches the broker at {args.mqtt}", False, exc)
                seen = set()
            missing = sorted(wanted - seen)
            c.save("broker_missing_discovery_topics", missing)
            c.check(f"all {len(wanted)} discovery topics retained on the broker",
                    not missing, f"{len(missing)} missing, e.g. {missing[:3]}" if missing else "")
    else:
        print(f"NOTE no broker set; the bridge reports {broker}")

    if args.log:
        problems, notes = log_problems(Path(args.log).read_text(errors="replace"),
                                       no_broker=not args.mqtt)
        (args.out / "log_problems.txt").write_text("\n".join(problems))
        for line in notes:
            print("NOTE", line[:200])
        c.check("log has no traceback or ERROR line", not problems,
                f"{len(problems)}, first: {problems[0][:200]}" if problems else "")

    print("RESULT:", "PASS" if not c.failed else f"FAIL {len(c.failed)}")
    return 0 if not c.failed else 1


if __name__ == "__main__":
    sys.exit(main())
