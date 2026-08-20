"""Minimal web interface for security digest."""

import datetime
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from src.dedupe import clear_seen
from src.history import load_entries
from src.llm_models import catalog, is_valid_model
from src.main import run
from src.recipients import ALL, topics_for_user, valid_email
from src.routing import accepts_feed, routing_matrix
from src.summariser import categories, domains, prompt_vocabulary_drift
from src.status import get as get_status
from src.usage import daily as daily_usage
from src.utils import PROJECT_ROOT, slug

log = logging.getLogger(__name__)


class _QuietLivenessProbeFilter(logging.Filter):
    """Drop successful GET /status lines from uvicorn's access log.

    /status is polled by the container healthcheck every 30s (2,880 lines a day
    per instance) and by the dashboard every 60s, or every 3s while a run is in
    progress. A 200 on a liveness endpoint carries no information, and the flood
    buries the lines that do: the scheduler silently skipped the daily digest for
    three months (16 runs) and the only trace -- one WARNING per missed run --
    was invisible among them.

    Anything that is not a plain successful GET /status still logs, so a probe
    that starts failing is as visible as it ever was. Set
    DIGEST_LOG_HEALTHCHECKS=1 to keep them all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access logs '%s - "%s %s HTTP/%s" %d' with exactly these five.
        # Anything else is a record we don't recognise: keep it rather than risk
        # silently swallowing logs this filter was never meant to touch.
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, path, _http_version, status = args
        try:
            ok = int(status) < 400
        except (TypeError, ValueError):
            return True
        return not (method == "GET" and str(path).split("?", 1)[0] == "/status" and ok)


if os.environ.get("DIGEST_LOG_HEALTHCHECKS", "") not in ("1", "true", "yes"):
    # Installed at import time. uvicorn applies its own dictConfig while building
    # its Config, before it imports this module to load the app, so a filter
    # added here survives rather than being reset by that config.
    logging.getLogger("uvicorn.access").addFilter(_QuietLivenessProbeFilter())


def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Gate admin endpoints behind a shared token. Fails closed if unset."""
    expected = os.environ.get("DIGEST_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Admin disabled: set DIGEST_ADMIN_TOKEN in .env to enable admin actions",
        )
    if not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")


admin_auth = Depends(require_admin)

_scheduler: BackgroundScheduler | None = None


def _scheduled_run():
    """Called by scheduler at configured time."""
    log.info("Scheduled run triggered")
    _run_background(digest_slug=None)


