"""Scan qualification.

A listing scan may only support *absence* analysis when it passes every check
below. Anything less is still preserved as positive evidence -- "we saw these
advertisements at this time" -- but it can never be used to conclude that an
advertisement disappeared.

The rules are versioned (``QUALIFICATION_RULES_VERSION``). Each assessment
stores the individual check results, so a later reader can see exactly why a
scan did or did not qualify, and a rules change produces a new assessment
instead of silently reinterpreting an old one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import QUALIFICATION_RULES_VERSION

LEGITIMATE_TERMINATION = {"no_more_link", "empty_validated_page"}


@dataclass(slots=True)
class Check:
    name: str
    passed: bool | None  # None = not applicable / no evidence available
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class ScanFacts:
    """Everything the qualification rules need, gathered during the scan."""

    requested_start_url: str
    expected_start_url: str
    pages_requested: int = 0
    pages_ok: int = 0
    pages_failed: int = 0
    max_pages_bound: int = 200
    structure_unrecognized_pages: list[int] = field(default_factory=list)
    termination_reason: str | None = None
    unresolved_candidates: int = 0
    # Pages whose FINAL response was an access-control answer: coverage lost.
    access_control_pages: list[int] = field(default_factory=list)
    # Pages that were challenged but then retrieved completely after backing
    # off. Coverage is intact, so this does not fail the scan -- but it is
    # recorded so "the source pushed back" is never invisible.
    challenged_then_recovered: list[int] = field(default_factory=list)
    unexpected_redirect_pages: list[dict[str, Any]] = field(default_factory=list)
    non_200_pages: list[dict[str, Any]] = field(default_factory=list)
    unique_ids: int = 0
    entries_seen: int = 0
    duplicate_occurrences: int = 0
    source_reported_total: int | None = None
    empty_result_validated: bool | None = None
    loop_signatures: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Assessment:
    qualified: bool
    reason: str | None
    checks: list[dict[str, Any]]
    rules_version: str = QUALIFICATION_RULES_VERSION


def assess(facts: ScanFacts) -> Assessment:
    checks: list[Check] = []

    checks.append(
        Check(
            "scope_unfiltered",
            facts.requested_start_url == facts.expected_start_url,
            "traversal started at the configured unfiltered listing URL with no additional filters",
            {"requested": facts.requested_start_url, "expected": facts.expected_start_url},
        )
    )

    checks.append(
        Check(
            "structure_recognized",
            not facts.structure_unrecognized_pages,
            "every retrieved page matched the PageUp listing structure",
            {"unrecognized_pages": facts.structure_unrecognized_pages},
        )
    )

    checks.append(
        Check(
            "all_pages_retrieved",
            facts.pages_failed == 0,
            "no page in the traversal failed to retrieve",
            {"pages_requested": facts.pages_requested, "pages_failed": facts.pages_failed},
        )
    )

    checks.append(
        Check(
            "legitimate_termination",
            facts.termination_reason in LEGITIMATE_TERMINATION,
            "pagination ended because the source said so, not because a bound or "
            "a failure stopped it",
            {"termination_reason": facts.termination_reason},
        )
    )

    checks.append(
        Check(
            "no_unresolved_identity",
            facts.unresolved_candidates == 0,
            "every listing row resolved to a source job identifier",
            {"unresolved_candidates": facts.unresolved_candidates},
        )
    )

    checks.append(
        Check(
            "no_access_control_response",
            not facts.access_control_pages,
            "no page ended in a challenge, block or rate-limit response",
            {
                "pages_unrecovered": facts.access_control_pages,
                "pages_challenged_then_recovered": facts.challenged_then_recovered,
                "note": "a page that was challenged and then retrieved completely "
                "still has full coverage; it is listed here for visibility only",
            },
        )
    )

    checks.append(
        Check(
            "no_unexpected_redirect",
            not facts.unexpected_redirect_pages,
            "no listing page redirected somewhere unexpected",
            {"redirects": facts.unexpected_redirect_pages},
        )
    )

    checks.append(
        Check(
            "all_pages_http_200",
            not facts.non_200_pages,
            "every listing page answered 200",
            {"non_200": facts.non_200_pages},
        )
    )

    checks.append(
        Check(
            "within_page_bound",
            facts.pages_requested < facts.max_pages_bound,
            "the traversal finished well inside the configured page ceiling",
            {"pages_requested": facts.pages_requested, "bound": facts.max_pages_bound},
        )
    )

    checks.append(
        Check(
            "no_pagination_loop",
            len(facts.loop_signatures) == len(set(facts.loop_signatures)),
            "no page repeated a previously seen page signature",
            {"pages": len(facts.loop_signatures), "distinct": len(set(facts.loop_signatures))},
        )
    )

    # Reconciliation against the source's own count, when the source gives one.
    if facts.source_reported_total is None:
        checks.append(
            Check(
                "source_count_reconciles",
                None,
                "the source did not report a total for this traversal",
                {},
            )
        )
    else:
        checks.append(
            Check(
                "source_count_reconciles",
                facts.source_reported_total == facts.unique_ids,
                "unique advertisements found equals the total the source reported",
                {
                    "source_reported_total": facts.source_reported_total,
                    "unique_ids": facts.unique_ids,
                    "note": "the more-link count reports jobs remaining after page 1; "
                    "the total is that plus the rows on page 1. Category and location "
                    "counts are never summed, because they overlap.",
                },
            )
        )

    if facts.unique_ids == 0:
        checks.append(
            Check(
                "empty_result_validated",
                bool(facts.empty_result_validated),
                "a zero-result scan requires the intact empty-result template, not "
                "merely an empty parser output",
                {"empty_result_validated": facts.empty_result_validated},
            )
        )

    failed = [c for c in checks if c.passed is False]
    qualified = not failed
    reason = None if qualified else "; ".join(f"{c.name} failed" for c in failed)
    return Assessment(
        qualified=qualified,
        reason=reason,
        checks=[c.as_dict() for c in checks],
    )
