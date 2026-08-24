"""Tests for per-feed health: is this feed working, and since when isn't it.

The bug this exists to end is a silent one. fetch_feed() logs one WARNING for a
dead feed and returns [], the run succeeds, the digest is a little thinner, and
nothing anywhere says a source has stopped. Threatpost and CSO Online sat dead
in sources.yaml for months. So the cases that matter most below are the ones
where nothing raises: a feed that fetches fine and is empty, and a feed that
fetches fine, has entries, and has published nothing in a month.
"""

import datetime

import pytest

import src.feed_health as fh
from src.feed_health import EMPTY, ERROR, OK, QUIET, UNKNOWN, describe, load, prune, record

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _ago(days=0, hours=0):
    return (NOW - datetime.timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def _row(db, url="https://e.com/f", **kw):
    """Store one outcome and read the row back."""
    record([{"url": url, "name": "Feed", **kw}], db)
    return load(db)[url]


# --- recording --------------------------------------------------------------


def test_a_healthy_fetch_is_recorded(tmp_path):
    db = tmp_path / "digest.db"
    row = _row(db, status=OK, items=8, newest=_ago(hours=3))
    assert row["status"] == OK and row["items"] == 8
    assert row["ok_at"] and row["failing_since"] is None


def test_a_failure_starts_a_streak(tmp_path):
    db = tmp_path / "digest.db"
    row = _row(db, status=ERROR, detail="HTTP 404")
    assert row["failing_since"] == row["checked_at"]
    assert row["ok_at"] is None


def test_the_streak_keeps_its_start_across_runs(tmp_path):
    """"Failing 12d" is the whole value of the column, and it is only knowable
    by remembering when the run of failures began."""
    db = tmp_path / "digest.db"
    record([{"url": "u", "name": "F", "status": ERROR, "detail": "HTTP 404"}], db)
    started = load(db)["u"]["failing_since"]
    for _ in range(3):
        record([{"url": "u", "name": "F", "status": ERROR, "detail": "HTTP 404"}], db)
    after = load(db)["u"]
    assert after["failing_since"] == started
    assert after["checked_at"] >= started


def test_recovery_clears_the_streak_and_keeps_the_last_good_time(tmp_path):
    db = tmp_path / "digest.db"
    record([{"url": "u", "name": "F", "status": ERROR}], db)
    record([{"url": "u", "name": "F", "status": OK, "items": 5}], db)
    row = load(db)["u"]
    assert row["failing_since"] is None and row["ok_at"] == row["checked_at"]


def test_a_failure_after_recovery_remembers_when_it_last_worked(tmp_path):
    db = tmp_path / "digest.db"
    record([{"url": "u", "name": "F", "status": OK, "items": 5}], db)
    good = load(db)["u"]["ok_at"]
    record([{"url": "u", "name": "F", "status": ERROR, "detail": "timed out"}], db)
    row = load(db)["u"]
    assert row["ok_at"] == good and row["failing_since"] is not None


def test_renaming_a_feed_keeps_its_history(tmp_path):
    """Rows are keyed by URL precisely so that editing the display name in the
    admin panel does not reset the failure streak to zero."""
    db = tmp_path / "digest.db"
    record([{"url": "u", "name": "Old name", "status": ERROR}], db)
    started = load(db)["u"]["failing_since"]
    record([{"url": "u", "name": "New name", "status": ERROR}], db)
    row = load(db)["u"]
    assert row["name"] == "New name" and row["failing_since"] == started


def test_recording_nothing_is_a_no_op(tmp_path):
    db = tmp_path / "digest.db"
    record([], db)
    assert load(db) == {}


def test_rows_without_a_url_are_skipped(tmp_path):
    db = tmp_path / "digest.db"
    record([{"name": "no url", "status": OK}, {"url": "u", "name": "F", "status": OK}], db)
    assert list(load(db)) == ["u"]


def test_pruning_forgets_feeds_that_are_gone(tmp_path):
    """Changing a feed's URL would otherwise leave a red row for a feed nobody
    has any more."""
    db = tmp_path / "digest.db"
    record([{"url": "old", "name": "F", "status": ERROR},
            {"url": "new", "name": "F", "status": OK}], db)
    prune(["new"], db)
    assert list(load(db)) == ["new"]


def test_pruning_to_nothing_is_refused(tmp_path):
    """An empty list means "no feeds were fetched", which is a fetch problem --
    not a reason to throw away every feed's history."""
    db = tmp_path / "digest.db"
    record([{"url": "u", "name": "F", "status": OK}], db)
    prune([], db)
    assert list(load(db)) == ["u"]


# --- what the column says ---------------------------------------------------


def test_a_feed_never_fetched_says_so_rather_than_looking_healthy(tmp_path):
    d = describe(None, NOW)
    assert d["status"] == UNKNOWN and d["summary"] == "never checked"


def test_a_healthy_feed_reads_as_ok(tmp_path):
    db = tmp_path / "digest.db"
    d = describe(_row(db, status=OK, items=8, newest=_ago(hours=3)), NOW)
    assert d["status"] == OK
    assert d["summary"].startswith("OK · 8 items · checked")


def test_a_single_item_is_not_pluralised(tmp_path):
    db = tmp_path / "digest.db"
    assert "1 item ·" in describe(_row(db, status=OK, items=1, newest=_ago()), NOW)["summary"]


def test_a_broken_feed_leads_with_the_reason_and_the_duration(tmp_path):
    db = tmp_path / "digest.db"
    record([{"url": "u", "name": "F", "status": ERROR, "detail": "HTTP 404"}], db)
    row = load(db)["u"]
    row["failing_since"] = _ago(days=12)
    d = describe(row, NOW)
    assert d["status"] == ERROR and d["summary"] == "HTTP 404 · failing 12d"


def test_a_failure_that_started_today_says_today(tmp_path):
    db = tmp_path / "digest.db"
    d = describe(_row(db, status=ERROR, detail="timed out"), NOW)
    assert d["summary"] == "timed out · failing today"


def test_an_empty_feed_is_a_failure_not_a_quiet_success(tmp_path):
    """HTTP 200 and zero entries is a broken feed, and nothing logs it."""
    db = tmp_path / "digest.db"
    d = describe(_row(db, status=EMPTY), NOW)
    assert d["status"] == EMPTY and d["summary"].startswith("no entries · failing")


def test_a_feed_that_fetches_fine_but_has_stopped_publishing_is_quiet(tmp_path):
    """The Threatpost case: 200 OK, valid XML, entries present, all of them old.
    Nothing errors and nothing warns, so only this makes it visible."""
    db = tmp_path / "digest.db"
    d = describe(_row(db, status=OK, items=10, newest=_ago(days=40)), NOW)
    assert d["status"] == QUIET
    assert d["summary"] == "10 items, newest 40d old · checked just now"


def test_a_merely_slow_week_is_not_called_quiet(tmp_path):
    db = tmp_path / "digest.db"
    assert describe(_row(db, status=OK, items=4, newest=_ago(days=6)), NOW)["status"] == OK


def test_the_quiet_threshold_is_read_not_baked_in(tmp_path):
    """Derived at read time, so changing it re-reads what is already stored
    rather than needing a fresh run against every feed."""
    db = tmp_path / "digest.db"
    row = _row(db, status=OK, items=4, newest=_ago(days=10))
    assert describe(row, NOW, quiet_after_days=21)["status"] == OK
    assert describe(row, NOW, quiet_after_days=7)["status"] == QUIET


def test_a_feed_whose_entries_carry_no_dates_is_never_called_quiet(tmp_path):
    """A feed that omits the field is not a publisher that has stopped, and
    crying wolf about it would train you to ignore the column."""
    db = tmp_path / "digest.db"
    d = describe(_row(db, status=OK, items=6, newest=None), NOW)
    assert d["status"] == OK and d["stale_days"] is None


def test_an_unreadable_stored_date_does_not_blow_up_the_page(tmp_path):
    db = tmp_path / "digest.db"
    d = describe(_row(db, status=OK, items=2, newest="not-a-date"), NOW)
    assert d["status"] == OK


@pytest.mark.parametrize("delta,expected", [
    ({"seconds": 30}, "just now"),
    ({"minutes": 20}, "20m ago"),
    ({"hours": 5}, "5h ago"),
    ({"days": 3}, "3d ago"),
])
def test_relative_times_avoid_the_timezone_trap(delta, expected):
    """A wall-clock time would be the container's UTC read as the browser's
    local, which is wrong by an hour or two and silently so."""
    assert fh._ago((NOW - datetime.timedelta(**delta)).isoformat(), NOW) == expected


# --- error phrasing ---------------------------------------------------------


def test_an_http_error_is_named_by_its_status_code():
    import httpx

    exc = httpx.HTTPStatusError(
        "Client error '404 Not Found' for url 'https://e.com'\\nFor more information…",
        request=httpx.Request("GET", "https://e.com"),
        response=httpx.Response(404),
    )
    assert fh.error_detail(exc) == "HTTP 404"


def test_a_timeout_says_so():
    import httpx

    assert fh.error_detail(httpx.ConnectTimeout("timed out")) == "timed out"


def test_a_long_message_is_cut_to_something_a_column_can_hold():
    detail = fh.error_detail(ValueError("x" * 500))
    assert len(detail) <= 61 and detail.endswith("…")
