# Security Digest

Fetches news, summarises and categorises each item with an LLM (Anthropic,
OpenAI, Mistral or OpenRouter), groups the results into one or more digests, and delivers
them by email (or file/console) on a daily schedule. Includes a small FastAPI
web UI for viewing digests, browsing history, and administering feeds, prompts,
and the LLM model from the browser.

Items come from two kinds of source, which the pipeline treats identically once
fetched:

- **RSS feeds** (`sources.yaml`) -- follow a publication wholesale.
- **Topics** (`topics.yaml`) -- track a named company, person or place by
  querying Google News and Bing News and filtering the results for relevance.

## Instances

One codebase and one image serve several **instances**. An instance is a
directory under `instances/` holding its own config, sources/topics, prompts,
database, schedule and email recipients — so two of them share every bug fix but
nothing else.

```
instances/
  security/    RSS feeds -> the security digest
  news/        topics    -> one digest per reader
```

Which instance the code runs against comes from `DIGEST_ROOT`. Locally that's a
path in the checkout; in Docker it is `/app`, with the instance's files
bind-mounted flat over it (set in the `Dockerfile`, mounts in
`docker-compose.yml` / `deploy.sh` / `container-station-app.yaml`).

```bash
DIGEST_ROOT=instances/security .venv/bin/python -m src.main
DIGEST_ROOT=instances/news     .venv/bin/python -m src.main
INSTANCE=news PORT=8081 docker compose up web
```

Adding an instance is copying a directory under `instances/`, giving it its own
`.env` (**with its own `DIGEST_ADMIN_TOKEN`** — sharing one lets either admin
panel drive the other), and deploying it with `--instance <name>`.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env
# then edit .env: set the key for your llm.provider (OPENAI_API_KEY /
# ANTHROPIC_API_KEY / MISTRAL_API_KEY), SMTP_* if using
# email delivery, and DIGEST_ADMIN_TOKEN (generate with: openssl rand -hex 32)

DIGEST_ROOT=instances/security .venv/bin/uvicorn src.web.app:app --reload --port 8080
# -> http://localhost:8080/  (dashboard)
# -> http://localhost:8080/history
# -> http://localhost:8080/admin

# Or run the pipeline once, outside the web server:
DIGEST_ROOT=instances/security .venv/bin/python -m src.main
```

Note that locally the LLM/SMTP secrets are read from the repo-root `.env`
(`load_dotenv()` searches from the working directory), not from
`instances/<name>/.env` — the per-instance file is what gets placed on the
deploy target and passed to the container via `env_file`.

## Configuration

All paths below are relative to an instance directory (`instances/<name>/`).

| File | Purpose |
| --- | --- |
| `config.yaml` | Main config: retry policy, source limits, LLM provider/model/categories, digest definitions, delivery settings |
| `sources.yaml` | RSS feed list (name + URL) |
| `topics.yaml` | **Seed** topic list for a brand-new instance; the live list is `data/topics.yaml`, written by the admin panel |
| `schedule.txt` | Daily run time (`enabled`, `hour`, `minute`, `timezone`) -- the only place schedule settings live; do not add a `schedule:` block to `config.yaml`, it would be silently overridden |
| `.env` | Secrets: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `SMTP_USER`/`SMTP_PASSWORD`, `DIGEST_ADMIN_TOKEN` |

### Multiple readers in one instance

**Recipients are managed entirely from the admin panel's Recipients card** —
add, edit and remove readers there and it takes effect on the next run. Nothing
to edit by hand, nothing to deploy.

They are stored in `data/users.yaml`, which is deliberately *not* git-tracked and
*not* synced by deploys: recipients are subscriber state, not configuration. That
directory is already mounted read-write everywhere (it holds the database), so
the panel writes the file in place — there is no read-only base file, no override
file shadowing it, and nothing for a deploy to reconcile or clobber. One file,
one writer.

Each topic then names who receives it:

```yaml
# data/users.yaml -- written by the admin panel
users:
  - name: Alice
    email: alice@example.com
  - name: Bob
    email: bob@example.com

# topics.yaml
topics:
  - name: Nvidia
    recipient: alice@example.com    # her only
  - name: Vattenfall
    recipient: all                  # everyone (also the default if omitted)