def _log_job_problem(event):
    """Report a scheduled run that never happened.

    Without this the only trace is APScheduler's own WARNING, which on a busy
    instance is buried under healthcheck request logs -- so a digest that
    silently stopped being produced looks identical to one nobody read.
    """
    if event.code == EVENT_JOB_MISSED:
        log.error("Scheduled digest run was missed and did not execute")
    else:
        log.error("Scheduled digest run raised", exc_info=event.exception)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler on startup, stop on shutdown."""
    global _scheduler
    config = _load_config()
    schedule_cfg = config.get("schedule", {})
    if schedule_cfg.get("enabled", False):
        _scheduler = BackgroundScheduler()
        hour = schedule_cfg.get("hour", 7)
        minute = schedule_cfg.get("minute", 0)
        tz = schedule_cfg.get("timezone") or "UTC"
        _scheduler.add_job(
            _scheduled_run,
            "cron",
            hour=hour,
            minute=minute,
            timezone=tz,
            # APScheduler's default misfire_grace_time is 1 second: if the host
            # is loaded enough that the scheduler thread wakes even a second
            # late, the day's digest is cancelled outright instead of running
            # late. On a NAS sharing CPU with backup and scan jobs that happens
            # regularly, and it costs a whole day's digest each time. A daily
            # digest is worth running however late the wakeup was, so never
            # treat one as too late to run.
            misfire_grace_time=None,
            # If several fires ever come due together (clock step, resume from
            # suspend), produce one digest rather than a burst of them.
            coalesce=True,
            max_instances=1,
        )
        _scheduler.add_listener(_log_job_problem, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        _scheduler.start()
        log.info("Scheduler started: digest will run daily at %02d:%02d %s", hour, minute, tz)
    yield
    if _scheduler:
        _scheduler.shutdown()


app = FastAPI(title="Security Digest", lifespan=lifespan)

_run_lock = threading.Lock()
_run_in_progress = False


def _load_config():
    from src.fetcher import load_config
    return load_config(PROJECT_ROOT / "config.yaml")


def _run_background(digest_slug: str | None = None):
    global _run_in_progress
    with _run_lock:
        if _run_in_progress:
            return
        _run_in_progress = True
    try:
        run(digest_filter=[digest_slug] if digest_slug else None)
    finally:
        with _run_lock:
            _run_in_progress = False


@app.get("/", response_class=HTMLResponse)
def index():
    html = Path(__file__).parent / "index.html"
    return html.read_text()


@app.get("/history", response_class=HTMLResponse)
def history_page():
    html = Path(__file__).parent / "history.html"
    return html.read_text()


@app.get("/api/history")
def api_history(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    digest: str | None = Query(None),
):
    """Paginated digest item history (newest first)."""
    config = _load_config()
    items, total = load_entries(config, limit=limit, offset=offset, digest_slug=digest)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/site")
def site_info():
    """Dashboard branding for this instance. Unauthenticated: it is the name on
    the page, which anyone who can reach the page already sees."""
    site = _load_config().get("site") or {}
    return {
        "title": site.get("title", "Security Digest"),
        "tagline": site.get("tagline", "Curated news for security consultants"),
        "icon": site.get("icon", "🛡"),
    }


@app.get("/api/usage")
def api_usage(days: int = Query(30, ge=1, le=400)):
    """Daily token consumption for this instance. Unauthenticated, like /status
    and /api/history -- it is operational data about a LAN-only app."""
    return {"days": days, "daily": daily_usage(days)}


@app.get("/api/digests")
def digests_info():
    """Return digest metadata: title, slug (for web page display)."""
    config = _load_config()
    digests = config.get("digests", [])
    return [{"title": d.get("title", "Digest"), "slug": slug(d.get("title", "Digest"))} for d in digests]


def _sources_overrides_path() -> Path:
    rel = _load_config().get("sources_overrides_file", "data/sources_overrides.yaml")
    return PROJECT_ROOT / rel


def _llm_overrides_path() -> Path:
    rel = _load_config().get("llm_overrides_file", "data/llm_overrides.yaml")
    return PROJECT_ROOT / rel


def _topics_path() -> Path:
    """The live topic list. Writable (under data/), so the panel edits it in
    place; the instance's topics.yaml only seeds a brand-new instance."""
    rel = _load_config().get("topics_file", "data/topics.yaml")
    return PROJECT_ROOT / rel


def _users_path() -> Path:
    """The one place recipients live. Writable (it is under data/), so the panel
    edits it in place -- no base/override pair, nothing to reconcile."""
    rel = _load_config().get("users_file", "data/users.yaml")
    return PROJECT_ROOT / rel


def _digests_for_source_name(config: dict, source_name: str) -> list[str]:
    """Full digest titles this feed currently reaches.

    Full titles, not the abbreviated labels this used to return: the editor now
    renders one checkbox per digest and ticks it by matching this value against
    the digest's title, so a shortened "AI Security" would match nothing, leave
    every box clear, and let the next Save write away all the routing."""
    if not source_name:
        return []
    return [
        str(d.get("title", ""))
        for d in (config.get("digests") or [])
        if accepts_feed(d, source_name)
    ]


def _sources_overrides_active() -> bool:
    """True when data/sources_overrides.yaml exists and supplies an rss list (replaces sources.yaml)."""
    path = _sources_overrides_path()
    if not path.exists():
        return False
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return False
    return isinstance(raw, dict) and isinstance(raw.get("rss"), list)


@app.get("/admin/sources", dependencies=[admin_auth])
def admin_get_sources():
    """RSS feeds from merged config (base sources.yaml + optional overrides)."""
    config = _load_config()
    rss = config.get("sources", {}).get("rss") or []
    ov_active = _sources_overrides_active()
    if not isinstance(rss, list):
        return {"rss": [], "overrides_active": ov_active}
    out: list[dict[str, Any]] = []
    for x in rss:
        if isinstance(x, dict) and x.get("name") and x.get("url"):
            name = str(x["name"]).strip()
            out.append({
                "name": name,
                "url": str(x["url"]).strip(),
                "digests": _digests_for_source_name(config, name),
            })
    return {
        "rss": out,
        "overrides_active": ov_active,
        # Every digest a feed may be routed to, so the editor can offer them as
        # checkboxes instead of the reader having to match names by hand.
        "all_digests": [str(d.get("title", "")) for d in (config.get("digests") or [])],
    }


