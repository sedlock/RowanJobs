"""Optional browser step for access-control challenges.

The Rowan career site is fronted by AWS WAF. When WAF decides a client is moving
too quickly it answers with the *challenge* action: an ordinary visitor's browser
runs the challenge script, receives an ``aws-waf-token`` cookie, and carries on.
There is no CAPTCHA and no human interaction involved.

This module reproduces exactly that ordinary path with a real, unmodified
Chromium: it loads the public page, lets the normal challenge script run, and
hands the resulting cookie back to the HTTP client. It does not spoof
fingerprints, rotate proxies, install stealth patches, or solve CAPTCHAs, and it
is only reached after the collector has already slowed down and backed off.

Collection stays correct without it: with the solver disabled, a challenge is
recorded as ``access_control_challenge`` -- an explicit coverage exception that
never becomes evidence of a missing advertisement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ChallengeSolver:
    """Obtain an access token the way a normal browser visit would."""

    enabled: bool = True
    headless: bool = True
    channel: str | None = None
    nav_timeout_seconds: float = 45.0
    token_ttl_seconds: float = 1800.0
    max_solves: int = 6
    user_agent: str | None = None
    solves: int = 0
    last_solved_at: float = field(default=0.0, repr=False)
    _cookies: dict[str, str] = field(default_factory=dict, repr=False)
    last_error: str | None = None

    def available(self) -> bool:
        if not self.enabled:
            return False
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            self.last_error = "playwright is not installed (extra: rowanjobs[browser])"
            return False
        return True

    def solve(self, url: str) -> dict[str, str] | None:
        if not self.available():
            return None
        now = time.monotonic()
        if self._cookies and (now - self.last_solved_at) < self.token_ttl_seconds:
            return dict(self._cookies)
        if self.solves >= self.max_solves:
            self.last_error = f"solver budget of {self.max_solves} reached for this run"
            return None

        from playwright.sync_api import sync_playwright

        self.solves += 1
        try:
            with sync_playwright() as pw:
                launch: dict[str, object] = {"headless": self.headless}
                if self.channel:
                    launch["channel"] = self.channel
                browser = pw.chromium.launch(**launch)  # type: ignore[arg-type]
                try:
                    context = browser.new_context(
                        user_agent=self.user_agent or None,
                        locale="en-US",
                    )
                    page = context.new_page()
                    page.goto(url, timeout=self.nav_timeout_seconds * 1000, wait_until="load")
                    # Give the challenge script time to settle and set its cookie.
                    page.wait_for_timeout(3000)
                    cookies = {
                        c["name"]: c["value"]
                        for c in context.cookies()
                        if c["name"].lower().startswith("aws-waf")
                    }
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001 - surfaced as last_error
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

        if not cookies:
            self.last_error = "browser visit produced no access token"
            return None
        self._cookies = cookies
        self.last_solved_at = time.monotonic()
        self.last_error = None
        return dict(cookies)

    def describe(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "available": self.available(),
            "solves": self.solves,
            "last_error": self.last_error,
        }
