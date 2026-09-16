"""The single request budget.

Every live request the application makes -- listing pages, detail pages,
resources, probes -- passes through one :class:`RequestBudget`. That is what
makes the pacing guarantee real rather than per-module wishful thinking.

The budget also owns the adaptive response to access-control challenges. The
Rowan career site sits behind AWS WAF, which answers with
``HTTP 202`` and ``x-amzn-waf-action: challenge`` when it decides a client is
moving too fast. Observed during the source audit on 2026-09-16: roughly seven
application requests inside 90 seconds tripped it, and it cleared after about
eleven minutes of quiet. So: pace conservatively, and when challenged, slow down
for the rest of the run rather than retrying at the same rate.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field


class BudgetExhausted(RuntimeError):
    """The configured per-run request ceiling was reached."""


class ChallengeWall(RuntimeError):
    """The source kept answering with an access-control challenge."""


@dataclass
class BudgetStats:
    requests: int = 0
    retries: int = 0
    challenges: int = 0
    consecutive_challenges: int = 0
    sleep_seconds: float = 0.0
    bytes_received: int = 0
    first_request_at: float | None = None
    last_request_at: float | None = None

    def as_dict(self) -> dict[str, float | int]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "challenges": self.challenges,
            "sleep_seconds": round(self.sleep_seconds, 2),
            "bytes_received": self.bytes_received,
            "wall_seconds": round((self.last_request_at or 0) - (self.first_request_at or 0), 2)
            if self.first_request_at
            else 0.0,
        }


@dataclass
class RequestBudget:
    """Serialises and paces live source traffic."""

    min_interval: float = 1.5
    max_requests: int = 1200
    challenge_backoff: float = 45.0
    max_consecutive_challenges: int = 4
    jitter: float = 0.75
    # Multiplier applied to the interval after each challenge, so the run gets
    # progressively gentler instead of hammering.
    challenge_slowdown: float = 1.6
    max_interval: float = 30.0
    sleeper: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._interval = self.min_interval
        self.stats = BudgetStats()

    # -- injectable clock/sleep, so tests never actually wait ----------------

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.stats.sleep_seconds += seconds
        if self.sleeper is not None:
            self.sleeper(seconds)  # type: ignore[operator]
        else:
            time.sleep(seconds)

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    # -- pacing --------------------------------------------------------------

    @property
    def current_interval(self) -> float:
        return self._interval

    def acquire(self) -> None:
        """Block until another live request is permitted."""
        with self._lock:
            if self.stats.requests >= self.max_requests:
                raise BudgetExhausted(f"request budget of {self.max_requests} reached for this run")
            now = self._now()
            wait = self._next_allowed - now
            if wait > 0:
                self._sleep(wait)
                now = self._now()
            # Jitter only de-synchronises request timing; not security-sensitive.
            delay = self._interval + random.uniform(0, self.jitter)  # noqa: S311
            self._next_allowed = now + delay
            self.stats.requests += 1
            if self.stats.first_request_at is None:
                self.stats.first_request_at = now
            self.stats.last_request_at = now

    def record_bytes(self, count: int) -> None:
        self.stats.bytes_received += count

    def record_retry(self) -> None:
        self.stats.retries += 1

    def record_success(self) -> None:
        with self._lock:
            self.stats.consecutive_challenges = 0

    def record_challenge(self) -> float:
        """Register an access-control challenge and return how long to wait.

        Raises :class:`ChallengeWall` once the run has been challenged too many
        times in a row: continuing would be both useless and rude.
        """
        with self._lock:
            self.stats.challenges += 1
            self.stats.consecutive_challenges += 1
            n = self.stats.consecutive_challenges
            self._interval = min(self._interval * self.challenge_slowdown, self.max_interval)
            if n > self.max_consecutive_challenges:
                raise ChallengeWall(
                    f"source returned an access-control challenge {n} times in a row; "
                    "stopping requests for this run"
                )
            return self.challenge_backoff * (2 ** (n - 1))

    def honour_retry_after(self, seconds: float) -> None:
        """Respect an explicit Retry-After."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, self._now() + seconds)

    def backoff(self, seconds: float) -> None:
        self._sleep(seconds)

    def remaining(self) -> int:
        return max(0, self.max_requests - self.stats.requests)
