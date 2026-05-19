"""
Unit tests for DLQ monitor retry logic.

Tests the count-based retry decision as a pure function
that mirrors the consumer's x-death header parsing.
"""

from __future__ import annotations


def should_retry(
    x_death: list[dict] | None,
    max_retries: int = 3,
) -> tuple[bool, int, int]:
    """Pure function mirroring DLQ consumer retry logic.

    Args:
        x_death: The x-death header value (list of dicts) or None if absent.
        max_retries: Maximum retries before permanent failure (default 3).

    Returns:
        Tuple of (should_retry: bool, retry_count: int, delay_ms: int).
        delay_ms is 0 if should_retry is False.
    """
    if x_death and len(x_death) > 0:
        retry_count = x_death[0].get("count", 1)
    else:
        retry_count = 0

    if retry_count < max_retries:
        delay_ms = int((2 ** retry_count) * 1000)
        return (True, retry_count, delay_ms)
    else:
        return (False, retry_count, 0)


def test_first_failure_retries():
    """First failure (count=1) must retry with 2s delay."""
    retry, count, delay = should_retry([{"count": 1, "reason": "rejected"}])
    assert retry is True
    assert count == 1
    assert delay == 2000


def test_second_failure_retries():
    """Second failure (count=2) must retry with 4s delay."""
    retry, count, delay = should_retry([{"count": 2, "reason": "expired"}])
    assert retry is True
    assert count == 2
    assert delay == 4000


def test_third_failure_is_permanent():
    """Third failure (count=3) must NOT retry."""
    retry, count, delay = should_retry([{"count": 3, "reason": "rejected"}])
    assert retry is False
    assert count == 3
    assert delay == 0


def test_count_4_is_permanent():
    """Beyond max retries must NOT retry."""
    retry, count, delay = should_retry([{"count": 5, "reason": "expired"}])
    assert retry is False
    assert count == 5
    assert delay == 0


def test_no_x_death_header_retries_as_zero():
    """Missing x-death header treats as retry count 0 (first attempt)."""
    retry, count, delay = should_retry(None)
    assert retry is True
    assert count == 0
    assert delay == 1000


def test_empty_x_death_retries_as_zero():
    """Empty x-death array treats as retry count 0 (first attempt)."""
    retry, count, delay = should_retry([])
    assert retry is True
    assert count == 0
    assert delay == 1000
