#!/usr/bin/env bash
set -euo pipefail

# Deploy one or more instances to the NAS.
#
#   ./deploy.sh                          every instance under instances/
#   ./deploy.sh --instance news          just that one
#   ./deploy.sh --instance news --instance security   both, explicitly
#   ./deploy.sh --skip-run               (any of the above) leave containers alone
#   ./deploy.sh --render-station         write deploy-out/container-station-app.yaml
#                                         with real qnet IPs filled in, then exit
#
# Deploying everything is the default because the alternative -- defaulting to
# one instance -- means a plain `./deploy.sh` silently leaves the others running
# stale config, with nothing in the output to say so. That happened: a deploy
# that looked like it covered both instances pushed only one, and the other was
# left without a prompt file its config required.
#
# --skip-run: build, transfer and load the image and sync config files, but leave
# the running containers alone. Required once lifecycle is managed by Container
# Station (container-station-app.yaml) -- this script's own stop/rm/run uses the
# old port-mapped, non-static-IP convention and would silently revert a
# Container Station-managed container to it. After --skip-run, apply the new
# image via Container Station -> Applications -> Recreate.
SKIP_RUN=false
RENDER_STATION=false
INSTANCES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-run) SKIP_RUN=true ;;
    --render-station) RENDER_STATION=true ;;
    --instance) INSTANCES="${INSTANCES} ${2:?--instance needs a name}"; shift ;;
    --instance=*) INSTANCES="${INSTANCES} ${1#*=}" ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# Real per-deployment values live in .deploy.env, git-ignored (see
# deploy.env.example) — auto-sourced here so nothing needs exporting by hand.
[ -f .deploy.env ] && . .deploy.env

TARGET_USER="${TARGET_USER:-deploy}"
TARGET_HOST="${TARGET_HOST:-nas.local}"
SSH_PORT="${SSH_PORT:-22}"
TARGET="${TARGET_USER}@${TARGET_HOST}"

# container-station-app.yaml is pasted by hand into Container Station's UI (see
# its own header comment) -- there is no CLI step to inject real values into it
# the way .deploy.env feeds this script. So it ships with placeholder IPs, and
# this renders the real ones from .deploy.env into a throwaway copy to paste
# from instead, exactly once (the tracked template rarely changes).
if [ "${RENDER_STATION}" = true ]; then
  STATION_IP_SECURITY="${STATION_IP_SECURITY:-10.0.0.2}"
  STATION_IP_NEWS="${STATION_IP_NEWS:-10.0.0.3}"
  mkdir -p deploy-out
  sed -E "s#STATION_IP_SECURITY_PLACEHOLDER#${STATION_IP_SECURITY}#
          s#STATION_IP_NEWS_PLACEHOLDER#${STATION_IP_NEWS}#" \
    container-station-app.yaml > deploy-out/container-station-app.yaml
  if command -v pbcopy >/dev/null 2>&1; then pbcopy < deploy-out/container-station-app.yaml; fi
  echo "✓ Wrote deploy-out/container-station-app.yaml (copied to your clipboard if pbcopy is available)"
  echo "  Paste that file into Container Station, not container-station-app.yaml itself."
  exit 0
fi

if [ -z "${INSTANCES// /}" ]; then
  INSTANCES="$(ls -1 instances 2>/dev/null | tr '\n' ' ')"
fi
if [ -z "${INSTANCES// /}" ]; then
  echo "No instances found under instances/" >&2
  exit 2
fi
for inst in ${INSTANCES}; do
  if [ ! -d "instances/${inst}" ]; then
    echo "No such instance: instances/${inst}" >&2
    echo "Available: $(ls -1 instances 2>/dev/null | tr '\n' ' ')" >&2
    exit 2
  fi
done
# Instance-independent on purpose: every instance runs the identical image, and
# container-station-app.yaml refers to this name. Changing it would orphan the
# Container Station application.
IMAGE_NAME="security-digest"
TAR_FILE="${IMAGE_NAME}.tar"
DOCKER_BIN="/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker"

