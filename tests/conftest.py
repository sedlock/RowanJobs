"""Shared fixtures and synthetic page builders.

Every test in this suite is offline and deterministic:

* HTTP always goes through :class:`httpx.MockTransport` via :class:`FakeSource`,
  which also records exactly which URLs were requested.
* The request budget never sleeps: ``min_interval`` and ``jitter`` are zero and
  a ``sleeper`` hook captures any back-off instead of waiting.
* Every database lives under ``tmp_path``; an autouse fixture repoints the XDG
  and ``ROWANJOBS_*`` environment variables so nothing can reach the real
  ``~/.local/share/rowanjobs``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from rowanjobs.collect.repo import Repository
from rowanjobs.collect.runner import CollectionResult, Collector
from rowanjobs.config import Config
from rowanjobs.db import Database, open_db
from rowanjobs.net.budget import RequestBudget
from rowanjobs.net.client import SourceClient
from rowanjobs.net.guard import UrlPolicy
from rowanjobs.timeutil import now_utc

FIXTURES = Path(__file__).parent / "fixtures" / "pageup"

BASE_URL = "https://jobs.rowan.edu"
LISTING_URL = f"{BASE_URL}/en-us/listing/"

Responder = Callable[[httpx.Request], httpx.Response]


# --------------------------------------------------------------- page fixtures


def read_fixture(name: str) -> str:
    """Read one of the trimmed real captures in ``tests/fixtures/pageup``."""
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def detail_url(job_id: str, slug: str = "example-position") -> str:
    return f"{BASE_URL}/en-us/job/{job_id}/{slug}"


def listing_job(
    job_id: str | None,
    title: str = "Example Position",
    *,
    location: str = "Glassboro, New Jersey",
    closes: str = "Sep 29 2026",
    closes_machine: str = "2026-09-30T03:55:00Z",
    summary: str | None = "Summary of the advertisement.",
    href: str | None = None,
    slug: str = "example-position",
) -> dict[str, Any]:
    """One advertisement row for :func:`build_listing_page`."""
    if href is None:
        href = f"/en-us/job/{job_id}/{slug}" if job_id is not None else "/en-us/listing/"
    return {
        "job_id": job_id,
        "title": title,
        "location": location,
        "closes": closes,
        "closes_machine": closes_machine,
        "summary": summary,
        "href": href,
    }


def _listing_rows(jobs: list[dict[str, Any]], *, comment_summaries: bool) -> str:
    out: list[str] = []
    for row in jobs:
        out.append(
            "    <tr>\n"
            "      <td>\n"
            f'        <a class="job-link" href="{row["href"]}">{row["title"]}</a>\n'
            "      </td>\n"
            "      <td>\n"
            f'        <span class="location">{row["location"]}</span>\n'
            "      </td>\n"
            "      <td>\n"
            f'        <span class="close-date"><time datetime="{row["closes_machine"]}">'
            f"{row['closes']} </time></span>\n"
            "      </td>\n"
            "    </tr>"
        )
        if row["summary"]:
            summary = f'    <tr class="summary">\n      <td colspan="3">{row["summary"]}</td>\n    </tr>'
            out.append(f"    <!--{summary}-->" if comment_summaries else summary)
    return "\n".join(out)


def _listing_table(heading: str, tbody_id: str, rows: str, *, columns: bool) -> str:
    header = (
        "      <tr>\n"
        '        <th width="60%">Position</th>\n'
        '        <th width="20%">Location</th>\n'
        '        <th width="20%">Closes</th>\n'
        "      </tr>\n"
        if columns
        else "      <tr><th>Advertisement</th></tr>\n"
    )
    return (
        f"  <h2>{heading}</h2>\n"
        "  <table>\n"
        "    <thead>\n"
        f"{header}"
        "    </thead>\n"
        f'  <tbody id="{tbody_id}">\n'
        f"{rows}\n"
        "    </tbody>\n"
        "    </table>\n"
    )


def build_listing_page(
    jobs: list[dict[str, Any]],
    *,
    more_href: str | None = None,
    more_count: int | None = None,
    include_recent_section: bool = True,
    recent_jobs: list[dict[str, Any]] | None = None,
    heading: str = "Search results",
    columns: bool = True,
    title: str = "Job Postings | Human Resources | Rowan University",
) -> str:
    """Build a PageUp listing page with the same shape as the real captures.

    ``recent_jobs`` defaults to ``jobs``: the live site repeats every row in a
    second ``tbody#recent-jobs-content`` section with the summaries commented
    out, which is what makes naive ``a.job-link`` counting double the inventory.
    """
    search = _listing_table(
        heading,
        "search-results-content",
        _listing_rows(jobs, comment_summaries=False),
        columns=columns,
    )
    recent = ""
    if include_recent_section:
        repeated = jobs if recent_jobs is None else recent_jobs
        recent = _listing_table(
            "Current Opportunities",
            "recent-jobs-content",
            _listing_rows(repeated, comment_summaries=True),
            columns=columns,
        )
    more = ""
    if more_href is not None:
        count = "" if more_count is None else f'<span class="count">{more_count}</span>'
        more = (
            f'    <p><a href="{more_href}" class="more-link button" style="display:block"'
            f' title="More Jobs">More Jobs {count}</a></p>\n'
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en-us">\n'
        f'<head><meta charset="utf-8"><title>{title}</title></head>\n'
        "<body>\n"
        '<div id="listing">\n'
        f"{search}"
        f"{more}"
        "</div>\n"
        f"{recent}"
        "</body>\n"
        "</html>\n"
    )


def build_detail_page(
    *,
    job_id: str | None = "501826",
    title: str = "Example Position",
    work_type: str | None = "Temporary Part-Time",
    location: str | None = "Glassboro, New Jersey",
    categories: str | None = "Public Safety/Security",
    extra_fields: tuple[tuple[str, str], ...] = (),
    body_html: str = "<p>The advertisement body.</p>",
    include_job_details: bool = True,
    advertised: tuple[str, str] | None = ("Sep 15 2026 ", "2026-09-15T12:00:00Z"),
    applications_close: tuple[str, str] | None = (
        "Sep 29 2026 11:55 PM ",
        "2026-09-30T03:55:00Z",
    ),
    tz_text: str = "Eastern Daylight Time",
    messages: str = "",
    job_no_span: str | None = None,
    extra_body_html: str = "",
) -> str:
    """Build a PageUp detail page.

    ``None`` for a metadata value omits the whole line (the label is absent);
    the empty string keeps the label with an empty value (a blank field).
    ``job_no_span`` overrides the value inside ``span.job-externalJobNo`` so a
    disagreement between the labelled value and the span can be exercised.
    """
    lines: list[str] = []
    if job_id is not None:
        span_value = job_id if job_no_span is None else job_no_span
        lines.append(
            f'  <b>Job no:</b> <span class="job-externalJobNo">{span_value}</span><br>'
        )
    for label, css, value in (
        ("Work type", "work-type", work_type),
        ("Location", "location", location),
        ("Categories", "categories", categories),
    ):
        if value is None:
            continue
        if value == "":
            lines.append(f"  <b>{label}:</b> <br>")
        else:
            lines.append(f'  <b>{label}:</b> <span class="{css}">{value}</span><br>')
    for label, value_html in extra_fields:
        lines.append(f"  <b>{label}</b> {value_html}<br>")

    details = f'<div id="job-details">\n{body_html}\n</div>' if include_job_details else ""

    dates: list[str] = []
    if advertised is not None:
        text, machine = advertised
        dates.append(
            f'  <b>Advertised:</b> <span class="open-date">'
            f'<time datetime="{machine}">{text}</time></span> {tz_text}<br>'
        )
    if applications_close is not None:
        text, machine = applications_close
        dates.append(
            f'  <b>Applications close:</b> <span class="close-date">'
            f'<time datetime="{machine}">{text}</time></span> {tz_text}'
        )
    date_block = "<p>\n" + "\n".join(dates) + "\n</p>" if dates else ""

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en-us">\n'
        '<head><meta charset="utf-8"><title>Job Postings | Rowan University</title></head>\n'
        "<body>\n"
        f'<div id="messages" style="display: none"><ul id="message-list" role="presentation">'
        f"{messages}</ul></div>\n"
        '<div id="job"><div id="job-content">\n'
        f"<h2>{title}</h2>\n"
        "<p>\n" + "\n".join(lines) + "\n</p>\n"
        f"{details}\n"
        f"{date_block}\n"
        f"{extra_body_html}\n"
        "</div></div>\n"
        "</body>\n"
        "</html>\n"
    )


UNRELATED_ERROR_PAGE = (
    "<!DOCTYPE html>\n<html lang='en'><head><title>Something went wrong</title></head>\n"
    "<body><h1>We are sorry</h1><p>An unexpected error occurred. "
    "Please try again later.</p></body></html>\n"
)


# ------------------------------------------------------------------ responders


def html_response(
    markup: str,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    charset: str = "utf-8",
) -> Responder:
    body = markup.encode(charset)

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=body,
            headers={"content-type": f"text/html; charset={charset}", **(headers or {})},
        )

    return respond


def bytes_response(
    payload: bytes, *, media_type: str = "application/pdf", status: int = 200
) -> Responder:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, headers={"content-type": media_type})

    return respond


def redirect_response(location: str, *, status: int = 302) -> Responder:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"location": location})

    return respond


def challenge_response(*, status: int = 202, action: str = "challenge") -> Responder:
    """An AWS WAF challenge: HTTP 202, empty body, ``x-amzn-waf-action``."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"", headers={"x-amzn-waf-action": action})

    return respond


