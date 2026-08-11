"""Tests for src.retry: exponential backoff and the non_retryable escape hatch
added in task 2.9."""

import pytest

from src.retry import retry


def test_retry_succeeds_on_first_try():
    assert retry(lambda: 42) == 42


def test_retry_retries_transient_failures_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flaky")
        return "ok"

    assert retry(flaky, max_retries=3, initial_delay=0.001) == "ok"
    assert calls["n"] == 3


def test_retry_raises_after_exhausting_retries():
    def always_fails():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        retry(always_fails, max_retries=2, initial_delay=0.001)


def test_retry_non_retryable_fails_immediately():
    calls = {"n": 0}

    class AuthError(Exception):
        pass

    def raises_auth():
        calls["n"] += 1
        raise AuthError("bad key")

    with pytest.raises(AuthError):
        retry(raises_auth, max_retries=5, initial_delay=0.001, non_retryable=(AuthError,))
    assert calls["n"] == 1


def test_retry_non_retryable_does_not_affect_other_exceptions():
    calls = {"n": 0}

    class AuthError(Exception):
        pass

    def raises_other():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("transient")
        return "ok"

    assert retry(raises_other, max_retries=3, initial_delay=0.001, non_retryable=(AuthError,)) == "ok"
    assert calls["n"] == 2