# Files that make up an instance. Flat on the target (the container sees them at
# /app), nested under instances/<name>/ here. Deliberately absent: users.yaml and
# the live topics list, which live in data/ and are owned by the admin panel.
INSTANCE_FILES="config.yaml sources.yaml topics.yaml schedule.txt prompts/summarise.txt prompts/summarise_batch.txt prompts/cluster.txt prompts/digest.txt"
PROMPT_FILES="prompts/summarise.txt prompts/summarise_batch.txt prompts/cluster.txt prompts/digest.txt"
# Hashes of the prompts as last deployed, kept on the target. Lives in data/
# because that is the one directory mounted read-write on every instance.
PROMPT_STAMP="data/.deployed-prompts"
# Feed names as last deployed. Without it, a feed in the admin-panel override but
# missing from sources.yaml is indistinguishable from one the panel just added,
# so deleting a feed by hand was silently undone by the next deploy. Same trick,
# same directory, same reason as PROMPT_STAMP above.
SOURCES_STAMP="data/.deployed-sources"
# The llm: block as last deployed -- same purpose again, for the keys the admin
# panel writes (provider, model). Without it the override wins unconditionally,
# so editing or removing one of those keys in config.yaml was silently reverted.
LLM_STAMP="data/.deployed-llm"

target_path() { echo "/share/Container/${1}-digest"; }
container_name() { echo "${1}-digest-web"; }
host_port() {
  # Only used by the fallback `docker run`; Container Station-managed containers
  # set their own (see container-station-app.yaml).
  case "$1" in
    security) echo 8089 ;;
    news)     echo 8090 ;;
    *)        echo 8091 ;;
  esac
}

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi

echo "==> Deploying to ${TARGET}: $(echo ${INSTANCES} | tr ' ' ',')"

# ---------------------------------------------------------------------------
# 1. Reconcile admin-panel state back into the git-tracked files, per instance.
#
# sources.yaml and config.yaml's llm: block are mounted read-only, so the admin
# panel writes to data/sources_overrides.yaml / data/llm_overrides.yaml instead,
# and those override the base file completely while they exist. Pull them down,
# merge, and only afterwards delete them on the target.
#
# Files are pulled with `ssh ... cat`, never scp: this NAS's sshd exposes no SFTP
# subsystem, so every scp fails with "subsystem request failed on channel 0".
# Written as `scp ... 2>/dev/null || true` that failure was invisible -- the merge
# silently did nothing while the delete still ran, which is how admin-panel edits
# were destroyed by a deploy.
# ---------------------------------------------------------------------------
TMP_FILES=""
trap 'rm -f ${TMP_FILES}' EXIT

pull_remote_file() {
  # pull_remote_file <remote-path> <local-path>; non-zero if absent or empty.
  ssh -p "${SSH_PORT}" "${TARGET}" "[ -f '$1' ] && cat '$1'" > "$2" 2>/dev/null && [ -s "$2" ]
}

CLEARABLE=""
for inst in ${INSTANCES}; do
  tp="$(target_path "${inst}")"
  dir="instances/${inst}"
  echo "==> [${inst}] reconciling admin-panel state from ${tp}"

  for kind in sources llm; do
    tmp="$(mktemp -t "digest-${kind}-override")"; rm -f "${tmp}"
    TMP_FILES="${TMP_FILES} ${tmp}"
    base="${dir}/sources.yaml"; [ "${kind}" = llm ] && base="${dir}/config.yaml"

    if pull_remote_file "${tp}/data/${kind}_overrides.yaml" "${tmp}"; then
      # Only a file we actually merged may be deleted on the target afterwards.
      CLEARABLE="${CLEARABLE} ${inst}:${kind}"
    elif ssh -p "${SSH_PORT}" "${TARGET}" "[ -e '${tp}/data/${kind}_overrides.yaml' ]" 2>/dev/null; then
      echo "  !! ${kind}_overrides.yaml exists on the target but could not be pulled." >&2
      echo "     Leaving it in place rather than deleting unmerged admin-panel edits." >&2
    fi
    kstamp="$(mktemp -t digest-kind-stamp)"; TMP_FILES="${TMP_FILES} ${kstamp}"
    remote_stamp="${SOURCES_STAMP}"; [ "${kind}" = llm ] && remote_stamp="${LLM_STAMP}"
    # No stamp on the target yet means "assume nothing was edited by hand", which
    # is the pre-stamp behaviour -- so an empty file here is a safe fallback, not
    # a failure. rm it so reconcile sees a genuinely absent stamp rather than an
    # empty one it would read as "nothing was ever deployed".
    pull_remote_file "${tp}/${remote_stamp}" "${kstamp}" || rm -f "${kstamp}"
    "${PYTHON}" -m src.reconcile "${kind}" "${base}" "${tmp}" "${kstamp}"
  done

  # prompts/*.txt has no base/override split -- the admin panel writes them in
  # place, so the target's copy IS the live truth once anyone has edited one in
  # the browser, and a deploy must not clobber that.
  #
  # "Target differs from repo" is NOT enough to conclude someone edited it in the
  # browser: it is equally true when the repo is deliberately ahead. Deciding on
  # that alone made prompt changes undeployable -- the target's older copy was
  # pulled back over them every time, reverting the working tree, while an edited
  # config.yaml went up regardless. That combination is the worst case: a schema
  # enforcing one vocabulary and a prompt teaching another, which fails silently
  # by coercing every item into the fallback section.
  #
  # So compare against what this script last deployed, recorded on the target in
  # ${PROMPT_STAMP}. Target unchanged since then means nobody edited it and the
  # repo wins; any difference from the stamp is a real browser edit and is pulled
  # down as before. A target with no stamp yet (first deploy after this change)
  # is treated as edited -- the safe assumption, since it may hold edits made
  # before anything was recorded.
  stamp="$(mktemp -t digest-stamp)"; TMP_FILES="${TMP_FILES} ${stamp}"
  pull_remote_file "${tp}/${PROMPT_STAMP}" "${stamp}" || : > "${stamp}"
  for f in ${PROMPT_FILES}; do
    tmp="$(mktemp -t digest-prompt)"; TMP_FILES="${TMP_FILES} ${tmp}"
    pull_remote_file "${tp}/${f}" "${tmp}" || continue
    diff -q "${tmp}" "${dir}/${f}" > /dev/null 2>&1 && continue

    recorded="$(awk -v k="${f}" '$2 == k { print $1 }' "${stamp}")"
    current="$(shasum -a 256 "${tmp}" | awk '{print $1}')"
    if [ -n "${recorded}" ] && [ "${recorded}" = "${current}" ]; then
      echo "  -> ${f} unchanged on the target since the last deploy -- pushing the repo version"
    else
      echo "  -> ${f} was edited in the browser -- pulling it down (review with git diff)"
      cp "${tmp}" "${dir}/${f}"
    fi
  done