# config.yaml -- the shape every derived digest takes
digest_template:
  title_format: "{name}'s News"
  sections: [key, notable, mention]
```

Nothing lists digests by hand. Adding a reader is one click in the Recipients
card; their digest appears as soon as a topic reaches them. A recipient with no topics gets no email rather than an empty one.

Hand-written `digests:` still work and are what the security instance uses — an
instance with no users derives nothing and keeps whatever it declares. An empty
`users.yaml` never blanks an explicit digest list.

Routing matches on name strings, so a typo yields an empty digest rather than an
error. `load_config` warns for any digest source matching no feed or topic, any
feed or topic no digest routes to, and any topic addressed to an unknown
recipient. Removing a recipient in the admin panel reports which topics it
orphaned.

Readers in one instance share a database, a schedule, an admin token and a web
UI. That is a deliberate trade: real isolation between readers means a separate
instance.

### Clustering: one story, many sources

With `llm.cluster: true` the summariser groups each topic's items by *event*
rather than summarising them one by one: several outlets covering one story
collapse into a single digest entry that credits and links each of them.

```
### Naver structures AI factory deal with Nvidia and Brookfield
*Seoul Economic Daily · Tech in Asia · 아시아경제*
```

Clustering is per topic, and that is a correctness requirement rather than an
optimisation — `source` is what digests route on, so merging two topics' items
would deliver the story to whichever recipient the surviving item belonged to
and silently deny it to the other. `prompts/cluster.txt` defines what counts as
the same event; it is the main dial if stories are being over- or under-merged.

### Where panel-managed lists live

Topics and recipients are edited in the admin panel and stored under `data/`,
which is mounted read-write and never synced by a deploy. There is no base file
shadowed by an override and nothing to reconcile: what the panel writes is what
the app reads.

| | live file | git-tracked |
| --- | --- | --- |
| Topics | `data/topics.yaml` | `topics.yaml` seeds a **new** instance, then inert |
| Recipients | `data/users.yaml` | not tracked — subscriber state |

Editing a seed file and deploying will not change a running instance. Use the
panel.

**Do not enable `sources.story_dedupe` alongside it.** The lexical pass runs
first and discards duplicates outright, so clustering never sees them and their
links are lost from the digest — `main.py` warns if both are on. `story_dedupe`
remains for a topic instance that doesn't cluster.

Instances serving more than one reader should set `sources.fair_trim: true`.
Without it, `max_total_items` is filled by whichever feed is newest, so on a busy
news day one reader's topics can crowd another's digest out entirely.
Topic instances also want `sources.max_age_days`, since news search — unlike a
publisher feed — happily returns years-old articles.

The admin panel (`/admin`) can edit topics, RSS sources, the LLM provider/model
(OpenAI, Anthropic, Mistral or OpenRouter),
and prompts at runtime. Topics, RSS sources and the LLM provider/model are
written to `data/sources_overrides.yaml`/`data/llm_overrides.yaml` (so
`config.yaml`/`sources.yaml` can stay read-only). Topics, recipients and prompts
need no override file at all — `data/topics.yaml`, `data/users.yaml` and
`prompts/*.txt` are writable, so the panel edits them straight
to `prompts/*.txt` in place -- so that directory must be a **read-write**
bind mount (not `:ro`) in any deployment, or edits vanish the next time the
container is recreated (they land in the container's ephemeral layer
instead of anywhere durable). Every deploy path in this repo
(`docker-compose.yml`, `deploy.sh`, `deploy-native.sh`,
`container-station-app.yaml`) already mounts it that way.

The **Recipients** card adds, edits and removes readers; the **Topics** card
edits `topics.yaml`'s entries — name, queries, relevance context,
language/market, and a **Send to** dropdown of recipients plus “All” — and shows a read-only column of which digests pick
each topic up, so a topic routed to nobody is visible rather than silent. On an
instance with no RSS feeds the **RSS sources** editor collapses behind a note:
it renders one blank row when the list is empty, and saving that would write an
override that empties the feed list. Saving an empty feed list is refused
outright unless explicitly confirmed.

An override file always replaces the corresponding base list wholesale while it
exists, so `deploy.sh` pulls each one down and merges it back into the
git-tracked file before pushing (`src/reconcile.py`), then deletes it on the
target.

### Admin authentication

`/admin/*` endpoints and `POST /run` require an `X-Admin-Token` header matching
`DIGEST_ADMIN_TOKEN` in `.env`. Without that variable set, admin actions are
disabled (fail closed) -- generate one with `openssl rand -hex 32`. The
browser prompts for the token once and caches it in `localStorage`.

## Running tests

```bash
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest
```

## Deployment

### Docker Compose (general)

```bash
docker compose up -d web                      # security instance on :8080
INSTANCE=news PORT=8081 docker compose up -d web
INSTANCE=news docker compose run --rm digest  # one-off manual pipeline run
```

`INSTANCE` (default `security`) picks which directory under `instances/` is
mounted; `PORT` (default 8080) is the host port. Give each instance its own port.

Don't run the `digest` service alongside `web` -- the web service's scheduler
is the single production scheduler. The container runs as a non-root user; each
instance's `data`, `output` and `prompts` must be writable by it regardless of
which host user created them:

```bash
mkdir -p instances/news/data instances/news/output/web
chmod -R 777 instances/news/data instances/news/output instances/news/prompts
```

### Manual Docker (no Compose)

```bash
docker build -t security-digest .

INSTANCE=security   # or news

docker run -d -p 8089:8080 \
  -v $(pwd)/instances/$INSTANCE/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/instances/$INSTANCE/sources.yaml:/app/sources.yaml:ro \
  -v $(pwd)/instances/$INSTANCE/topics.yaml:/app/topics.yaml:ro \
  -v $(pwd)/instances/$INSTANCE/schedule.txt:/app/schedule.txt:ro \
  -v $(pwd)/instances/$INSTANCE/data:/app/data \
  -v $(pwd)/instances/$INSTANCE/output:/app/output \
  -v $(pwd)/instances/$INSTANCE/prompts:/app/prompts \
  --env-file instances/$INSTANCE/.env \
  --name $INSTANCE-digest-web \
  --restart unless-stopped \
  security-digest \
  uvicorn src.web.app:app --host 0.0.0.0 --port 8080
```

The image bakes in no config at all — every instance file arrives by mount, and
`DIGEST_ROOT=/app` (set in the `Dockerfile`) tells the code to look there. A
missing mount therefore fails loudly with "Config not found" instead of falling
back to some other instance's baked-in copy.

Access at `http://<host>:8089/` (dashboard), `/history`, `/admin`. The
container always listens on port 8080 internally; map whichever external port
you want via `-p`.

### Remote deploy script (`deploy.sh`)

`deploy.sh` cross-compiles a `linux/amd64` image on the dev machine (via
`docker buildx`), saves it to a tar, transfers it plus the YAML config files
over SSH, and (re)starts the container on a remote Docker host (written for a
QNAP NAS via container-station). Copy `deploy.env.example` to `.deploy.env`
(git-ignored) and fill in `TARGET_USER`/`TARGET_HOST`/`SSH_PORT` for your
target -- the script auto-sources it, so nothing needs exporting by hand or
editing in the tracked script -- then:

```bash
./deploy.sh                      # defaults to --instance security
./deploy.sh --instance news
```

`--instance <name>` selects which directory under `instances/` is deployed, and
derives the target path (`/share/Container/<name>-digest`), container name
(`<name>-digest-web`) and host port from it. The image itself is instance-independent
and shared, so it's built once per deploy regardless.

Use this when the dev machine can cross-compile for the target's architecture
without issue, or you'd rather not run a build on the NAS itself.

Before building, both `deploy.sh` and `deploy-native.sh` reconcile the
target's live admin-panel state back into the local git-tracked files, so a
deploy can't silently clobber edits made from the browser:

- `data/sources_overrides.yaml`/`data/llm_overrides.yaml` are pulled down and
  merged into `sources.yaml`/`config.yaml` (`src/reconcile.py`: the override
  wins on a name/key collision, entries unique to either side are kept), then
  the merged result is pushed up and the now-redundant override files are
  deleted from the target.
- `prompts/*.txt` has no such override file -- the admin panel writes
  directly into it -- so it's pulled down and, if it differs from the local
  copy, overwrites the local copy outright (the target's version wins; check
  `git diff` afterwards to decide whether to keep or discard it in git).

### Remote deploy script, built natively on the target (`deploy-native.sh`)

`deploy-native.sh` skips cross-compilation entirely: it rsyncs the source to a
disposable build directory on the target and runs `docker build` there, so the
image is built for whatever architecture the target's own Docker daemon
actually is -- no `--platform` flag, no QEMU emulation. It prints the target's
`uname -m` and Docker-reported architecture first so you can confirm what
you're building for. The source sync only ever touches its own disposable
build directory (`TARGET_BUILD_PATH`), never the persistent directory holding
`data/`/`output/`/config/`.env` (`TARGET_DATA_PATH`) -- `rsync --delete` can't
reach your real seen-store or history no matter what. Edit the same target
variables at the top of the script, then:

```bash
./deploy-native.sh
```

Prefer this when cross-compiling is slow/unreliable, or you just want the
build to happen on the same architecture it'll run on. Same `.env` caveat as
`deploy.sh`: not synced automatically, copy it once by hand and keep it
current on the target.

### Running via QNAP Container Station (no CLI for day-to-day ops)

`container-station-app.yaml` is a Container Station "Application" definition
(Docker Compose under the hood) that runs the image `deploy.sh`/
`deploy-native.sh` already built -- it doesn't build anything itself. The
tracked file ships with placeholder qnet IPs, so run
`./deploy.sh --render-station` first (fills them in from `.deploy.env` --
see `deploy.env.example`) and point Container Station's Applications ->
Create at the rendered `deploy-out/container-station-app.yaml` instead,
once. After that, picking up a newly built image or an edited `.env` is a
"Recreate" click in the UI instead of manual `docker stop`/`rm`/`run` over
SSH (a plain `docker restart` does **not** pick up either of those -- see
the comment at the top of the file). Secrets are kept out of the YAML via
`env_file:` pointing at the existing `.env`, rather than inlined.

It runs on a static LAN IP on the `qnet-static-eth1-dc7e3a` network (see
`STATION_IP_SECURITY` in `deploy.env.example`) rather than a NAT'd port
mapping, so it's reached directly at `http://<that IP>:8080/` -- **not**
`http://<nas host>:8089/` from the manual-CLI instructions above, which is a
separate access point tied to the manually-run container. Only one of the two
should be running at a time to avoid confusion about which one you're looking
at.

Once Container Station owns the running container, rebuild/reload the image
with `--skip-run` so `deploy.sh`/`deploy-native.sh` don't also try to
stop/rm/run their own container on the old port-mapped convention (which
would silently take you off the static IP):

```bash
./deploy.sh --skip-run          # or ./deploy-native.sh --skip-run
```

Then apply it: Container Station -> Applications -> security-digest ->
Recreate. That also picks up any `config.yaml`/`.env` changes, for the same
reason a plain "Restart" doesn't.

## Architecture

```
instances/       one directory per deployed instance (config, sources/topics,
                 prompts, data, output) -- the code is shared, these are not
src/
  fetcher.py     fetch feeds, load/merge config.yaml + topics + overrides
  topics.py      expand topics into news-search feeds; clean up their quirks
  settings.py    pydantic models validating the merged config at load time,
                 plus warnings for topics/feeds no digest routes to
  dedupe.py      cross-feed content dedup; seen-store filter/mark split
  summariser.py  LLM categorisation + summarisation (batch, with per-item fallback)
  digest.py      group items into sections, render markdown (HTML-escaped)
  delivery.py    console / file / email delivery
  history.py     log of delivered items, for the History page
  status.py      last-run status, for the dashboard
  db.py          shared SQLite connection (data/digest.db: seen, status, history)
  llm_models.py  curated model catalog + live provider validation
  retry.py       exponential backoff with a non-retryable-exception escape hatch
  main.py        orchestrates the full pipeline: fetch -> summarise -> digest -> deliver
  web/app.py     FastAPI app: dashboard, history, admin, scheduler
tests/           pytest suite for the pure/dependency-free functions above
```

`data/digest.db` (SQLite, WAL mode) holds the seen-link store, last-run
status, and delivery history -- previously three separate JSON files, each
rewritten wholesale on every write with no locking. If those JSON files exist
from an older version, they're imported into the database automatically and
losslessly on first access, then left in place untouched (not deleted).

See `IMPROVEMENTS.md` for the tracked backlog of architectural, functional,
and security improvements (most of Phases 1-3 are done as of this writing).
