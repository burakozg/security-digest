"""Tests for the web app's daily scheduler wiring.

The digest runs from an in-process APScheduler job, so how that job is
registered is the whole difference between "the digest is late" and "there is
no digest today".
"""

import asyncio

import src.web.app as app_module


def _register_job(monkeypatch, schedule):
    """Run the real lifespan with a stubbed config and return the job it added."""
    monkeypatch.setattr(app_module, "_load_config", lambda: {"schedule": schedule})
    monkeypatch.setattr(app_module, "_scheduler", None)

    async def drive():
        async with app_module.lifespan(app_module.app):
            sched = app_module._scheduler
            return sched.get_jobs()[0] if sched else None

    return asyncio.run(drive())


def test_late_wakeup_still_runs_the_digest(monkeypatch):
    """A daily digest is worth running late; APScheduler's default grace of one
    second cancels it outright instead, which cost whole days of digests on a
    loaded host."""
    job = _register_job(monkeypatch, {"enabled": True, "hour": 7, "minute": 0})
    assert job.misfire_grace_time is None


def test_pileup_produces_one_run_not_a_burst(monkeypatch):
    job = _register_job(monkeypatch, {"enabled": True, "hour": 7, "minute": 0})
    assert job.coalesce is True
    assert job.max_instances == 1


def test_job_fires_at_the_configured_local_time(monkeypatch):
    job = _register_job(
        monkeypatch,
        {"enabled": True, "hour": 7, "minute": 30, "timezone": "Europe/Stockholm"},
    )
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "7"
    assert fields["minute"] == "30"
    assert str(job.trigger.timezone) == "Europe/Stockholm"


def test_no_scheduler_when_disabled(monkeypatch):
    assert _register_job(monkeypatch, {"enabled": False}) is None


def test_missed_job_is_logged_as_an_error(monkeypatch, caplog):
    """The failure mode that hid this bug: the only trace of a skipped run was
    APScheduler's own warning, lost among healthcheck request logs."""
    event = type("E", (), {"code": app_module.EVENT_JOB_MISSED, "exception": None})()
    with caplog.at_level("ERROR"):
        app_module._log_job_problem(event)
    assert "missed" in caplog.text.lower()