@app.post("/admin/sources", dependencies=[admin_auth])
def admin_save_sources(body: dict = Body(...)):
    """Replace rss list in writable sources overrides file."""
    rss = body.get("rss")
    if not isinstance(rss, list):
        return JSONResponse({"ok": False, "message": "rss must be a list"}, status_code=400)
    # Saving an empty list writes an override that replaces the whole feed list
    # with nothing, silently emptying the digest on the next run. Easy to do by
    # accident: the editor renders one blank row when there are no feeds, so a
    # stray click on Save wipes the instance. Require it to be deliberate.
    if not rss and not body.get("allow_empty"):
        return JSONResponse(
            {"ok": False, "message": "Refusing to save an empty feed list -- that would "
                                     "stop every RSS-based digest on this instance. Remove "
                                     "sources.yaml's feeds instead if that's the intent."},
            status_code=400,
        )
    known_digests = {str(d.get("title", "")) for d in (_load_config().get("digests") or [])}
    cleaned: list[dict[str, Any]] = []
    for i, x in enumerate(rss):
        if not isinstance(x, dict):
            return JSONResponse({"ok": False, "message": f"Row {i + 1}: invalid entry"}, status_code=400)
        name = (x.get("name") or "").strip()
        url = (x.get("url") or "").strip()
        if not name or not url:
            return JSONResponse(
                {"ok": False, "message": f"Row {i + 1}: name and URL are required"},
                status_code=400,
            )
        if not url.startswith(("http://", "https://")):
            return JSONResponse(
                {"ok": False, "message": f"Row {i + 1}: URL must start with http:// or https://"},
                status_code=400,
            )
        # Routing lives on the feed now, so it has to survive a save. Dropping
        # it here would leave every digest with no `sources` -- which does not
        # fail, it silently means "accept every feed", turning three curated
        # digests into three copies of the same firehose.
        row: dict[str, Any] = {"name": name, "url": url}
        if isinstance(x.get("digests"), list):
            titles = [str(t).strip() for t in x["digests"] if str(t).strip()]
            unknown = [t for t in titles if t not in known_digests]
            if unknown:
                return JSONResponse(
                    {"ok": False, "message": f"Row {i + 1}: no digest named "
                                             f"{', '.join(repr(u) for u in unknown)}"},
                    status_code=400,
                )
            row["digests"] = titles
        cleaned.append(row)
    path = _sources_overrides_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump({"rss": cleaned}, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
        log.info("Wrote %d RSS feeds to %s", len(cleaned), path)
        return {"ok": True, "message": f"Saved {len(cleaned)} feeds ({path.name})"}
    except OSError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.post("/admin/sources/reset-overrides", dependencies=[admin_auth])
def admin_reset_sources_overrides():
    """Remove sources overrides file so feeds come from sources.yaml again."""
    path = _sources_overrides_path()
    try:
        if path.exists():
            path.unlink()
            log.info("Removed RSS overrides file %s", path)
        return {"ok": True, "message": "Feed overrides removed; using sources.yaml."}
    except OSError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/admin/topics", dependencies=[admin_auth])
def admin_get_topics():
    """Tracked topics from merged config (topics.yaml + optional overrides).

    `digests` per topic mirrors the RSS editor's read-only column: routing is
    matched on the topic name, so seeing which digests pick a topic up is the
    only way to catch a name that reaches nobody."""
    config = _load_config()
    topics = config.get("topics") or []
    out: list[dict[str, Any]] = []
    for t in topics:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        name = str(t["name"]).strip()
        queries = t.get("queries") or []
        if isinstance(queries, str):
            queries = [queries]
        out.append({
            "name": name,
            "queries": [str(q) for q in queries],
            "context": str(t.get("context") or "").strip(),
            "lang": str(t.get("lang") or "").strip(),
            "country": str(t.get("country") or "").strip(),
            "recipient": str(t.get("recipient") or ALL).strip(),
            "digests": _digests_for_source_name(config, name),
        })
    # users lets the Topics card render its recipient dropdown without a second
    # request, and keeps the two views consistent within one page load.
    return {"topics": out, "users": config.get("users") or []}


@app.post("/admin/topics", dependencies=[admin_auth])
def admin_save_topics(body: dict = Body(...)):
    """Replace the topic list in the writable topics overrides file."""
    topics = body.get("topics")
    if not isinstance(topics, list):
        return JSONResponse({"ok": False, "message": "topics must be a list"}, status_code=400)

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, t in enumerate(topics):
        if not isinstance(t, dict):
            return JSONResponse({"ok": False, "message": f"Row {i + 1}: invalid entry"}, status_code=400)
        name = (t.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "message": f"Row {i + 1}: name is required"}, status_code=400)
        # Two topics with one name would produce two sets of feeds routing to the
        # same digests -- duplicate items, doubled LLM spend, no way to tell them
        # apart afterwards.
        if name.casefold() in seen:
            return JSONResponse(
                {"ok": False, "message": f"Row {i + 1}: duplicate topic name {name!r}"},
                status_code=400,
            )
        seen.add(name.casefold())

        raw_queries = t.get("queries") or []
        if isinstance(raw_queries, str):
            raw_queries = [q for q in raw_queries.split("\n")]
        queries = [str(q).strip() for q in raw_queries if str(q).strip()]

        entry: dict[str, Any] = {"name": name}
        if queries:
            entry["queries"] = queries
        for key in ("context", "lang", "country"):
            value = (t.get(key) or "").strip()
            if value:
                entry[key] = value

        recipient = (t.get("recipient") or ALL).strip()
        if recipient.casefold() != ALL:
            known = {u["email"].casefold() for u in (_load_config().get("users") or [])}
            if recipient.casefold() not in known:
                return JSONResponse(
                    {"ok": False, "message": f"Row {i + 1}: {recipient!r} is not a known "
                                             f"recipient -- add them under Recipients first"},
                    status_code=400,
                )
        entry["recipient"] = recipient
        cleaned.append(entry)

    path = _topics_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump({"topics": cleaned}, default_flow_style=False, allow_unicode=True,
                      sort_keys=False, width=120),
            encoding="utf-8",
        )
        log.info("Wrote %d topics to %s", len(cleaned), path)
        return {"ok": True, "message": f"Saved {len(cleaned)} topics ({path.name})"}
    except OSError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/admin/panel", dependencies=[admin_auth])
