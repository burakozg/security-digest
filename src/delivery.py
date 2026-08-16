"""Deliver the digest to console, file, or email."""

import datetime

import markdown

from src.retry import retry
from src.utils import PROJECT_ROOT, slug
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)


def deliver_previous(
    html_path: Path, config: dict, title: str, digest_cfg: dict | None = None
) -> None:
    """Deliver a previously saved digest (when no new items)."""
    delivery = config.get("delivery", {})
    output = delivery.get("output", "console")
    content = html_path.read_text(encoding="utf-8")
    # Prepend "no new updates" banner
    banner = '<div class="digest-banner" style="padding:12px;background:#f0f0f0;border-radius:6px;margin-bottom:16px;font-size:0.9em;color:#666">No new updates today — displaying previous digest.</div>'
    if "<body" in content:
        content = content.replace("<body>", f"<body>{banner}", 1)
    else:
        content = banner + content
    # Save to output/web so web UI displays it with banner
    web_dir = PROJECT_ROOT / "output" / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / f"{slug(title)}.html").write_text(content, encoding="utf-8")
    # File output has nothing meaningful to re-send (only update web UI); email
    # re-sends the previous digest so the owner still gets a daily message.
    if output == "console":
        _deliver_console(content[:500] + "\n... (previous digest)")
    elif output == "email":
        _deliver_email_html(content, config, title, no_new_updates=True, digest_cfg=digest_cfg)


def deliver(
    content: str, config: dict, title: str | None = None, digest_cfg: dict | None = None
) -> None:
    """Deliver digest according to config (console, file, or email).

    digest_cfg is the digest's own definition; it carries a `to:` override so
    several digests in one run can reach different recipients."""
    delivery = config.get("delivery", {})
    output = delivery.get("output", "console")

    if output == "console":
        _deliver_console(content)
    elif output == "file":
        _deliver_file(content, delivery, title)
    elif output == "email":
        _deliver_email(content, config, title, digest_cfg)
    else:
        log.warning("Unknown output '%s', falling back to console", output)
        _deliver_console(content)


def _markdown_to_html(md: str) -> str:
    """Convert markdown to HTML with email-friendly styling."""
    html = markdown.markdown(md)
    style = """
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 15px; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }
    h1 { font-size: 1.5em; color: #111; margin-bottom: 0.25em; }
    h2 { font-size: 1.2em; color: #222; margin-top: 1.5em; margin-bottom: 0.5em; border-bottom: 1px solid #eee; padding-bottom: 0.25em; }
    h3 { font-size: 1em; margin: 1em 0 0.25em 0; }
    a { color: #0066cc; text-decoration: none; }
    a:hover { text-decoration: underline; }
    p { margin: 0.5em 0; }
    em { color: #666; font-size: 0.9em; }
    body.dark { background: #0f172a; color: #e2e8f0; }
    body.dark h1, body.dark h2 { color: #f8fafc; border-color: #475569; }
    body.dark h3 { color: #f1f5f9; }
    body.dark p, body.dark li { color: #e2e8f0; }
    body.dark a { color: #7dd3fc; }
    body.dark a:hover { color: #bae6fd; }
    body.dark em { color: #cbd5e1; }
    body.dark strong { color: #f8fafc; }
    body.dark ul, body.dark ol { color: #e2e8f0; }
    body.dark code { background: #1e293b; color: #e2e8f0; padding: 0.15em 0.4em; border-radius: 4px; }
    body.dark hr { border-color: #475569; }
    .digest-banner { padding:12px; background:#f0f0f0; border-radius:6px; margin-bottom:16px; font-size:0.9em; color:#666; }
    body.dark .digest-banner { background:#334155 !important; color:#94a3b8 !important; }
    """
    script = """<script>if(window.self!==window.top)document.body.classList.add('dark');</script>"""
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>{style}</style></head><body>{html}{script}</body></html>'


def _save_for_web(html: str, title: str) -> None:
    """Save HTML digest for web viewing."""
    base = PROJECT_ROOT / "output" / "web"
    base.mkdir(parents=True, exist_ok=True)
    safe_name = slug(title) + ".html"
    (base / safe_name).write_text(html, encoding="utf-8")
    log.info("Saved web copy to output/web/%s", safe_name)


def _deliver_console(content: str) -> None:
    print(content)


def _deliver_file(content: str, delivery: dict, title: str | None = None) -> None:
    base_path = Path(delivery.get("file_path", "./output/digest.md"))
    if title:
        safe_name = slug(title) + ".md"
        path = base_path.parent / safe_name
    else:
        path = base_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("Wrote digest to %s", path)


