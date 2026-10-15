"""Failed-login throttling, per (username, client IP) (#99).

After ``MAX_FAILURES`` failed sign-ins within ``WINDOW_S``, every attempt for
that pair is refused for ``LOCKOUT_S`` without the password being checked, so
a lockout cannot be used to test passwords. Failures stay counted for the rest
of their window, so once a lockout ends each further failure inside the window
starts a new one: a guesser gets one try a minute, not five.

The key is the name as typed (lower-cased), never the account, so a name that
does not exist is throttled exactly like one that does: the response never says
which. A successful sign-in clears its pair.

State is in memory, per process: a restart clears every counter and lockout.
That is accepted for the interim (#99): the bridge is reached on a LAN or VPN,
and the audit trail (``login_failed`` / ``login_throttled``) survives restarts.
"""

from __future__ import annotations

import math
import time
from collections import deque

MAX_FAILURES = 5
WINDOW_S = 15 * 60.0
LOCKOUT_S = 60.0

#: Pairs tracked before stale ones are pruned, so a flood of made-up names
#: cannot grow the table without bound.
_MAX_KEYS = 10_000


def throttle_key(username: str, ip: str | None) -> tuple[str, str]:
    return ((username or "").strip().lower()[:64], ip or "")


class LoginThrottle:
    """Failure counts and lockouts. ``now`` is monotonic seconds (testable)."""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window_s: float = WINDOW_S,
        lockout_s: float = LOCKOUT_S,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._failures: dict[tuple[str, str], deque[float]] = {}
        self._locked_until: dict[tuple[str, str], float] = {}

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def retry_after(self, key: tuple[str, str], now: float | None = None) -> int | None:
        """Seconds until ``key`` may try again, or None if it may try now."""
        t = self._now(now)
        until = self._locked_until.get(key)
        if until is None:
            return None
        if t >= until:
            del self._locked_until[key]
            return None
        return max(1, math.ceil(until - t))

    def record_failure(self, key: tuple[str, str], now: float | None = None) -> bool:
        """Count a failure. True if it starts a lockout (audit it once, then)."""
        t = self._now(now)
        q = self._failures.setdefault(key, deque())
        q.append(t)
        while q and t - q[0] > self.window_s:
            q.popleft()
        if len(self._failures) > _MAX_KEYS:
            self._prune(t)
        if len(q) >= self.max_failures:
            self._locked_until[key] = t + self.lockout_s
            return True
        return False

    def record_success(self, key: tuple[str, str]) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)

    def _prune(self, t: float) -> None:
        for k in [k for k, q in self._failures.items() if not q or t - q[-1] > self.window_s]:
            del self._failures[k]
        for k in [k for k, until in self._locked_until.items() if t >= until]:
            del self._locked_until[k]