def admin_panel():
    """Which admin cards apply to this instance.

    One page serves every instance, but the instances are not alike: a
    feed-following digest has no recipients and no topics, and a topic tracker
    has no RSS feeds. Showing a card that cannot do anything here -- even with a
    note explaining why -- is just clutter to read past every visit, so the page
    asks first and omits what does not apply.

    The rules are deliberately self-correcting rather than hard-coded per
    instance: add a feed to a topic instance and its RSS card appears; add a
    topic to a feed instance and its Topics card appears."""
    config = _load_config()
    derived = bool(config.get("digests_are_derived", True))
    has_topics = bool(config.get("topics"))
    has_feeds = bool((config.get("sources") or {}).get("rss"))
    return {
        # Recipients only drive delivery where digests are derived from them.
        "recipients": derived,
        # Shown when topics are in use, or could be: a brand-new topic instance
        # has none yet and still needs somewhere to add the first one.
        "topics": derived or has_topics,
        # Same, mirrored: a feed instance with no feeds yet still needs the editor.
        "feeds": (not derived) or has_feeds,
    }


@app.get("/admin/routing", dependencies=[admin_auth])
def admin_routing():
    """The feed x category grid, and which pairs reach no digest.

    Delivery needs the item's category AND its feed to be accepted, so the unit
    that matters is the pair. The per-feed warning could only say "this feed
    goes nowhere" -- true of two feeds while nine others were each losing a whole
    category in silence. Showing the grid is the only way that reads at a
    glance."""
    config = _load_config()
    matrix = routing_matrix(config)
    matrix["digests"] = [
        {"title": d.get("title", ""), "sections": d.get("sections") or []}
        for d in (config.get("digests") or [])
    ]
    return matrix