def _resolve_email_config(
    config: dict, digest_cfg: dict | None = None
) -> tuple[str, int, str, list[str], str, str]:
    """Validate delivery.email config and env credentials. Raises ValueError if
    misconfigured. Returns (smtp_host, smtp_port, from_addr, to_addrs, user, password).

    A digest may name its own recipients via `to:`, which wins over the global
    delivery.email.to -- that's what lets one instance send different topic sets
    to different people. Only the recipient is per-digest; everything sends from
    the same account over the same SMTP connection."""
    delivery = config.get("delivery", {})
    email_cfg = delivery.get("email", {})
    if not email_cfg:
        raise ValueError("delivery.output is 'email' but delivery.email is not configured")

    smtp_host = email_cfg.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(email_cfg.get("smtp_port", 587))

    # Addresses come from .env, which is never committed and never deployed --
    # config.yaml is a git-tracked file that gets pushed over the target's copy
    # on every deploy, so an address written there is both published and fragile.
    # It survived one sanitising pass for a public repo (real address -> a
    # placeholder) which the next deploy would have pushed onto the NAS, sending
    # every digest to you@example.com.
    #
    # Env wins over config, matching SMTP_USER below: the file is the fallback
    # for a local run, the environment is the deployment's answer. A digest's own
    # `to:` still beats both -- that is per-recipient routing, not a default.
    from_addr = os.environ.get("DIGEST_EMAIL_FROM") or email_cfg.get("from", "")
    to_addrs = (
        (digest_cfg or {}).get("to")
        or os.environ.get("DIGEST_EMAIL_TO")
        or email_cfg.get("to", "")
    )

    if isinstance(to_addrs, str):
        to_addrs = [a.strip() for a in to_addrs.split(",") if a.strip()]

    if not from_addr or not to_addrs:
        raise ValueError(
            "No email addresses configured. Set DIGEST_EMAIL_FROM and "
            "DIGEST_EMAIL_TO in .env (preferred -- .env is neither committed nor "
            "overwritten by a deploy), or delivery.email.from/to in config.yaml."
        )

    user = os.environ.get("SMTP_USER") or email_cfg.get("smtp_user") or from_addr
    password = os.environ.get("SMTP_PASSWORD")

    if not password:
        raise ValueError(
            "SMTP_PASSWORD must be set in .env for email delivery "
            "(or use an app-specific password for Gmail)"
        )

    return smtp_host, smtp_port, from_addr, to_addrs, user, password


def _send_email_message(
    subject: str, plain: str, html: str, config: dict, digest_cfg: dict | None = None
) -> None:
    """Build and send a multipart plain+html email. Shared by _deliver_email
    (new digest) and _deliver_email_html (re-sending a saved digest). Raises
    ValueError if delivery.email is misconfigured, or with a clearer message on
    SMTP auth failure."""
    smtp_host, smtp_port, from_addr, to_addrs, user, password = _resolve_email_config(
        config, digest_cfg
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    retry_cfg = config.get("retry", {})
    max_retries = retry_cfg.get("max_retries", 3)
    initial_delay = retry_cfg.get("initial_delay", 1.0)
    max_delay = retry_cfg.get("max_delay", 60.0)

    def _send_email() -> None:
        with smtplib.SMTP(smtp_host, smtp_port) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(from_addr, to_addrs, msg.as_string())

    try:
        retry(
            _send_email,
            max_retries=max_retries, initial_delay=initial_delay, max_delay=max_delay,
            non_retryable=(smtplib.SMTPAuthenticationError,),
        )
        log.info("Sent email '%s' to %s", subject, to_addrs)
    except smtplib.SMTPAuthenticationError as e:
        raise ValueError(
            "SMTP authentication failed. For Gmail, use an app-specific password: "
            "https://myaccount.google.com/apppasswords"
        ) from e


def _deliver_email(
    content: str, config: dict, title_override: str | None = None,
    digest_cfg: dict | None = None,
) -> None:
    digest_cfg = digest_cfg or config.get("digest", {}) or {}
    title = title_override or digest_cfg.get("title", "Security Digest")
    today = datetime.date.today().isoformat()
    subject = f"{title} — {today}"

    html_body = _markdown_to_html(content)
    _save_for_web(html_body, title)
    _send_email_message(subject, content, html_body, config, digest_cfg)


def _deliver_email_html(
    html_content: str, config: dict, title: str, no_new_updates: bool = False,
    digest_cfg: dict | None = None,
) -> None:
    """Send pre-rendered HTML by email (e.g. when re-sending previous digest)."""
    today = datetime.date.today().isoformat()
    subject = f"{title} — {today} (no new updates)" if no_new_updates else f"{title} — {today}"
    _send_email_message(subject, "View in HTML.", html_content, config, digest_cfg)
