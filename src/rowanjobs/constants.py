"""Enumerations shared across the schema and the code.

Keeping them here means the CHECK constraints in the migration and the Python
call sites cannot drift apart silently.
"""

from __future__ import annotations

RUN_KINDS = ("daily", "retry", "manual", "verification")

RUN_OUTCOMES = (
    "success",  # every planned unit finished, discovery qualified
    "partial",  # useful evidence collected, but at least one coverage exception
    "failed",  # nothing useful collected
    "aborted",  # interrupted; may be resumed
    "lock_contention",  # another collector held the lock; nothing attempted
)

SCAN_ROLES = ("discovery", "verification", "reconciliation")

SCAN_TERMINATION = (
    "no_more_link",  # source signalled the final page
    "empty_validated_page",  # recognised structure, validated zero rows
    "max_pages",  # configured safety bound hit -> NOT qualified
    "loop_detected",  # pagination repeated a previously seen page signature
    "fetch_failure",  # a page could not be retrieved
    "structure_unrecognized",  # listing markup did not match the adapter
)

# What a single retrieval attempt produced.
RESPONSE_STATES = ("complete", "partial", "no_response")

FETCH_PURPOSES = ("listing_page", "posting_detail", "resource", "probe")

CAPTURE_STATES = ("complete", "partial", "empty")

# How the archived bytes relate to what came off the wire.
REPRESENTATIONS = ("http-decoded-body", "http-wire-body", "browser-dom-serialized")

IDENTITY_STATES = (
    "match",  # page displays the expected job id
    "mismatch",  # page displays a different job id
    "absent_on_page",  # no job id present in the response
    "not_observed",  # no usable response at all
)

AVAILABILITY_STATES = (
    "content_captured",  # a real advertisement body was retrieved
    "explicit_closure",  # source displayed a closed/unavailable template
    "not_found",  # source returned a definite not-found
    "redirected_to_listing",  # detail URL bounced to the general listing
    "redirected_to_other_job",  # detail URL bounced to a different advertisement
    "identity_mismatch",  # body parsed but the job id disagreed
    "access_control_challenge",  # WAF/bot challenge: collection uncertainty, NOT absence
    "retrieval_failed",  # network/HTTP failure: collection uncertainty, NOT absence
)

# States that must never be read as evidence that an advertisement disappeared.
UNCERTAIN_AVAILABILITY = (
    "access_control_challenge",
    "retrieval_failed",
)

# States where the detail URL still serves substantive advertisement content.
SUBSTANTIVE_AVAILABILITY = ("content_captured",)

# States that count towards "this historical URL is terminal" and so may be
# demoted from daily to weekly rechecking.
TERMINAL_AVAILABILITY = (
    "explicit_closure",
    "not_found",
    "redirected_to_listing",
)

CHECK_REASONS = ("listed", "historical-daily", "historical-weekly", "manual")

WORK_KINDS = ("posting_detail", "resource")

WORK_STATES = ("pending", "in_progress", "done", "failed", "abandoned")

FIELD_STATES = (
    "present",  # label found, non-empty value
    "blank",  # label found, explicitly empty value
    "absent",  # validated structure, label not present
    "unresolved",  # structure not validated, so we cannot say
)

DATE_PARSE_STATES = ("parsed", "unparsed", "invalid", "absent")

DATE_PRECISIONS = ("date", "minute", "second", "unknown")

RESOURCE_OUTCOMES = (
    "captured",
    "too_large",
    "unsupported",
    "failed",
    "blocked_destination",
    "not_modified",
)

RESOURCE_CLASSIFICATIONS = (
    "job_document",  # substantive job-specific attachment -> in scope
    "apply_workflow",  # application submission -> never followed
    "internal_nav",  # site chrome / other listings
    "external",  # unrelated external destination
    "mailto",
    "anchor",
    "unknown",
)

PRESENCE_EVENT_KINDS = (
    "first_observed",
    "listed",
    "absent_qualified",
    "reappeared",
    "content_changed",
    "coverage_gap",
)

BACKUP_KINDS = ("daily", "weekly", "monthly", "manual", "predeploy")

PROTECTION_STATES = ("VERIFIED", "DEGRADED", "UNCONFIGURED", "BLOCKED_EXTERNAL", "FAILED")

# Delivery state of one run report (``notifications.state``, migration 7).
# 'skipped' is not a failure: it records a run that was deliberately never
# reported, which is what runs predating the reporting feature are.
NOTIFICATION_STATES = ("pending", "accepted", "failed", "abandoned", "skipped")

NOTIFICATION_KINDS = ("run_report",)

# Why a delivery attempt failed. Only 'transient' is worth another attempt.
NOTIFICATION_FAILURE_KINDS = ("transient", "permanent")
