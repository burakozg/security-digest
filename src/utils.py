"""Shared utilities."""

import os
import re
from pathlib import Path

# Instance root: the directory holding this instance's config.yaml, its topic or
# feed list, prompts/, data/ and output/. The Python package is shared by every
# instance (see instances/), so this is what separates one from another --
# in Docker it's /app, with the instance's files bind-mounted in; locally it's
# instances/<name>, e.g. DIGEST_ROOT=instances/security.
#
# SECURITY_DIGEST_ROOT is the original name for this, kept as a fallback so an
# already-deployed container that sets it keeps working across the upgrade.
_root = os.environ.get("DIGEST_ROOT") or os.environ.get("SECURITY_DIGEST_ROOT")
PROJECT_ROOT = (
    Path(_root).resolve() if _root else Path(__file__).resolve().parent.parent / "instances" / "security"
)


def slug(title: str) -> str:
    """Convert a title to a URL-safe slug (e.g. 'AI Security' -> 'ai-security').
    Strips to [a-z0-9-] -- a title containing '/' or ':' would otherwise produce
    a broken or unexpected file path wherever the slug is used to name a file."""
    s = title.strip().lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def render_template(path: Path, **kwargs: str) -> str:
    """Read a template file fresh (so admin edits take effect on the next call, not
    just after a restart) and substitute {key} placeholders. Unlike str.format(),
    literal braces elsewhere in the template -- e.g. a JSON example in an
    admin-edited prompt -- don't need escaping; only the named placeholders passed
    in kwargs are replaced."""
    text = path.read_text()
    for key, value in kwargs.items():
        text = text.replace("{" + key + "}", value)
    return text
