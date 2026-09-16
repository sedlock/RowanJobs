"""The HTTP collector.

Archive first, parse second: a fetch result carries raw bytes plus everything
worth preserving about the retrieval. An access-control response is its own
outcome and is never reported as a missing page; an oversized response becomes
an explicit partial capture rather than a silently truncated "complete" one.
"""

from __future__ import annotations

import httpx
import pytest

from rowanjobs.net.budget import RequestBudget
from rowanjobs.net.client import detect_access_control, sanitize_headers

from .conftest import (
    LISTING_URL,
    FakeSource,
    bytes_response,
    challenge_response,
    error_response,
    html_response,
    redirect_response,
)

DETAIL_URL = "https://jobs.rowan.edu/en-us/job/501826/slug"


# ------------------------------------------------------------------- headers


def test_credential_bearing_headers_are_redacted_not_archived() -> None:
    sanitized = sanitize_headers(
        httpx.Headers(
            {
                "Authorization": "Bearer secret-token",
                "Cookie": "aws-waf-token=abc",
                "Set-Cookie": "session=xyz",
                "X-Api-Key": "k",
                "Server": "nginx",
                "Content-Type": "text/html",
            }
        )
    )
    assert sanitized["authorization"] == "<redacted>"
    assert sanitized["cookie"] == "<redacted>"
    assert sanitized["set-cookie"] == "<redacted>"
    assert sanitized["x-api-key"] == "<redacted>"
    assert sanitized["server"] == "nginx"
    assert "secret-token" not in str(sanitized)


def test_repeated_headers_are_joined_rather_than_lost() -> None:
    headers = httpx.Headers([("vary", "accept"), ("vary", "user-agent")])
    assert sanitize_headers(headers)["vary"] == "accept, user-agent"


# --------------------------------------------------------- access control


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        (202, {"x-amzn-waf-action": "challenge"}, b"", "aws-waf-challenge"),
        (403, {"x-amzn-waf-action": "block"}, b"", "aws-waf-block"),
        (429, {}, b"", "rate-limited"),
        (503, {}, b"<html>cf-browser-verification</html>", "cf-challenge"),
        (403, {}, b"<html>CAPTCHA required</html>", "captcha"),
        (200, {}, b"<html>fine</html>", None),
        (404, {}, b"<html>gone</html>", None),
    ],
)
def test_access_control_responses_are_classified(
    status: int, headers: dict[str, str], body: bytes, expected: str | None
) -> None:
    assert detect_access_control(status, httpx.Headers(headers), body) == expected


def test_challenge_is_a_distinct_outcome_and_never_a_missing_page(make_client) -> None:
    source = FakeSource()
    source.add(LISTING_URL, challenge_response())
    client = make_client(source, max_retries=0)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.http_status == 202
    assert result.access_control_signal == "aws-waf-challenge"
    assert result.failure_kind == "access_control"
    assert "not evidence of absence" in (result.failure_detail or "")
    assert result.ok is False
    assert result.body == b""
    assert result.capture_state == "empty"


def test_a_persistent_challenge_stops_requesting_after_the_configured_wall(
    make_client, budget: RequestBudget
) -> None:
    source = FakeSource()
    source.add(LISTING_URL, challenge_response())
    client = make_client(source, max_retries=5)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    # budget fixture allows two consecutive challenges before the wall.
    assert budget.stats.challenges == 3
    assert source.count(LISTING_URL) == 3
    assert result.access_control_signal == "aws-waf-challenge"
    assert "stopping requests for this run" in (result.failure_detail or "")


def test_a_challenge_followed_by_a_good_page_is_retried_and_succeeds(make_client) -> None:
    source = FakeSource()
    source.add(LISTING_URL, challenge_response(), html_response("<html>ok</html>"))
    client = make_client(source, max_retries=3)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.ok is True
    assert result.access_control_signal is None
    assert source.count(LISTING_URL) == 2


# -------------------------------------------------------------- happy path


def test_successful_fetch_preserves_the_bytes_and_the_retrieval_metadata(make_client) -> None:
    source = FakeSource()
    source.add(LISTING_URL, html_response("<html>listing</html>"))
    client = make_client(source)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.ok is True
    assert result.body == b"<html>listing</html>"
    assert result.received_bytes == len(result.body)
    assert result.response_state == "complete"
    assert result.capture_state == "complete"
    assert result.representation == "http-wire-body"
    assert result.media_type == "text/html"
    assert result.charset_declared == "utf-8"
    assert result.purpose == "listing_page"
    assert result.final_url == LISTING_URL
    assert result.started_at_utc.endswith("Z")
    assert result.ended_at_utc.endswith("Z")
    assert result.duration_ms is not None
    assert result.request_headers["user-agent"] == "RowanJobsTests/1.0 (+tests)"


def test_empty_body_is_an_empty_capture_not_a_complete_one(make_client) -> None:
    source = FakeSource()
    source.add(LISTING_URL, html_response(""))
    client = make_client(source)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.capture_state == "empty"
    assert result.body == b""


# ---------------------------------------------------------------- redirects