@app.get("/admin/users", dependencies=[admin_auth])
def admin_get_users():
    """Recipients of this instance, with the topics each currently receives."""
    config = _load_config()
    users = config.get("users") or []
    topics = config.get("topics") or []
    return {
        "users": [
            {**u, "topics": topics_for_user(topics, u["email"])}
            for u in users
        ],
        "derived": bool(config.get("digests_are_derived", True)),
        "digests": [d.get("title", "") for d in (config.get("digests") or [])],
    }


@app.post("/admin/users", dependencies=[admin_auth])
def admin_save_users(body: dict = Body(...)):
    """Replace the recipient list in the writable users overrides file."""
    users = body.get("users")
    if not isinstance(users, list):
        return JSONResponse({"ok": False, "message": "users must be a list"}, status_code=400)
    if not _load_config().get("digests_are_derived", True):
        return JSONResponse(
            {"ok": False, "message": "This instance declares its own digests in config.yaml, "
                                     "so recipients are not used here -- delivery goes to "
                                     "delivery.email.to. Saving recipients would have no effect."},
            status_code=400,
        )

    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, u in enumerate(users):
        if not isinstance(u, dict):
            return JSONResponse({"ok": False, "message": f"Row {i + 1}: invalid entry"}, status_code=400)
        email = (u.get("email") or "").strip()
        name = (u.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "message": f"Row {i + 1}: name is required"}, status_code=400)
        if not valid_email(email):
            return JSONResponse(
                {"ok": False, "message": f"Row {i + 1}: {email!r} is not a valid email address"},
                status_code=400,
            )
        if email.casefold() in seen:
            return JSONResponse(
                {"ok": False, "message": f"Row {i + 1}: duplicate email {email!r}"},
                status_code=400,
            )
        seen.add(email.casefold())
        cleaned.append({"name": name, "email": email})

    # Removing a recipient orphans any topic addressed to them: still fetched and
    # summarised, delivered to nobody. Name them rather than silently dropping.
    config = _load_config()
    orphaned = [
        str(t.get("name"))
        for t in (config.get("topics") or [])
        if isinstance(t, dict)
        and str(t.get("recipient") or ALL).strip().casefold() not in
        ({ALL} | {e.casefold() for e in seen})
    ]

    path = _users_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump({"users": cleaned}, default_flow_style=False, allow_unicode=True,
                      sort_keys=False, width=120),
            encoding="utf-8",
        )
        log.info("Wrote %d recipients to %s", len(cleaned), path)
        message = f"Saved {len(cleaned)} recipient(s) ({path.name})"
        if orphaned:
            message += (
                f". Warning: {', '.join(orphaned)} "
                f"{'is' if len(orphaned) == 1 else 'are'} now addressed to nobody -- "
                f"reassign in Topics."
            )
        return {"ok": True, "message": message, "orphaned_topics": orphaned}
    except OSError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.post("/run", dependencies=[admin_auth])
def trigger_run(digest: str | None = Query(None)):
    """Run pipeline. Omit ?digest= to run all digests; use ?digest=security-digest for one."""
    global _run_in_progress
    config = _load_config()
    interval_min = int(config.get("web", {}).get("min_run_interval_minutes", 30))
    status = get_status()
    last_run = status.get("last_run")
    if last_run:
        try:
            last = datetime.datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            now = datetime.datetime.now(last.tzinfo) if last.tzinfo else datetime.datetime.now()
            if (now - last).total_seconds() < interval_min * 60:
                return JSONResponse(
                    {"ok": False, "message": f"Wait {interval_min} minutes between runs"},
                    status_code=429,
                )
        except (ValueError, TypeError):
            pass
    with _run_lock:
        if _run_in_progress:
            return JSONResponse(
                {"ok": False, "message": "Run already in progress"},
                status_code=409,
            )
        thread = threading.Thread(target=_run_background, kwargs={"digest_slug": digest})
        thread.start()
    return {"ok": True, "message": "Run started"}


@app.get("/status")
def status():
    return get_status()


@app.get("/digests")
def list_digests():
    base = PROJECT_ROOT / "output" / "web"
    if not base.exists():
        return []
    return [f.stem for f in base.glob("*.html")]


