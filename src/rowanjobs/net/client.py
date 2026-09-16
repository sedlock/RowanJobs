"""The HTTP collector.

Rules enforced here:

* Archive first, parse second. :class:`FetchResult` carries raw bytes; nothing in
  this module decodes or interprets them.
* Redirects are followed manually so that every hop is validated by
  :class:`~rowanjobs.net.guard.UrlPolicy` and preserved as evidence.
* An access-control challenge is a distinct outcome. It is never reported as a
  missing page, and it never counts as evidence that an advertisement is gone.
* Responses larger than the configured ceiling are stored as an explicit partial
  capture with a coverage exception -- never silently truncated and called
  complete.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..timeutil import now_utc, utc_str
from .budget import BudgetExhausted, ChallengeWall, RequestBudget
from .guard import DestinationError, UrlPolicy

# Header names never written to the archive or the logs.
SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-amz-security-token",
}


def sanitize_headers(headers: Any) -> dict[str, str]:
    """Drop credential-bearing headers, keep everything else verbatim."""
    out: dict[str, str] = {}
    try:
        items = headers.multi_items()
    except AttributeError:
        items = list(headers.items())
    for key, value in items:
        lk = key.lower()
        if lk in SENSITIVE_HEADERS:
            out[lk] = "<redacted>"
        elif lk in out:
            out[lk] = f"{out[lk]}, {value}"
        else:
            out[lk] = value
    return out


def detect_access_control(status: int, headers: Any, body: bytes) -> str | None:
    """Classify an access-control response, or return None.

    AWS WAF answers a challenge with ``202`` plus ``x-amzn-waf-action``; a block
    is ``403`` with the same header. Cloudflare-style challenges are recognised
    too so the collector degrades the same way if the source ever moves.
    """
    action = None
    for key in ("x-amzn-waf-action", "x-amz-waf-action"):
        value = headers.get(key)
        if value:
            action = f"aws-waf-{value.lower()}"
            break
    if action:
        return action
    if status == 429:
        return "rate-limited"
    if status == 503 and b"cf-browser-verification" in body[:4096].lower():
        return "cf-challenge"
    if status == 403 and b"captcha" in body[:4096].lower():
        return "captcha"
    return None


@dataclass(slots=True)
class RedirectHop:
    url: str
    status: int
    location: str | None
    at_utc: str


@dataclass(slots=True)
class FetchResult:
    """Everything worth preserving about one retrieval attempt."""

    purpose: str
    requested_url: str
    final_url: str | None = None
    transport: str = "httpx"
    attempt_no: int = 1
    started_at_utc: str = ""
    ended_at_utc: str | None = None
    duration_ms: int | None = None
    http_status: int | None = None
    http_version: str | None = None
    response_state: str = "no_response"
    redirect_chain: list[RedirectHop] = field(default_factory=list)
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    content_encoding: str | None = None
    declared_content_length: int | None = None
    received_bytes: int = 0
    body: bytes | None = None
    representation: str = "http-wire-body"
    content_encoding_removed: str | None = None
    media_type: str | None = None
    charset_declared: str | None = None
    capture_state: str = "empty"
    capture_exception: str | None = None
    access_control_signal: str | None = None
    retry_after: str | None = None
    failure_kind: str | None = None
    failure_detail: str | None = None

    @property
    def redirect_count(self) -> int:
        return len(self.redirect_chain)

    @property
    def ok(self) -> bool:
        return (
            self.response_state == "complete"
            and self.http_status == 200
            and self.access_control_signal is None
        )

    def redirect_chain_json(self) -> list[dict[str, Any]]:
        return [
            {"url": h.url, "status": h.status, "location": h.location, "at_utc": h.at_utc}
            for h in self.redirect_chain
        ]


class SourceClient:
    """A paced, guarded HTTP client for exactly one source."""

    def __init__(
        self,
        budget: RequestBudget,
        policy: UrlPolicy,
        *,
        user_agent: str,
        accept_language: str = "en-US,en;q=0.9",
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        write_timeout: float = 10.0,
        pool_timeout: float = 10.0,
        http2: bool = True,
        max_redirects: int = 5,
        max_bytes: int = 25 * 1024 * 1024,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        backoff_max: float = 120.0,
        challenge_solver: Any | None = None,
    ) -> None:
        self.budget = budget
        self.policy = policy
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.challenge_solver = challenge_solver
        self.default_headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": accept_language,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        self._client = httpx.Client(
            http2=http2,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            ),
            # Connection reuse; still one request at a time via the budget.
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            headers=self.default_headers,
        )
        self.challenge_solve_attempts = 0

    # ----------------------------------------------------------------- public

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SourceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(
        self,
        url: str,
        *,
        purpose: str,
        max_bytes: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """Retrieve ``url``, retrying transient failures within the budget."""
        attempt = 0
        last: FetchResult | None = None
        while True:
            attempt += 1
            result = self._attempt(
                url,
                purpose=purpose,
                attempt_no=attempt,
                max_bytes=max_bytes if max_bytes is not None else self.max_bytes,
                extra_headers=extra_headers,
            )
            last = result

            if result.access_control_signal:
                if not self._handle_challenge(result, attempt):
                    return result
                continue

            if result.ok or not self._retryable(result):
                if result.ok:
                    self.budget.record_success()
                return result

            if attempt > self.max_retries:
                return result

            self.budget.record_retry()
            delay = min(self.backoff_base ** attempt, self.backoff_max)
            if result.retry_after:
                try:
                    delay = max(delay, float(result.retry_after))
                except ValueError:
                    pass
            self.budget.backoff(delay)

        return last  # pragma: no cover - unreachable

    # ---------------------------------------------------------------- private

    def _handle_challenge(self, result: FetchResult, attempt: int) -> bool:
        """Back off (and optionally solve) a challenge. True => retry."""
        try:
            wait = self.budget.record_challenge()
        except ChallengeWall as exc:
            result.failure_kind = "access_control"
            result.failure_detail = str(exc)
            return False
        if attempt > self.max_retries + 1:
            return False
        if self.challenge_solver is not None:
            self.challenge_solve_attempts += 1
            try:
                cookies = self.challenge_solver.solve(result.requested_url)
            except Exception as exc:  # noqa: BLE001 - solver is best effort
                result.failure_detail = f"challenge solver failed: {exc}"
                cookies = None
            if cookies:
                for name, value in cookies.items():
                    self._client.cookies.set(name, value, domain=".rowan.edu")
                return True
        self.budget.backoff(wait)
        return True

    @staticmethod
    def _retryable(result: FetchResult) -> bool:
        if result.failure_kind in {"timeout_connect", "timeout_read", "connect", "dns", "tls"}:
            return True
        if result.http_status is not None and result.http_status >= 500:
            return True
        if result.http_status == 429:
            return True
        return False

    def _attempt(
        self,
        url: str,
        *,
        purpose: str,
        attempt_no: int,
        max_bytes: int,
        extra_headers: dict[str, str] | None,
    ) -> FetchResult:
        result = FetchResult(
            purpose=purpose,
            requested_url=url,
            attempt_no=attempt_no,
            started_at_utc=utc_str(now_utc()),
        )
        started = time.monotonic()

        current = url
        try:
            for hop in range(self.max_redirects + 1):
                try:
                    current = self.policy.check(current)
                except DestinationError as exc:
                    result.failure_kind = "blocked_destination"
                    result.failure_detail = exc.reason
                    result.response_state = "no_response"
                    return result

                try:
                    self.budget.acquire()
                except BudgetExhausted as exc:
                    result.failure_kind = "budget_exhausted"
                    result.failure_detail = str(exc)
                    return result

                headers = dict(extra_headers or {})
                response = self._send(current, headers, result)
                if response is None:
                    return result

                with response:
                    result.http_status = response.status_code
                    result.http_version = response.http_version
                    result.response_headers = sanitize_headers(response.headers)
                    result.final_url = str(response.url)
                    retry_after = response.headers.get("retry-after")
                    if retry_after:
                        result.retry_after = retry_after
                        try:
                            self.budget.honour_retry_after(float(retry_after))
                        except ValueError:
                            pass

                    if response.is_redirect and hop < self.max_redirects:
                        location = response.headers.get("location")
                        result.redirect_chain.append(
                            RedirectHop(
                                url=current,
                                status=response.status_code,
                                location=location,
                                at_utc=utc_str(now_utc()),
                            )
                        )
                        # Drain so the connection can be reused.
                        body = self._read_capped(response, result, 64 * 1024)
                        if not location:
                            result.failure_kind = "http_error"
                            result.failure_detail = "redirect without Location"
                            result.response_state = "complete"
                            result.body = body
                            result.capture_state = "complete"
                            return result
                        current = str(httpx.URL(current).join(location))
                        continue

                    body = self._read_capped(response, result, max_bytes)
                    result.body = body
                    result.received_bytes = len(body)
                    self.budget.record_bytes(len(body))
                    result.declared_content_length = _int_or_none(
                        response.headers.get("content-length")
                    )
                    encoding = response.headers.get("content-encoding")
                    result.content_encoding = encoding
                    if encoding and encoding.lower() not in ("identity", ""):
                        # httpx removed the transfer encoding for us; these bytes
                        # are the decoded entity body, not the wire bytes.
                        result.representation = "http-decoded-body"
                        result.content_encoding_removed = encoding
                    else:
                        result.representation = "http-wire-body"
                    ctype = response.headers.get("content-type", "")
                    result.media_type = ctype.split(";")[0].strip() or None
                    result.charset_declared = _charset(ctype)
                    if result.capture_state != "partial":
                        result.capture_state = "complete" if body else "empty"
                    if result.response_state != "partial":
                        result.response_state = "complete"

                    result.access_control_signal = detect_access_control(
                        response.status_code, response.headers, body
                    )
                    if result.access_control_signal:
                        result.failure_kind = "access_control"
                        result.failure_detail = (
                            f"source returned an access-control response "
                            f"({result.access_control_signal}); this is collection "
                            "uncertainty, not evidence of absence"
                        )
                    elif response.status_code >= 400:
                        result.failure_kind = "http_error"
                        result.failure_detail = f"HTTP {response.status_code}"
                    return result

            result.failure_kind = "too_many_redirects"
            result.failure_detail = f"exceeded {self.max_redirects} redirects"
            return result
        finally:
            result.ended_at_utc = utc_str(now_utc())
            result.duration_ms = int((time.monotonic() - started) * 1000)
            result.request_headers = sanitize_headers(
                {**self.default_headers, **(extra_headers or {})}
            )

    def _send(
        self, url: str, headers: dict[str, str], result: FetchResult
    ) -> httpx.Response | None:
        request = self._client.build_request("GET", url, headers=headers)
        try:
            return self._client.send(request, stream=True)
        except httpx.ConnectTimeout as exc:
            result.failure_kind, result.failure_detail = "timeout_connect", str(exc)
        except httpx.ReadTimeout as exc:
            result.failure_kind, result.failure_detail = "timeout_read", str(exc)
        except httpx.ConnectError as exc:
            kind = "dns" if "getaddrinfo" in str(exc) or "Name or service" in str(exc) else "connect"
            result.failure_kind, result.failure_detail = kind, str(exc)
        except httpx.TooManyRedirects as exc:  # pragma: no cover - handled manually
            result.failure_kind, result.failure_detail = "too_many_redirects", str(exc)
        except httpx.HTTPError as exc:
            result.failure_kind, result.failure_detail = "transport", str(exc)
        result.response_state = "no_response"
        return None

    def _read_capped(self, response: httpx.Response, result: FetchResult, cap: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > cap:
                    keep = cap - (total - len(chunk))
                    if keep > 0:
                        chunks.append(chunk[:keep])
                    result.capture_state = "partial"
                    result.response_state = "partial"
                    result.capture_exception = (
                        f"response exceeded the {cap} byte limit; stored bytes are a "
                        "prefix and this retrieval is an explicit coverage exception"
                    )
                    response.close()
                    break
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            result.capture_state = "partial" if chunks else "empty"
            result.response_state = "partial" if chunks else "no_response"
            result.failure_kind = result.failure_kind or "transport"
            result.failure_detail = result.failure_detail or f"body read failed: {exc}"
            result.capture_exception = "body transfer failed part-way through"
        return b"".join(chunks)


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _charset(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset":
            return value.strip().strip('"') or None
    return None
