FROM python:3.13-slim

WORKDIR /app

# The instance's files are bind-mounted flat into /app, so /app *is* the
# instance root here -- unlike a local checkout, where it's instances/<name>.
ENV DIGEST_ROOT=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Deliberately no config.yaml/sources.yaml/schedule.txt/prompts baked in: one
# image serves every instance (see instances/), and each supplies its own copies
# by bind mount at run time (docker-compose.yml, deploy.sh, deploy-native.sh,
# container-station-app.yaml). Baking one instance's files would give the others
# a silent, wrong fallback whenever a mount is misconfigured; with none baked,
# load_config() raises a clear "Config not found" instead.
COPY src/ src/

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