ALLOWED_PROMPTS = {"summarise.txt", "summarise_batch.txt", "cluster.txt", "digest.txt"}


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    html = Path(__file__).parent / "admin.html"
    return html.read_text()


@app.post("/admin/flush", dependencies=[admin_auth])
def admin_flush():
    """Clear the seen store so items can be processed again."""
    try:
        clear_seen()
        return {"ok": True, "message": "Seen store cleared"}
    except (OSError, sqlite3.Error) as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/admin/llm", dependencies=[admin_auth])
def admin_get_llm():
    """Current LLM settings and model catalog for admin UI."""
    config = _load_config()
    llm = config.get("llm", {})
    return {
        "provider": llm.get("provider", "openai"),
        "model": llm.get("model", "gpt-5.6-luna"),
        "temperature": llm.get("temperature", 0.3),
        "models": catalog(),
    }


@app.post("/admin/llm", dependencies=[admin_auth])
def admin_set_llm(body: dict = Body(...)):
    """Update llm.provider and llm.model in writable LLM overrides file."""
    provider = (body.get("provider") or "").strip().lower()
    model = (body.get("model") or "").strip()
    if provider not in ("openai", "anthropic", "mistral", "openrouter"):
        return JSONResponse(
            {"ok": False, "message": "provider must be openai, anthropic, mistral or openrouter"},
            status_code=400,
        )
    if not is_valid_model(provider, model):
        return JSONResponse(
            {"ok": False, "message": "Unknown model for provider (not in the curated list, and the provider's API didn't recognise it either)"},
            status_code=400,
        )
    path = _llm_overrides_path()
    try:
        data: dict[str, Any] = {}
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                data = loaded
        if data is None:
            data = {}
        if "llm" not in data:
            data["llm"] = {}
        data["llm"]["provider"] = provider
        data["llm"]["model"] = model
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        return {"ok": True, "message": f"Set {provider} / {model} (saved to {path.name})"}
    except yaml.YAMLError as e:
        return JSONResponse({"ok": False, "message": f"Invalid YAML: {e}"}, status_code=500)
    except OSError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/admin/prompts", dependencies=[admin_auth])
def admin_list_prompts():
    """Prompt files, plus the vocabulary the API will enforce on their output.

    The prompt is where the categories and domains are explained to the model,
    but config.yaml is what makes them a schema enum. Editing one without the
    other produces no error -- just a digest where everything has quietly been
    coerced into the fallback section. Showing the enforced values next to the
    editor is what makes that visible before it happens rather than after."""
    config = _load_config()
    return {
        "prompts": sorted(ALLOWED_PROMPTS),
        "categories": categories(config),
        "domains": domains(config),
        "drift": prompt_vocabulary_drift(config),
    }


@app.get("/admin/prompts/{name}", dependencies=[admin_auth])
def admin_get_prompt(name: str):
    if name not in ALLOWED_PROMPTS:
        return JSONResponse({"error": "Not found"}, status_code=404)
    path = PROJECT_ROOT / "prompts" / name
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@app.post("/admin/prompts/{name}", dependencies=[admin_auth])
def admin_save_prompt(name: str, body: dict = Body(...)):
    content = body.get("content", "")
    if name not in ALLOWED_PROMPTS:
        return JSONResponse({"error": "Not found"}, status_code=404)
    path = PROJECT_ROOT / "prompts" / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "message": f"Saved {name}"}
    except OSError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/digests/{name}")
def get_digest(name: str):
    if ".." in name or "/" in name:
        return JSONResponse({"error": "Not found"}, status_code=404)
    base = PROJECT_ROOT / "output" / "web"
    path = base / f"{name}.html"
    if not path.exists():
        # Recover the digest's real title from config rather than un-slugging the
        # filename, which drops punctuation and mangles case ("Alice's News" ->
        # "Alices News").
        title = next(
            (d.get("title") for d in (_load_config().get("digests") or [])
             if slug(d.get("title", "")) == name),
            name.replace("-", " ").title(),
        )
        return HTMLResponse(
            f'<div style="padding:24px;color:#94a3b8;font-family:system-ui,sans-serif;background:#0f172a;min-height:100vh;display:flex;align-items:center;justify-content:center;">'
            f'No {title} yet. Run the digest to generate.</div>'
        )
    return FileResponse(path, media_type="text/html")


def serve(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    serve()
