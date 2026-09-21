# Security Digest

Fetches news, summarises and categorises each item with an LLM (Mistral or
OpenRouter -- open-weight models only), groups the results into one or more digests, and delivers
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
`docker-compose.yml` / `deploy` / `container-station-app.yaml`).

```bash
DIGEST_ROOT=instances/security .venv/bin/python -m src.main
DIGEST_ROOT=instances/news     .venv/bin/python -m src.main
INSTANCE=news PORT=8081 docker compose up web
```

Adding an instance is copying a directory under `instances/`, giving it its own
`.env`, and deploying it with `--instance <name>`.

## Quick start

```bash
uv sync

cp .env.example .env
# then edit .env: set the key for your llm.provider (MISTRAL_API_KEY /
# OPENROUTER_API_KEY), SMTP_* if using email delivery

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
| `.env` | Secrets: `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `SMTP_USER`/`SMTP_PASSWORD`, `VAULT_COUCHDB_URL`/`VAULT_COUCHDB_PASSWORD` |

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
    frequency: weekly               # one email a week instead of seven
    send_day: sat                   # default; "mon".."sun"

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
  weekly_sections: [key, notable]   # what a weekly edition prints
```

Nothing lists digests by hand. Adding a reader is one click in the Recipients
card; their digest appears as soon as a topic reaches them. A recipient with no topics gets no email rather than an empty one.

### Daily or weekly

Each recipient picks their cadence in the Recipients card. A **weekly** reader
gets the same coverage as a daily one — their topics are fetched, summarised and
routed on every run exactly as before — but the send is held and their items are
queued in `data/digest.db`. On their day the week is re-read in one pass that
merges a Monday story with its Thursday follow-up into a single entry keeping
both outlets' links, re-ranks everything against the week rather than the day it
appeared, and opens with a short paragraph on what kind of week it was.
`prompts/weekly.txt` and `prompts/weekly_intro.txt` define both, and are editable
in the admin panel like every other prompt.

`weekly_sections` narrows what the email prints — a week of "Briefly" items is a
tail nobody reads, and they are all on the History page anyway. It is applied
when rendering and **never** to `sections`, which is what routes items: trimming
that would leave the dropped categories unrouted, so they would never be marked
seen and would be re-fetched and re-summarised at full token cost every day.
Filtering afterwards is also the right order, since the week's context can
promote a Monday `mention` into `notable`.

The send is owed until it happens: a container down on the send day, or a mail
server refusing, sends on the next run rather than costing the week, and the
queue is only cleared once the email is actually away. A second run the same day
does not send twice. History records the consolidated entries, not the daily
ones, so it shows what landed in the inbox.

Hand-written `digests:` still work and are what the security instance uses — an
instance with no users derives nothing and keeps whatever it declares. An empty
`users.yaml` never blanks an explicit digest list.

Routing matches on name strings, so a typo yields an empty digest rather than an
error. `load_config` warns for any digest source matching no feed or topic, any
feed or topic no digest routes to, and any topic addressed to an unknown
recipient. Removing a recipient in the admin panel reports which topics it
orphaned.

Readers in one instance share a database, a schedule, and a web UI (gated by
a shared reverse-proxy login now, not a per-instance admin token — see the
sibling `homelab-auth` project). That is a deliberate trade: real isolation
between readers means a separate instance.

### Clustering: one story, many sources

With `llm.cluster: true` the summariser groups each topic's items by *event*
rather than summarising them one by one: several outlets covering one story
collapse into a single digest entry that credits and links each of them.

```
### Naver structures AI factory deal with Nvidia and Brookfield
*Seoul Economic Daily · Tech in Asia · 아시아경제*
```

`llm.cluster_scope` decides what one clustering call may merge within.

`source` (the default) clusters each feed on its own, and on a **topic**
instance that is a correctness requirement rather than an optimisation —
`source` is the tracked topic, which is what digests route on, so merging two
topics' items would deliver the story to whichever recipient the surviving item
belonged to and silently deny it to the other.

`all` clusters every feed together, which is what a **publisher** instance
needs. There `source` is the outlet, so the duplicates worth merging are
precisely the ones that span outlets — one advisory in Krebs, Bleeping Computer
and The Hacker News lands in three groups under the default and never meets
itself. It is safe only while every feed reaches the same digests, since a
merged item keeps the first member's `source` and `accepts_feed` decides
delivery from that one name; `load_config` warns as soon as that stops being
true, so unticking a digest for one feed in the admin panel cannot quietly
reintroduce the loss.

`llm.cluster_chars` splits clustering into two passes: the grouping call sees a
trimmed copy of each item and decides only which are the same story, then the
merged stories are summarised from the **full** `max_description_chars`, with
every member's text pooled so the summary reflects what each outlet added.
Without it a single pass groups and summarises together — right where
descriptions are short, a real quality loss where they are not. The grouping
call reports only groups of two or more; anything it omits stands alone.

`prompts/cluster.txt` defines what counts as the same event; it is the main dial
if stories are being over- or under-merged. The security instance's copy leans
deliberately conservative — a surviving duplicate is a visible annoyance, while
two distinct events merged deletes one of them with nothing recording that it
happened.

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
(Mistral or OpenRouter),
and prompts at runtime. Topics, RSS sources and the LLM provider/model are
written to `data/sources_overrides.yaml`/`data/llm_overrides.yaml` (so
`config.yaml`/`sources.yaml` can stay read-only). Topics, recipients and prompts
need no override file at all — `data/topics.yaml`, `data/users.yaml` and
`prompts/*.txt` are writable, so the panel edits them straight
to `prompts/*.txt` in place -- so that directory must be a **read-write**
bind mount (not `:ro`) in any deployment, or edits vanish the next time the
container is recreated (they land in the container's ephemeral layer
instead of anywhere durable). Every deploy path in this repo
(`docker-compose.yml`, `deploy`,
`container-station-app.yaml`) already mounts it that way.

The **RSS sources** card carries a **Status** column showing what the last run
actually got from each feed — item count, or the reason it failed and how long it
has been failing. It also flags a third state: a feed that returns HTTP 200 and
valid XML but has published nothing in `sources.quiet_after_days` (21 by
default) is shown as **quiet**. That is what a shut-down publisher looks like —
nothing errors, nothing warns, and no items arrive — and it is how Threatpost and
CSO Online sat dead in `sources.yaml` for months. Health is recorded by the
daily run rather than probed, so the column costs no extra traffic and reports
what the pipeline experienced; **Check** (per feed) and **Check all now** fetch
live, for a feed you have just added or fixed.

The **Recipients** card adds, edits and removes readers; the **Topics** card
edits `topics.yaml`'s entries — name, queries, relevance context,
language/market, and a **Send to** dropdown of recipients plus “All” — and shows a read-only column of which digests pick
each topic up, so a topic routed to nobody is visible rather than silent. On an
instance with no RSS feeds the **RSS sources** editor collapses behind a note:
it renders one blank row when the list is empty, and saving that would write an
override that empties the feed list. Saving an empty feed list is refused
outright unless explicitly confirmed.

An override file always replaces the corresponding base list wholesale while it
exists, so `./deploy` pulls each one down and merges it back into the
git-tracked file before pushing (`src/reconcile.py`), then deletes it on the
target.

### Obsidian vault

The email is the product; the vault is the record. When `vault.enabled` is set,
every digest that goes out is also written into an Obsidian vault as notes:

| Note | Where | What it holds |
| --- | --- | --- |
| one per **story** | `12 daily-digest/<year>/<month>/<date>-<slug>.md` | the summary, every outlet that reported it, the topics it names, and the feed text the fetcher already had |
| one per **topic** | `99 topics/<slug>.md` | every story in the corpus that named this thing, oldest to newest |

There is deliberately **no per-day index note**. One existed briefly and earned
nothing: measured against the live vault, all 64 had zero inbound links while
every one of the 427 story notes had at least one, and their whole content was
`[[story]]` lines duplicating frontmatter the story notes already carry. The
digest as an *edition* is what the email and the History page are for.

The mechanism is not a file copy. Obsidian's [Self-hosted LiveSync] replicates a
vault against a CouchDB database, so writing LiveSync's *own* document format
into that database materialises the notes on every device that syncs -- nothing
has to be awake but the server, and no folder has to be mounted anywhere. The
format is reverse-engineered rather than documented; `src/vault/livesync.py` is a
port of the same code running in two other projects.

**`99 topics/` is shared.** Another application (`podcast-digest`) writes its own
entity notes into that folder, and a topic note is divided by *ownership* rather
than by author: each writer replaces only the region between its own
`<!-- begin:<owner> -->` markers, and namespaces its frontmatter keys
(`security_mentions` here, `podcasts_mentions` there). Everything else on the
page -- another writer's section, and your own prose at the top -- is never
touched. `src/vault/notes.py` is that contract; read it before changing anything
in it, because the failure mode is silently eating someone else's work in their
own vault.

A topic earns a note on its **second** mention (`vault.min_mentions`). One
mention is a detail in a story, not a thread through the corpus, and security
feeds name enough one-off CVEs to bury a vault in single-use notes. Below the
bar, a topic is plain text in the story note; when it later crosses the bar, the
older stories that named it are relinked so the graph has both edges.

**Deleting a note in Obsidian sticks.** A digest is generated output, so if you
prune last month's stories the projection leaves them alone and says so in the
log rather than putting them back.

Stories are filed by month (`2026/08/`) rather than in one flat folder: this
instance adds ~15 a day, and `podcast-digest` already nests under a year in the
same vault. Nesting costs nothing, because **Obsidian resolves `[[wikilinks]]` by
filename, not by path** -- which is also why filenames keep their date prefix,
and why moving notes between folders breaks no link.

Notes are written to `output/vault/` first and pushed from there, so a CouchDB
that is unreachable never fails a run -- the notes are on disk and the next run,
or `POST /admin/vault/resync`, catches up. That endpoint takes
`{"prune": true}` to also remove notes the vault still holds under
`12 daily-digest/` that the app no longer produces -- how a reorganisation
finishes. It never touches `99 topics/`, which is shared, and refuses if nothing
is on disk.

#### Setting it up

1. Turn it on in `config.yaml` (`vault.enabled`, and `llm.extract_entities`,
   which is what makes the model name the things each story is about).
2. Put the address and password in `.env` as `VAULT_COUCHDB_URL` and
   `VAULT_COUCHDB_PASSWORD` -- not in `config.yaml`, which is committed and is
   pushed over the target's copy on every deploy.
3. Give it an account with member access to the vault database. As the CouchDB
   admin:

   ```bash
   curl -X PUT "$COUCH/_users/org.couchdb.user:security_digest" \
     -H 'Content-Type: application/json' \
     -d '{"name":"security_digest","password":"...","roles":[],"type":"user"}'

   # Add it to the EXISTING member list -- replacing the list would lock your
   # own LiveSync clients out of their vault.
   curl "$COUCH/vault/_security"          # read it, add the name, then PUT it back
   ```

4. In the LiveSync plugin, **end-to-end encryption and path obfuscation must both
   be off**. We write plaintext chunks keyed by path; either setting silently
   stops the projection matching what the clients read.

#### Backfilling what was already sent

A vault that starts empty starts with no topics, and a topic note appears on a
thing's second mention -- which for most things means waiting months. The
`history` table already holds every story ever delivered, so:

```bash
python -m src.vault.backfill --dry-run          # how many rows, how many model calls
python -m src.vault.backfill --since 2026-07-01 # a recent slice first
python -m src.vault.backfill                    # the lot
```

It is resumable and idempotent: a story whose note is already on disk is skipped
without a model call, so an interrupted run is resumed by running it again.

It recovers less than a live run does, permanently, and every note it writes says
`backfilled: true` because of it. `history` stores eight columns per story, so
**raw content is gone** (`description` was never persisted) and **the
multi-outlet byline is gone** (clustering kept only the primary link). Entities
are re-extracted by the model from the title and summary rather than an article,
so expect headline-level things and some misses. The note's date is the day it
was *emailed*; the publication date was never stored.

[Self-hosted LiveSync]: https://github.com/vrtmrz/obsidian-livesync

### Admin authentication

Neither `/admin/*` (page and API) nor `POST /run` is gated by this app -- both
deployed instances sit behind a reverse proxy (Traefik) that requires login
for `PathPrefix('/admin')` and `Path('/run')` before a request ever reaches
here. `/status` and the other read-only endpoints remain open by design,
unchanged.

## Running tests

```bash
uv sync
uv run pytest
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

### Remote deploy script (`./deploy`)

`./deploy` builds the image, ships it to the NAS with each instance's config
files, ships the compose definition and brings the stack up over ssh. Copy
`deploy.env.example` to `.deploy.env` (git-ignored) and fill in
`NAS_SSH`/`NAS_SSH_PORT` for your target -- the script auto-sources it, so
nothing needs exporting by hand or editing in the tracked script -- then:

```bash
./deploy                         # every instance under instances/
./deploy --instance news         # just that one
./deploy ship                    # build + ship, nothing applied
```

Its verbs (`ship`, `apply`, `render`, `check`, `--no-apply`) are shared with the
three sibling NAS projects; see `homelab/README.md` for the contract. This was
two scripts until they were merged -- `deploy.sh` and `deploy-native.sh`,
differing only in where the image was built. They had already drifted apart in a
way that mattered: the native one invoked `src/reconcile.py` without its stamp
argument, silently disabling the protection described below for `sources.yaml`
and the `llm:` block.

`--instance <name>` selects which directory under `instances/` is deployed, and
derives the target path (`/share/Container/<name>-digest`) from it. Deploying
every instance is the default: defaulting to one meant a plain deploy left the
others running stale config with nothing in the output to say so.

`--build-on` picks where the image is built:

- `mac` (default) cross-compiles `linux/amd64` here with `docker buildx` and
  streams it straight into `docker load` on the NAS over one SSH pipe -- no
  intermediate tar on either end -- then verifies with `docker image inspect`
  that it actually landed (`docker load` exits 0 after failing mid-stream often
  enough to be worth checking).
- `nas` skips cross-compilation entirely: it pushes the build context
  (`Dockerfile`, `pyproject.toml`, `uv.lock`, `src/` -- all the Dockerfile needs) to a
  disposable directory on the NAS and runs `docker build` there, so the image is
  built for whatever architecture that Docker daemon actually is. No `--platform`,
  no QEMU emulation. The build directory is wiped and re-pushed each time and is
  never an instance directory, so nothing there can reach a seen-store, history
  or digest output. Prefer it when cross-compiling is slow or unreliable.

`.env` is not synced by either path: copy it once by hand and keep it current on
the target.

Before building, `./deploy` reconciles the
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

### Running on the NAS (`docker-compose.nas.yml`)

`docker-compose.nas.yml` defines both instances as one compose project
(`name: daily-digests`) running the image `./deploy` already built -- it doesn't
build anything itself. `./deploy` ships it to `/share/Container/daily-digests/`
and runs `docker compose up -d` there over ssh. One command, no UI step.

`./deploy render` writes `deploy-out/docker-compose.nas.yml`, the copy that
actually gets shipped -- there's nothing left for it to substitute, since
neither instance carries a per-instance address any more, but the step still
runs so a token left unfilled by mistake would be caught rather than shipped.
Secrets stay out of the YAML too: each instance reads its own `.env` from its
own directory via `env_file:`.

```bash
./deploy                        # ship, then compose up -d
./deploy --no-apply             # ship only, leave the containers running
./deploy apply                  # compose up -d from what's already shipped
```

`compose up -d` recreates only the services whose definition or image actually
changed, and picks up `config.yaml`/`.env` changes for free -- a container is
*created* fresh, which is what a plain `docker restart` never did.

Neither instance has a LAN address of its own. Both join `homelab-internal`, a
plain bridge shared with Traefik, and are reached only through Traefik's
`security-digest.servers.zou` / `news-digest.servers.zou` routes -- **not**
`http://<nas host>:8089/` from the manual-CLI instructions above, and not a
qnet IP. `./deploy check` probes each instance from the NAS itself over
`homelab-internal`, by asking Traefik's own container to reach it by name --
there is no LAN-facing address left to curl directly.

#### Why this stopped being a Container Station "Application"

It used to be one, pasted into Container Station's UI and applied with a
"Recreate" click. A Container Station Application is a plain compose project too
-- the same `docker compose` v2.29.1 -- and the only real difference is that
Container Station keeps its **own copy** of the YAML under
`.qpkg/container-station/data/application/<name>/`. "Recreate" re-reads that
copy, not this repo's file, so every change here needed a manual re-paste and
forgetting was silent.

Three things made that a bad trade, and none of them was compensated:

- the deploy couldn't be one command -- render, paste, click;
- Container Station rejects resource limits in the pasted YAML and wants them set
  in its own panel, so they couldn't be version-controlled here (the panel it
  generated for these two services contained nothing but zeros);
- surviving a NAS reboot comes from `restart: unless-stopped`, which the docker
  daemon honours whoever started the container -- not from being an Application.

These containers now join `homelab-internal` (`external: true` in the compose
file) rather than Container Station's qnet macvlan -- a plain bridge created
once and shared with Traefik via `docker network connect`, not a
QNAP-managed network. Container Station still lists the containers themselves
under **Containers** for logs and start/stop; only the Application wrapper
and the qnet membership are gone.

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
  vault/         project delivered digests into an Obsidian vault over LiveSync
    livesync.py    LiveSync's CouchDB document format (chunks + path-keyed entries)
    notes.py       the ownership contract for topic notes several apps write to
    text.py        slugs, escaping, entity canonicalisation -- shared rules, do not drift
    topics.py      per-(thing, story) mentions, the note threshold, the topic note
    render.py      story notes
    backfill.py    one-off: rebuild the vault from the history table
  status.py      last-run status, for the dashboard
  db.py          shared SQLite connection (data/digest.db: seen, status, history)
  llm_models.py  curated model catalog + live provider validation
  retry.py       exponential backoff with a non-retryable-exception escape hatch
  main.py        orchestrates the full pipeline: fetch -> summarise -> digest -> deliver
  web/app.py     FastAPI app: dashboard, history, admin, scheduler
tests/           pytest suite for the pure/dependency-free functions above
```

`data/digest.db` (SQLite, WAL mode) holds the seen-link store, last-run
status, delivery history, feed health, the weekly queue, token usage, and the
vault's entity mentions -- previously three separate JSON files, each
rewritten wholesale on every write with no locking. If those JSON files exist
from an older version, they're imported into the database automatically and
losslessly on first access, then left in place untouched (not deleted).

See `IMPROVEMENTS.md` for the tracked backlog of architectural, functional,
and security improvements (most of Phases 1-3 are done as of this writing).
