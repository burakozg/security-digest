"""Tests for the uvicorn access-log filter that quiets liveness probes.

The healthcheck polls /status every 30s. That flood is what hid a scheduler
that had silently skipped the daily digest 16 times, so the filter has to be
narrow: quiet the probe, keep everything else -- including a probe that fails.
"""

import logging

from src.web.app import _QuietLivenessProbeFilter


def _record(method, path, status):
    """A record shaped exactly like uvicorn.access emits."""
    r = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0,
                          '%s - "%s %s HTTP/%s" %d', None, None)
    r.args = ("127.0.0.1:1234", method, path, "1.1", status)
    return r


def _kept(method, path, status):
    return _QuietLivenessProbeFilter().filter(_record(method, path, status))


def test_successful_status_probe_is_dropped():
    assert _kept("GET", "/status", 200) is False


def test_failing_status_probe_is_kept():
    """The whole point: a probe that starts failing must stay visible."""
    assert _kept("GET", "/status", 500) is True
    assert _kept("GET", "/status", 404) is True


def test_other_paths_are_kept():
    for path in ("/", "/api/digests", "/admin", "/history", "/statuses", "/status/extra"):
        assert _kept("GET", path, 200) is True, path


def test_non_get_to_status_is_kept():
    assert _kept("POST", "/status", 200) is True


def test_query_string_does_not_defeat_the_match():
    assert _kept("GET", "/status?t=1", 200) is False


def test_unrecognised_records_are_kept():
    """Fail open -- never silently swallow a record this filter didn't expect."""
    r = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "something else", None, None)
    assert _QuietLivenessProbeFilter().filter(r) is True

    r2 = _record("GET", "/status", 200)
    r2.args = ("only", "three", "args")
    assert _QuietLivenessProbeFilter().filter(r2) is True

    r3 = _record("GET", "/status", "not-a-status-code")
    assert _QuietLivenessProbeFilter().filter(r3) is True


def test_filter_is_installed_on_the_uvicorn_access_logger():
    """Importing the app must be enough -- there is no wiring step in the
    Dockerfile or compose command to forget."""
    import src.web.app  # noqa: F401

    filters = logging.getLogger("uvicorn.access").filters
    assert any(isinstance(f, _QuietLivenessProbeFilter) for f in filters)