def test_every_redirect_hop_is_validated_and_preserved(make_client) -> None:
    source = FakeSource()
    source.add(DETAIL_URL, redirect_response("/en-us/listing/"))
    source.add(LISTING_URL, html_response("<html>listing</html>"))
    client = make_client(source)

    result = client.fetch(DETAIL_URL, purpose="posting_detail")

    assert result.ok is True
    assert result.final_url == LISTING_URL
    assert result.redirect_count == 1
    hop = result.redirect_chain[0]
    assert (hop.url, hop.status, hop.location) == (DETAIL_URL, 302, "/en-us/listing/")
    assert hop.at_utc.endswith("Z")
    assert result.redirect_chain_json()[0]["location"] == "/en-us/listing/"


def test_redirect_loops_stop_at_the_configured_hop_limit(make_client) -> None:
    source = FakeSource()
    source.add(DETAIL_URL, redirect_response(DETAIL_URL))
    client = make_client(source, max_redirects=2)

    result = client.fetch(DETAIL_URL, purpose="posting_detail")

    # Two hops are followed, then the last redirect is kept as the outcome
    # rather than being chased further or mistaken for a successful capture.
    assert source.count(DETAIL_URL) == 3
    assert result.redirect_count == 2
    assert result.http_status == 302
    assert result.ok is False


def test_redirect_without_a_location_is_recorded_rather_than_followed(make_client) -> None:
    source = FakeSource()

    def no_location(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    source.add(DETAIL_URL, no_location)
    client = make_client(source)

    result = client.fetch(DETAIL_URL, purpose="posting_detail")

    assert result.failure_kind == "http_error"
    assert result.failure_detail == "redirect without Location"


# ------------------------------------------------------------------- limits


def test_oversized_response_is_an_explicit_partial_capture(make_client) -> None:
    source = FakeSource()
    source.add(DETAIL_URL, bytes_response(b"A" * 5000, media_type="application/pdf"))
    client = make_client(source)

    result = client.fetch(DETAIL_URL, purpose="resource", max_bytes=1000)

    assert result.capture_state == "partial"
    assert result.response_state == "partial"
    assert result.ok is False
    assert result.body == b"A" * 1000
    assert result.capture_exception is not None
    assert "explicit coverage exception" in result.capture_exception


def test_a_response_at_the_limit_is_still_a_complete_capture(make_client) -> None:
    source = FakeSource()
    source.add(DETAIL_URL, bytes_response(b"A" * 1000))
    client = make_client(source)

    result = client.fetch(DETAIL_URL, purpose="resource", max_bytes=1000)

    assert result.capture_state == "complete"
    assert result.capture_exception is None


# ------------------------------------------------------------------ retries


def test_server_errors_are_retried_within_the_budget_and_never_slept_through(
    make_client, budget: RequestBudget, sleeps: list[float]
) -> None:
    source = FakeSource()
    source.add(LISTING_URL, error_response(500), html_response("<html>ok</html>"))
    client = make_client(source, max_retries=3)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.ok is True
    assert budget.stats.retries == 1
    assert source.count(LISTING_URL) == 2
    assert sleeps == []  # backoff_base is zero in tests; nothing really waited


def test_retries_stop_at_the_configured_maximum(make_client) -> None:
    source = FakeSource()
    source.add(LISTING_URL, error_response(503))
    client = make_client(source, max_retries=2)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.http_status == 503
    assert result.failure_kind == "http_error"
    assert source.count(LISTING_URL) == 3


def test_client_errors_are_not_retried(make_client) -> None:
    source = FakeSource()
    source.add(DETAIL_URL, error_response(404, "gone"))
    client = make_client(source, max_retries=3)

    result = client.fetch(DETAIL_URL, purpose="posting_detail")

    assert result.http_status == 404
    assert result.failure_kind == "http_error"
    assert result.ok is False
    assert source.count(DETAIL_URL) == 1


def test_retry_after_is_honoured_for_a_rate_limited_response(make_client, sleeps) -> None:
    source = FakeSource()

    def throttled(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "7"}, content=b"slow down")

    source.add(LISTING_URL, throttled, html_response("<html>ok</html>"))
    client = make_client(source, max_retries=2)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.ok is True
    assert any(s == pytest.approx(7.0, abs=0.5) for s in sleeps)


def test_transport_failures_are_classified_and_retried(make_client) -> None:
    calls: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, content=b"<html>ok</html>")

    client = make_client(flaky, max_retries=2)
    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.ok is True
    assert len(calls) == 2


def test_a_permanent_transport_failure_reports_no_response(make_client) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    client = make_client(broken, max_retries=1)
    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.response_state == "no_response"
    assert result.failure_kind == "timeout_read"
    assert result.body is None
    assert result.ok is False


def test_budget_exhaustion_ends_the_run_without_another_request(make_client) -> None:
    source = FakeSource()
    source.add(LISTING_URL, html_response("<html>ok</html>"))
    exhausted = RequestBudget(min_interval=0, jitter=0, max_requests=0, sleeper=lambda _s: None)
    client = make_client(source, budget=exhausted)

    result = client.fetch(LISTING_URL, purpose="listing_page")

    assert result.failure_kind == "budget_exhausted"
    assert source.requests == []


def test_blocked_destination_is_refused_before_any_connection(make_client) -> None:
    source = FakeSource()
    client = make_client(source)

    result = client.fetch("https://evil.example.com/", purpose="listing_page")

    assert result.failure_kind == "blocked_destination"
    assert source.requests == []
    assert result.response_state == "no_response"
