"""Destination policy.

Fetched pages are untrusted input: neither a link nor a redirect inside one may
steer the collector at the local network, a link-local address or a cloud
metadata service.
"""

from __future__ import annotations

import pytest

from rowanjobs.net.guard import DestinationError, UrlPolicy, same_origin

from .conftest import FakeSource, redirect_response

ALLOWED = "https://jobs.rowan.edu/en-us/listing/"


@pytest.fixture
def policy() -> UrlPolicy:
    return UrlPolicy(("jobs.rowan.edu", "careers-static.pageuppeople.com"), resolve=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost/",
        "http://10.0.0.5/internal",
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://evil.example.com/",
        "https://jobs.rowan.edu.evil.example.com/",
    ],
)
def test_check_refuses_addresses_outside_the_collection_allowlist(
    policy: UrlPolicy, url: str
) -> None:
    with pytest.raises(DestinationError):
        policy.check(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://jobs.rowan.edu/x",
        "gopher://jobs.rowan.edu/",
        "//jobs.rowan.edu/x",
    ],
)
def test_check_refuses_schemes_other_than_http_and_https(policy: UrlPolicy, url: str) -> None:
    with pytest.raises(DestinationError, match=r"scheme|no host"):
        policy.check(url)


def test_check_refuses_embedded_credentials(policy: UrlPolicy) -> None:
    with pytest.raises(DestinationError, match="embedded credentials"):
        policy.check("https://user:secret@jobs.rowan.edu/en-us/listing/")
    with pytest.raises(DestinationError, match="embedded credentials"):
        policy.check("https://user@jobs.rowan.edu/en-us/listing/")


def test_metadata_service_is_refused_even_if_it_were_allowlisted() -> None:
    wide_open = UrlPolicy(("169.254.169.254",), resolve=False)
    with pytest.raises(DestinationError, match="metadata service"):
        wide_open.check("http://169.254.169.254/latest/meta-data/")


def test_literal_private_address_is_refused_when_resolution_is_enabled() -> None:
    policy = UrlPolicy(("127.0.0.1",), resolve=True)
    with pytest.raises(DestinationError, match="not a public address"):
        policy.check("http://127.0.0.1/")


def test_allowed_url_is_returned_normalised(policy: UrlPolicy) -> None:
    assert policy.check(ALLOWED) == ALLOWED
    assert policy.check("https://jobs.rowan.edu") == "https://jobs.rowan.edu/"
    assert policy.check("https://JOBS.rowan.edu./en-us/listing/?page=2#frag") == (
        "https://JOBS.rowan.edu./en-us/listing/?page=2"
    )


def test_subdomains_are_only_allowed_when_explicitly_configured() -> None:
    strict = UrlPolicy(("rowan.edu",), resolve=False)
    assert strict.host_allowed("rowan.edu") is True
    assert strict.host_allowed("jobs.rowan.edu") is False
    lenient = UrlPolicy(("rowan.edu",), allow_subdomains=True, resolve=False)
    assert lenient.host_allowed("jobs.rowan.edu") is True
    assert lenient.host_allowed("evil-rowan.edu") is False


def test_same_origin_compares_scheme_host_and_port() -> None:
    assert same_origin("https://jobs.rowan.edu/a", "https://jobs.rowan.edu/b") is True
    assert same_origin("https://jobs.rowan.edu/a", "http://jobs.rowan.edu/a") is False
    assert same_origin("https://jobs.rowan.edu/a", "https://other.rowan.edu/a") is False


def test_redirect_to_a_blocked_destination_is_refused_mid_chain(make_client) -> None:
    source = FakeSource()
    source.add(ALLOWED, redirect_response("http://169.254.169.254/latest/meta-data/"))
    client = make_client(source)

    result = client.fetch(ALLOWED, purpose="listing_page")

    assert result.failure_kind == "blocked_destination"
    assert result.failure_detail == "metadata service address"
    assert result.response_state == "no_response"
    assert result.body is None
    # The first hop happened; the blocked destination was never contacted.
    assert source.urls == [ALLOWED]
    assert [hop.location for hop in result.redirect_chain] == [
        "http://169.254.169.254/latest/meta-data/"
    ]


def test_redirect_off_the_allowlist_is_refused_mid_chain(make_client) -> None:
    source = FakeSource()
    source.add(ALLOWED, redirect_response("https://evil.example.com/collect"))
    client = make_client(source)

    result = client.fetch(ALLOWED, purpose="listing_page")

    assert result.failure_kind == "blocked_destination"
    assert "allowlist" in (result.failure_detail or "")
    assert source.urls == [ALLOWED]
