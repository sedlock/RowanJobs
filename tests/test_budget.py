"""The single request budget.

One budget paces every live request the application makes, and it is what turns
"be gentle with the source" into something the code actually enforces.
"""

from __future__ import annotations

import pytest

from rowanjobs.net.budget import BudgetExhausted, ChallengeWall, RequestBudget


def make_budget(**kwargs) -> tuple[RequestBudget, list[float]]:
    slept: list[float] = []
    kwargs.setdefault("min_interval", 0.0)
    kwargs.setdefault("jitter", 0.0)
    return RequestBudget(sleeper=slept.append, **kwargs), slept


def test_pacing_waits_between_requests_through_the_injected_sleeper() -> None:
    budget, slept = make_budget(min_interval=1.5)
    budget.acquire()
    budget.acquire()
    budget.acquire()
    assert budget.stats.requests == 3
    # The first request is immediate; each later one waits out the interval.
    assert len(slept) == 2
    assert all(0 < s <= 1.5 for s in slept)
    assert budget.stats.sleep_seconds == pytest.approx(sum(slept))


def test_request_ceiling_is_a_hard_stop_for_the_run() -> None:
    budget, _ = make_budget(max_requests=2)
    budget.acquire()
    budget.acquire()
    assert budget.remaining() == 0
    with pytest.raises(BudgetExhausted, match="request budget of 2"):
        budget.acquire()
    assert budget.stats.requests == 2


def test_each_consecutive_challenge_backs_off_further_and_slows_the_run() -> None:
    budget, _ = make_budget(min_interval=1.0, challenge_backoff=45.0, max_consecutive_challenges=4)
    assert budget.record_challenge() == 45.0
    assert budget.record_challenge() == 90.0
    assert budget.record_challenge() == 180.0
    assert budget.stats.challenges == 3
    # The pacing interval itself gets gentler for the rest of the run.
    assert budget.current_interval > 1.0


def test_challenge_wall_stops_requesting_rather_than_hammering_the_source() -> None:
    budget, _ = make_budget(max_consecutive_challenges=2)
    budget.record_challenge()
    budget.record_challenge()
    with pytest.raises(ChallengeWall, match="3 times in a row"):
        budget.record_challenge()


def test_a_success_resets_the_consecutive_challenge_counter() -> None:
    budget, _ = make_budget(max_consecutive_challenges=2, challenge_backoff=10.0)
    budget.record_challenge()
    budget.record_challenge()
    budget.record_success()
    assert budget.stats.consecutive_challenges == 0
    assert budget.record_challenge() == 10.0
    assert budget.stats.challenges == 3


def test_the_slowed_interval_is_not_reset_by_a_success() -> None:
    """Challenges slow the whole run down; a single success does not undo that."""
    budget, _ = make_budget(min_interval=1.0)
    budget.record_challenge()
    slowed = budget.current_interval
    budget.record_success()
    assert budget.current_interval == slowed > 1.0


def test_interval_growth_is_capped() -> None:
    budget, _ = make_budget(min_interval=1.0, max_interval=5.0, max_consecutive_challenges=100)
    for _ in range(20):
        budget.record_challenge()
    assert budget.current_interval == 5.0


def test_retry_after_defers_the_next_request() -> None:
    budget, slept = make_budget(min_interval=0.0)
    budget.honour_retry_after(30.0)
    budget.acquire()
    assert slept and slept[0] == pytest.approx(30.0, abs=0.5)


def test_explicit_backoff_and_byte_accounting_are_recorded() -> None:
    budget, slept = make_budget()
    budget.backoff(12.5)
    budget.record_bytes(2048)
    budget.record_retry()
    assert slept == [12.5]
    assert budget.stats.bytes_received == 2048
    assert budget.stats.retries == 1


def test_negative_or_zero_waits_never_reach_the_sleeper() -> None:
    budget, slept = make_budget()
    budget.backoff(0)
    budget.backoff(-5)
    assert slept == []
    assert budget.stats.sleep_seconds == 0


def test_stats_summary_reports_the_run_traffic() -> None:
    budget, _ = make_budget()
    budget.acquire()
    budget.record_bytes(100)
    budget.record_challenge()
    summary = budget.stats.as_dict()
    assert summary["requests"] == 1
    assert summary["bytes_received"] == 100
    assert summary["challenges"] == 1
    assert summary["wall_seconds"] >= 0