def error_response(status: int = 500, body: str = "server error") -> Responder:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode("utf-8"))

    return respond


class FakeSource:
    """A routing table for :class:`httpx.MockTransport` that records requests.

    Routes are queues: each extra responder registered for a URL answers one
    further request, and the final responder repeats for every request after
    that. An unrouted URL answers 404 so a test can never accidentally depend on
    a real request.
    """

    def __init__(self) -> None:
        self._routes: dict[str, list[Responder]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, *responders: Responder) -> FakeSource:
        self._routes.setdefault(url, []).extend(responders)
        return self

    def page(self, url: str, markup: str, **kwargs: Any) -> FakeSource:
        return self.add(url, html_response(markup, **kwargs))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self._routes.get(str(request.url))
        if not queue:
            return httpx.Response(404, content=b"<html><body>not routed</body></html>")
        responder = queue[0] if len(queue) == 1 else queue.pop(0)
        return responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def count(self, url: str) -> int:
        return self.urls.count(url)


# ------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from the operator's real data root and config."""
    monkeypatch.setenv("ROWANJOBS_DATA_ROOT", str(tmp_path / "env-data-root"))
    monkeypatch.setenv("ROWANJOBS_CONFIG", str(tmp_path / "env-config" / "config.toml"))
    monkeypatch.delenv("ROWANJOBS_DB", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(data_root=tmp_path / "data")
    config.network.min_interval_seconds = 0.0
    config.network.jitter_seconds = 0.0
    config.network.challenge_backoff_seconds = 0.0
    config.network.backoff_base_seconds = 0.0
    config.network.max_retries = 1
    config.browser.enabled = False
    return config


@pytest.fixture
def db(cfg: Config) -> Iterator[Database]:
    cfg.layout.ensure()
    database = open_db(cfg.layout.db_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    return Repository(db)


@pytest.fixture
def sleeps() -> list[float]:
    """Every back-off the budget asked for, captured instead of slept."""
    return []


@pytest.fixture
def budget(sleeps: list[float]) -> RequestBudget:
    return RequestBudget(
        min_interval=0,
        jitter=0,
        challenge_backoff=0,
        max_consecutive_challenges=2,
        sleeper=sleeps.append,
    )


@pytest.fixture
def make_client(budget: RequestBudget) -> Iterator[Callable[..., SourceClient]]:
    opened: list[SourceClient] = []

    def factory(source: FakeSource | Callable[..., httpx.Response], **kwargs: Any) -> SourceClient:
        handler = source.handler if isinstance(source, FakeSource) else source
        client = SourceClient(
            kwargs.pop("budget", budget),
            kwargs.pop("policy", UrlPolicy(("jobs.rowan.edu",), resolve=False)),
            user_agent=kwargs.pop("user_agent", "RowanJobsTests/1.0 (+tests)"),
            max_retries=kwargs.pop("max_retries", 1),
            backoff_base=kwargs.pop("backoff_base", 0.0),
            backoff_max=kwargs.pop("backoff_max", 0.0),
            http2=False,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )
        opened.append(client)
        return client

    yield factory
    for client in opened:
        client.close()


@pytest.fixture
def advancing_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Give every retrieval its own second, so timestamps stay distinguishable."""
    base = now_utc()
    state = {"tick": 0}

    def fake_now() -> Any:
        state["tick"] += 1
        return base + timedelta(seconds=state["tick"])

    monkeypatch.setattr("rowanjobs.net.client.now_utc", fake_now)
    return lambda: state["tick"]


@pytest.fixture
def run_environment(cfg: Config, repo: Repository) -> dict[str, Any]:
    """A source, config and run row, for tests that drive one component."""
    source_id = repo.ensure_source(cfg)
    source_config_id = repo.ensure_source_config(source_id, cfg)
    run_id, run_uuid = repo.start_run(
        source_id=source_id,
        source_config_id=source_config_id,
        run_kind="manual",
        scheduled_slot_utc=None,
        scheduled_slot_local_date="2026-09-16",
    )
    return {
        "source_id": source_id,
        "source_config_id": source_config_id,
        "run_id": run_id,
        "run_uuid": run_uuid,
    }


@pytest.fixture
def collect(
    cfg: Config, db: Database, make_client: Callable[..., SourceClient]
) -> Callable[..., CollectionResult]:
    """Run a whole collection against a :class:`FakeSource`."""

    def runner(source: FakeSource, *, run_kind: str = "manual", **kwargs: Any) -> CollectionResult:
        client = kwargs.pop("client", None) or make_client(source)
        collector = Collector(cfg, db=db, client=client)
        return collector.run(run_kind=run_kind, **kwargs)

    return runner