done

# ---------------------------------------------------------------------------
# 2. Build, transfer and load the image ONCE. Every instance runs the same
#    image, so doing this per instance would re-send ~58MB and re-load an
#    identical tar for each one.
# ---------------------------------------------------------------------------
echo "==> Building image for linux/amd64..."
docker buildx build --platform linux/amd64 -t "${IMAGE_NAME}" .

echo "==> Saving image to ${TAR_FILE}..."
docker save "${IMAGE_NAME}" -o "${TAR_FILE}"

STAGING_PATH="$(target_path "$(echo ${INSTANCES} | awk '{print $1}')")"
echo "==> Transferring image to ${TARGET}:${STAGING_PATH} (staging; loaded once)..."
ssh -p "${SSH_PORT}" "${TARGET}" "mkdir -p '${STAGING_PATH}' && cat > '${STAGING_PATH}/${TAR_FILE}'" < "${TAR_FILE}"

echo "==> Loading image on target..."
ssh -p "${SSH_PORT}" "${TARGET}" "'${DOCKER_BIN}' load -i '${STAGING_PATH}/${TAR_FILE}'"

# ---------------------------------------------------------------------------
# 3. Per instance: sync mounted files, drop merged overrides, fix permissions,
#    and (unless --skip-run) restart the container.
# ---------------------------------------------------------------------------
for inst in ${INSTANCES}; do
  tp="$(target_path "${inst}")"
  dir="instances/${inst}"

  # Mounts read files on the target host -- the image alone does not update them.
  echo "==> [${inst}] syncing config files to ${tp}"
  ssh -p "${SSH_PORT}" "${TARGET}" "mkdir -p '${tp}'"
  for f in ${INSTANCE_FILES}; do
    echo "  -> ${f}"
    ssh -p "${SSH_PORT}" "${TARGET}" "mkdir -p '${tp}/$(dirname "${f}")' && cat > '${tp}/${f}'" < "${dir}/${f}"
  done

  # Record what the prompts look like as deployed, so the next run can tell a
  # browser edit from a target that simply hasn't been touched. Written after the
  # push and from the files as pushed, so the stamp always describes what is
  # actually on the target.
  ( cd "${dir}" && shasum -a 256 ${PROMPT_FILES} ) \
    | ssh -p "${SSH_PORT}" "${TARGET}" "cat > '${tp}/${PROMPT_STAMP}'"

  # Same idea for the feed list: record the names as just pushed, so the next
  # deploy can tell a feed deleted from sources.yaml (in the stamp, absent from
  # the base) from one the admin panel added (in neither).
  "${PYTHON}" -c "import sys,yaml;d=yaml.safe_load(open(sys.argv[1]))or{};print('\n'.join(f['name'] for f in (d.get('rss') or []) if isinstance(f,dict) and f.get('name')))" "${dir}/sources.yaml" \
    | ssh -p "${SSH_PORT}" "${TARGET}" "cat > '${tp}/${SOURCES_STAMP}'"

  # And the llm: block as pushed, for the same comparison next time.
  "${PYTHON}" -c "import sys,yaml;d=yaml.safe_load(open(sys.argv[1]))or{};print(yaml.dump(d.get('llm') or {}, default_flow_style=False, sort_keys=False), end='')" "${dir}/config.yaml" \
    | ssh -p "${SSH_PORT}" "${TARGET}" "cat > '${tp}/${LLM_STAMP}'"

  # The base files just pushed are a superset of whatever the overrides held, so
  # the overrides can go -- but only the ones we actually merged. These are
  # read-only bind mounts, not baked into the image, so this takes effect on the
  # next request the running container handles, independent of --skip-run.
  clear_paths=""
  for entry in ${CLEARABLE}; do
    case "${entry}" in
      "${inst}:sources") clear_paths="${clear_paths} '${tp}/data/sources_overrides.yaml'" ;;
      "${inst}:llm")     clear_paths="${clear_paths} '${tp}/data/llm_overrides.yaml'" ;;
    esac
  done
  if [ -n "${clear_paths}" ]; then
    echo "  -> clearing reconciled overrides"
    ssh -p "${SSH_PORT}" "${TARGET}" "rm -f ${clear_paths}"
  fi

  echo "==> [${inst}] preparing directories and container"
  ssh -p "${SSH_PORT}" "${TARGET}" bash -s -- \
      "${tp}" "$(container_name "${inst}")" "${IMAGE_NAME}" "${SKIP_RUN}" "$(host_port "${inst}")" "${DOCKER_BIN}" << 'REMOTE'
  set -euo pipefail
  TARGET_PATH="$1"; CONTAINER_NAME="$2"; IMAGE_NAME="$3"
  SKIP_RUN="$4"; HOST_PORT="$5"; DOCKER="$6"
  cd "${TARGET_PATH}"

  # The container runs as a non-root user; data/ and output/ are world-writable
  # in the image but bind mounts inherit the host directory's own permissions,
  # so make sure they're writable here too regardless of which host user owns
  # them (mkdir is a no-op if they already exist).
  echo "  -> ensuring data/output/prompts are writable"
  mkdir -p data output/web prompts
  # chmod only works on paths this SSH user owns. Directories created by a
  # different NAS user fail with "Operation not permitted" even when they
  # already carry the mode we want -- and under `set -e` that aborted the whole
  # deploy over a no-op. Only the end state matters, so ignore the failure and
  # verify the mode instead.
  chmod -R 777 data output prompts 2>/dev/null || true
  for d in data output prompts; do
    mode="$(stat -c '%a' "${d}")"
    if [ "${mode}" != "777" ]; then
      echo "  !! ${d} is mode ${mode} (want 777) and $(whoami) cannot chmod it -- it's owned by $(stat -c '%U' "${d}")."
      echo "     Fix from a NAS shell with rights to it: chmod -R 777 ${TARGET_PATH}/${d}"
      echo "     Left unfixed, admin-panel saves fail with [Errno 13] Permission denied."
      exit 1
    fi
  done

  if [ "${SKIP_RUN}" = "true" ]; then
    echo "  -> --skip-run: leaving the running container alone."
    exit 0
  fi

  if "${DOCKER}" ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "  -> stopping and removing existing container"
    "${DOCKER}" stop "${CONTAINER_NAME}"
    "${DOCKER}" rm "${CONTAINER_NAME}"
  fi

  echo "  -> starting container"
  "${DOCKER}" run -d -p "${HOST_PORT}:8080" \
    -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
    -v "$(pwd)/sources.yaml:/app/sources.yaml:ro" \
    -v "$(pwd)/topics.yaml:/app/topics.yaml:ro" \
    -v "$(pwd)/schedule.txt:/app/schedule.txt:ro" \
    -v "$(pwd)/data:/app/data" \
    -v "$(pwd)/output:/app/output" \
    -v "$(pwd)/prompts:/app/prompts" \
    --env-file .env \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    "${IMAGE_NAME}" \
    uvicorn src.web.app:app --host 0.0.0.0 --port 8080

  "${DOCKER}" ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
REMOTE
done

echo
if [ "${SKIP_RUN}" = true ]; then
  echo "==> Done. Image loaded and config synced for:$(echo " ${INSTANCES}" | tr -s ' ')"
  echo "    Container Station -> Applications -> Recreate to apply the new image."
  echo "    (One application holds both services, so one Recreate covers them.)"
else
  for inst in ${INSTANCES}; do
    echo "==> ${inst}: http://${TARGET_HOST}:$(host_port "${inst}")/"
  done
fi
