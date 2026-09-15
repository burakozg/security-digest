FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# uv itself, pinned to a known release rather than whatever pip resolves.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app

# The instance's files are bind-mounted flat into /app, so /app *is* the
# instance root here -- unlike a local checkout, where it's instances/<name>.
ENV DIGEST_ROOT=/app

# --- dependency layer ---------------------------------------------------
# `package = false` in pyproject.toml (src/ has no __init__.py and isn't an
# importable package -- it runs in place via `python -m src.main`), so there's
# no separate --no-install-project pass: this one `uv sync` installs exactly
# the locked dependencies into /app/.venv and nothing else.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Deliberately no config.yaml/sources.yaml/schedule.txt/prompts baked in: one
# image serves every instance (see instances/), and each supplies its own copies
# by bind mount at run time (docker-compose.yml, deploy,
# container-station-app.yaml). Baking one instance's files would give the others
# a silent, wrong fallback whenever a mount is misconfigured; with none baked,
# load_config() raises a clear "Config not found" instead.
COPY src/ src/

ENV PATH="/app/.venv/bin:${PATH}"

# Run as non-root. data/ and output/ are typically host bind mounts (see
# docker-compose.yml, deploy.sh) whose owning UID/GID on the host is unknown at
# build time, so they're made world-writable rather than chowned to a fixed
# UID -- the alternative would silently break writes (seen.json, status.json,
# digest history, rendered HTML) whenever the host directory's owner doesn't
# match. Host-side deploy scripts chmod these dirs the same way; see deploy.sh
# and DOCKER_DEPLOY.txt.
RUN groupadd -g 1000 appuser && useradd -g appuser -u 1000 appuser \
    && mkdir -p /app/data /app/output/web \
    && chown -R appuser:appuser /app \
    && chmod -R 777 /app/data /app/output
USER appuser

# Meaningful for the long-running `web` service; harmlessly reports unhealthy
# for the one-shot `digest` pipeline run, which isn't a supervised service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/status', timeout=3).status == 200 else 1)" || exit 1

CMD ["python", "-m", "src.main"]
