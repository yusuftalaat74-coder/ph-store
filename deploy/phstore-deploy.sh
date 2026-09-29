#!/usr/bin/env bash
# PH Store — production deploy. Runs ON the server (Hostinger), in /opt/phstore.
#
#   ssh root@srv1525769.hstgr.cloud
#   cd /opt/phstore && bash phstore-deploy.sh            # or: bash phstore-deploy.sh --dry-run
#
# What it does, in order, and it stops at the first thing that is not right:
#
#   1. works out how /opt/phstore is set up — the two setups that exist:
#        clone : the api container `git clone`s github.com/…/ph-store on every
#                start and runs `alembic upgrade head` itself (what the server
#                has run since September; /opt/phstore is not a git checkout)
#        build : the repository's own docker-compose.yml — an image built from
#                ./backend and a one-shot `migrate` service
#      and keeps every compose file the stack already uses, the TLS overlay
#      (docker-compose.tls.yml) included: a recreated api container without
#      its Traefik labels would silently fall off https://api.novaraca.com.
#   2. pg_dump of the database to /opt/phstore/backups — aborts if it fails,
#      before anything has changed (the build setup's `git pull` included)
#   2b. makes sure uploaded files (licence photos, price lists) survive a
#      recreate: when the api has no volume at ROVA_STORAGE_DIR, copies what
#      the container holds to /opt/phstore/storage, writes
#      docker-compose.storage.yml (a bind mount of that directory) and adds
#      it to COMPOSE_FILE in .env (backup: .env.bak-<stamp>)
#   3. checks the code to deploy actually contains the expected migration
#      (clone: on GitHub; build: in the checkout after `git pull`)
#   4. rolls the code: clone → recreate api (it clones + migrates on boot),
#      then jobs; build → build images, run `migrate`, recreate api + jobs
#   5. waits for https://api.novaraca.com/healthz, prints the alembic head,
#      and proves the new route is live with a sign-up that cannot succeed
#
# It touches only this project's own services (db is read from, never
# restarted). No `down`, no `prune`, nothing under Traefik, /opt/phoffice or
# any other stack on the host.
#
# Overridable: PHSTORE_DIR, HEALTH_URL, EXPECT_HEAD, HEALTH_TIMEOUT,
# ALLOW_NO_TLS=1 (deploy although docker-compose.tls.yml is not in COMPOSE_FILE).

set -Eeuo pipefail

PHSTORE_DIR="${PHSTORE_DIR:-/opt/phstore}"
HEALTH_URL="${HEALTH_URL:-https://api.novaraca.com/healthz}"
EXPECT_HEAD="${EXPECT_HEAD:-0009_pharmacy_signup}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }
run()  { info "\$ $*"; if (( DRY_RUN )); then return 0; fi; "$@"; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

cd "$PHSTORE_DIR" || die "no $PHSTORE_DIR"

# --- docker compose, v2 plugin or v1 binary --------------------------------
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  die "neither 'docker compose' nor 'docker-compose' is available"
fi

# --- 1. which compose files --------------------------------------------------
# `docker compose` reads COMPOSE_FILE from ./.env by itself. When .env does
# not set it, name the files explicitly: base + override + TLS overlay, in
# that order. (Setting COMPOSE_FILE turns off the automatic override file,
# so the override is listed too when it exists.)
env_compose_file=""
if [[ -f .env ]]; then
  env_compose_file="$(grep -E '^[[:space:]]*COMPOSE_FILE=' .env | tail -n1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
fi
if [[ -n "$env_compose_file" ]]; then
  export COMPOSE_FILE="$env_compose_file"
  info "COMPOSE_FILE from .env: $COMPOSE_FILE"
else
  files=()
  for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
    [[ -f "$f" ]] && { files+=("$f"); break; }
  done
  (( ${#files[@]} )) || die "no docker-compose.yml in $PHSTORE_DIR"
  for f in docker-compose.override.yml docker-compose.override.yaml docker-compose.tls.yml docker-compose.tls.yaml; do
    [[ -f "$f" ]] && files+=("$f")
  done
  export COMPOSE_FILE="$(IFS=:; echo "${files[*]}")"
  info "COMPOSE_FILE (from the files present): $COMPOSE_FILE"
fi
IFS=: read -r -a compose_files <<< "$COMPOSE_FILE"
for f in "${compose_files[@]}"; do [[ -f "$f" ]] || die "COMPOSE_FILE names $f, which does not exist"; done
if ls docker-compose.tls.y*ml >/dev/null 2>&1 && [[ "$COMPOSE_FILE" != *tls* ]] && [[ "${ALLOW_NO_TLS:-0}" != 1 ]]; then
  die "docker-compose.tls.yml exists but is not in COMPOSE_FILE ($COMPOSE_FILE) — recreating api would drop TLS"
fi

CONFIG="$("${DC[@]}" config 2>/dev/null)" || die "'docker compose config' failed — fix the compose files first"
services="$("${DC[@]}" config --services)"
grep -qx api <<<"$services" || die "no 'api' service in this stack"
grep -qx db  <<<"$services" || die "no 'db' service in this stack"
has_jobs=0; grep -qx jobs <<<"$services" && has_jobs=1
has_migrate=0; grep -qx migrate <<<"$services" && has_migrate=1

# the api service's block of the resolved config
api_block="$(awk '/^  api:/{f=1;print;next} f&&/^  [^ ]/{f=0} f' <<<"$CONFIG")"
if grep -q 'git clone' <<<"$api_block"; then
  MODE=clone
elif grep -qE '^    build:' <<<"$api_block" && (( has_migrate )); then
  MODE=build
else
  die "cannot tell how this stack gets its code (api has neither a 'git clone' command nor build: + a migrate service)"
fi
say "setup: $MODE  (services: $(tr '\n' ' ' <<<"$services"))"
if grep -qi traefik <<<"$api_block"; then info "api carries Traefik labels — kept"; fi

# --- where uploaded licences (and price-list files) land must survive a recreate
# Inside the container the API writes to ROVA_STORAGE_DIR. In the build setup
# the repository's compose file mounts a named volume there; the clone setup's
# compose file may not. Without a mount, every recreate — this script's own
# step 4 included — throws the files away. So when the api has no mount at
# that path, this script adds one: a bind mount of $PHSTORE_DIR/storage,
# declared in docker-compose.storage.yml and added to COMPOSE_FILE in .env so
# that a plain `docker compose up -d` by hand keeps it too. Files already in
# the running container are copied out first.
storage_dir="$(grep -E 'ROVA_STORAGE_DIR' <<<"$api_block" | head -n1 | sed -E 's/.*ROVA_STORAGE_DIR:? *"?([^"]*)"?.*/\1/' || true)"
if [[ -z "$storage_dir" || "$storage_dir" != /* ]]; then
  # relative or unset: only the container knows where that resolves to
  resolved="$("${DC[@]}" exec -T api python -c 'import os; from rova.config import get_settings; print(os.path.abspath(get_settings().storage_dir))' 2>/dev/null | tr -d '\r' || true)"
  [[ "$resolved" == /* ]] && storage_dir="$resolved"
fi
STORAGE_OVERLAY=docker-compose.storage.yml
NEED_STORAGE_MOUNT=0
if [[ "$storage_dir" != /* ]]; then
  printf '\033[33m    WARNING: cannot tell where the api keeps uploaded files (ROVA_STORAGE_DIR is not set and\n'
  printf '             the api container did not answer). Licence photos may not survive a recreate.\033[0m\n'
elif grep -qE "target: ${storage_dir}$|:${storage_dir}(:|$)" <<<"$api_block"; then
  info "uploaded files: api mounts $storage_dir — kept"
else
  NEED_STORAGE_MOUNT=1
  say "uploaded files: api has NO mount at $storage_dir — will bind-mount $PHSTORE_DIR/storage there ($STORAGE_OVERLAY)"
fi

ensure_storage_mount() {
  # called after the backup, before anything is recreated
  (( NEED_STORAGE_MOUNT )) || return 0
  local host_dir="$PHSTORE_DIR/storage" tmp
  info "host directory: $host_dir"
  run mkdir -p "$host_dir"
  run chmod 700 "$host_dir"

  # what the running container holds, before it is thrown away
  if "${DC[@]}" ps --status running --services 2>/dev/null | grep -qx api; then
    info "copying the api container's $storage_dir to $host_dir"
    if (( ! DRY_RUN )); then
      "${DC[@]}" cp "api:${storage_dir}/." "$host_dir/" 2>/dev/null \
        || docker cp "$("${DC[@]}" ps -q api | head -n1):${storage_dir}/." "$host_dir/" \
        || die "could not copy $storage_dir out of the api container — copy it by hand to $host_dir, then rerun"
      info "$(find "$host_dir" -type f | wc -l) file(s) now in $host_dir"
    fi
  fi

  # the overlay: one bind mount for api (and jobs, which shares the image)
  tmp="$(printf '# Written by phstore-deploy.sh on %s.\n# Keeps uploaded licence photos and price-list files across container recreates.\n# Listed in COMPOSE_FILE (.env); do not remove it from there.\nservices:\n  api:\n    volumes:\n      - %s:%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$host_dir" "$storage_dir")"
  if (( has_jobs )); then
    tmp+="$(printf '\n  jobs:\n    volumes:\n      - %s:%s\n' "$host_dir" "$storage_dir")"
  fi
  info "writing $STORAGE_OVERLAY:"
  sed 's/^/        | /' <<<"$tmp"
  if (( ! DRY_RUN )); then printf '%s\n' "$tmp" > "$STORAGE_OVERLAY"; fi

  # make it stick: COMPOSE_FILE in .env, with a backup of .env first
  local new_compose_file="${COMPOSE_FILE}:${STORAGE_OVERLAY}"
  if [[ "$COMPOSE_FILE" == *"$STORAGE_OVERLAY"* ]]; then new_compose_file="$COMPOSE_FILE"; fi
  info "COMPOSE_FILE -> $new_compose_file (in .env; backup .env.bak-$stamp)"
  if (( ! DRY_RUN )); then
    [[ -f .env ]] && cp -p .env ".env.bak-$stamp"
    if [[ -n "$env_compose_file" ]]; then
      sed -i -E "s#^([[:space:]]*COMPOSE_FILE=).*#\1${new_compose_file}#" .env
    else
      printf '\n# added by phstore-deploy.sh: the storage overlay keeps uploaded files across recreates\nCOMPOSE_FILE=%s\n' "$new_compose_file" >> .env
    fi
    export COMPOSE_FILE="$new_compose_file"
    if ! "${DC[@]}" config >/dev/null 2>&1; then
      [[ -f ".env.bak-$stamp" ]] && cp -p ".env.bak-$stamp" .env
      rm -f "$STORAGE_OVERLAY"
      die "'docker compose config' rejected the storage overlay — .env restored, nothing recreated"
    fi
    "${DC[@]}" config 2>/dev/null | awk '/^  api:/{f=1;print;next} f&&/^  [^ ]/{f=0} f' \
      | grep -qE "target: ${storage_dir}$" || die "the overlay did not take: api still has no mount at $storage_dir"
    info "api now mounts $host_dir at $storage_dir"
  fi
}

# --- database coordinates ----------------------------------------------------
db_block="$(awk '/^  db:/{f=1;print;next} f&&/^  [^ ]/{f=0} f' <<<"$CONFIG")"
PGUSER_="$(grep -E 'POSTGRES_USER' <<<"$db_block" | head -n1 | sed -E 's/.*POSTGRES_USER:? *"?([^"]*)"?.*/\1/' || true)"
PGDB_="$(grep -E 'POSTGRES_DB' <<<"$db_block" | head -n1 | sed -E 's/.*POSTGRES_DB:? *"?([^"]*)"?.*/\1/' || true)"
PGUSER_="${PGUSER_:-phstore}"; PGDB_="${PGDB_:-$PGUSER_}"
psql_db() { "${DC[@]}" exec -T db psql -U "$PGUSER_" -d "$PGDB_" -Atc "$1"; }

"${DC[@]}" ps --status running --services 2>/dev/null | grep -qx db || die "the db service is not running"
head_before="$(psql_db 'SELECT version_num FROM alembic_version' || echo '?')"
say "database $PGDB_ (user $PGUSER_) — alembic head now: $head_before"

# --- 2. backup, or nothing — before anything, the git pull included -----------------------------------------------------
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="backups/phstore-${stamp}-before-${EXPECT_HEAD}.dump"
say "pg_dump -> $PHSTORE_DIR/$dump"
if (( ! DRY_RUN )); then
  "${DC[@]}" exec -T db pg_dump -U "$PGUSER_" -d "$PGDB_" -Fc > "$dump" \
    || { rm -f "$dump"; die "pg_dump failed — nothing was changed"; }
  [[ -s "$dump" ]] || { rm -f "$dump"; die "pg_dump wrote an empty file — nothing was changed"; }
  # a dump that pg_restore cannot list is not a backup
  "${DC[@]}" exec -T db pg_restore -l < "$dump" > /dev/null \
    || die "the dump does not read back with pg_restore -l — nothing was changed"
  chmod 600 "$dump"
  info "$(du -h "$dump" | cut -f1) — reads back with pg_restore"
fi

# --- 2b. the storage mount, now that there is a backup and before anything is recreated ----
ensure_storage_mount

# --- 3. is the code to deploy the code we mean? ------------------------------
if [[ "$MODE" == clone ]]; then
  repo="$(grep -oE 'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+' <<<"$api_block" | head -n1 | sed 's/\.git$//')"
  [[ -n "$repo" ]] || die "could not read the repository URL from the api command"
  branch="$(grep -oE -- '(-b|--branch)[ =]+[A-Za-z0-9_./-]+' <<<"$api_block" | head -n1 | awk '{print $NF}' | sed 's/.*=//' || true)"
  branch="${branch:-main}"
  raw="${repo/github.com/raw.githubusercontent.com}/${branch}/backend/migrations/versions/${EXPECT_HEAD}.py"
  info "code comes from $repo ($branch)"
  curl -fsS --max-time 20 -o /dev/null "$raw" \
    || die "$EXPECT_HEAD is not on $repo@$branch yet — push the branch first (git push), then rerun"
  remote_sha="$(git ls-remote "$repo" "refs/heads/$branch" 2>/dev/null | cut -c1-12 || true)"
  info "GitHub $branch is at ${remote_sha:-?} and carries $EXPECT_HEAD"
else
  [[ -d .git ]] || die "build setup but $PHSTORE_DIR is not a git checkout — cannot update ./backend"
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "local changes in $PHSTORE_DIR — commit or stash them first"
  run git fetch --quiet origin
  run git pull --ff-only
  [[ -f "backend/migrations/versions/${EXPECT_HEAD}.py" ]] || (( DRY_RUN )) \
    || die "backend/migrations/versions/${EXPECT_HEAD}.py is not in this checkout after the pull"
  info "checkout at $(git rev-parse --short HEAD)"
fi

# --- 4. roll ---------------------------------------------------------------------
if [[ "$MODE" == clone ]]; then
  # Recreated rather than restarted: a restarted container keeps its old
  # filesystem, a recreated one clones afresh. api first — it runs alembic
  # on boot — and jobs only once api is healthy, so the two never migrate
  # at the same time.
  say "recreating api (clones $repo and runs alembic on boot)"
  run "${DC[@]}" up -d --no-deps --force-recreate api
else
  say "building images from ./backend"
  build_services=(api migrate); (( has_jobs )) && build_services+=(jobs)
  run "${DC[@]}" build "${build_services[@]}"
  say "running migrations (one-shot migrate service)"
  run "${DC[@]}" run --rm --no-deps migrate
  say "recreating api"
  run "${DC[@]}" up -d --no-deps --force-recreate api
fi

# --- 5. healthy? ---------------------------------------------------------------
say "waiting for $HEALTH_URL (up to ${HEALTH_TIMEOUT}s)"
if (( ! DRY_RUN )); then
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  sleep 5
  until curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
      "${DC[@]}" logs --tail 40 api || true
      die "api not healthy after ${HEALTH_TIMEOUT}s. Backup: $PHSTORE_DIR/$dump (restore: see the end of this script)"
    fi
    sleep 4
  done
  info "healthy: $(curl -fsS --max-time 5 "$HEALTH_URL")"
fi

if (( has_jobs )); then
  say "recreating jobs"
  run "${DC[@]}" up -d --no-deps --force-recreate jobs
fi

if (( ! DRY_RUN )); then
  head_after="$(psql_db 'SELECT version_num FROM alembic_version' || echo '?')"
  say "alembic head: $head_before -> $head_after"
  [[ "$head_after" == "$EXPECT_HEAD" ]] || die "expected alembic head $EXPECT_HEAD, got $head_after"

  # A sign-up that cannot succeed: 88 is not a Mozambican mobile range, so
  # the answer is 422 when the route is live and 404 when it is not. (Not
  # "84 000 0000" — that one IS valid and would create an account.)
  api_base="${HEALTH_URL%/healthz}"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$api_base/v1/auth/signup" \
          -F pharmacy_name=Deploy-smoke -F owner_name=Smoke -F phone='88 000 0000' \
          -F password=12345678 -F city=MAPUTO -F accept_terms=true || true)"
  if [[ "$code" == 422 ]]; then info "POST /v1/auth/signup -> 422 on a non-mobile number: route is live"
  else die "POST /v1/auth/signup answered $code (expected 422)"; fi
  "${DC[@]}" ps
fi

say "done. Backup kept at $PHSTORE_DIR/$dump"
cat <<EOF
    To roll the DATABASE back to this backup (stops api/jobs, restores, restarts):
      ${DC[*]} stop api jobs
      ${DC[*]} exec -T db pg_restore -U $PGUSER_ -d $PGDB_ --clean --if-exists < $dump
      ${DC[*]} up -d api jobs
    (0009 is forward-only; the code that runs against a restored 0008 schema
     must be the previous commit, so in the clone setup revert on GitHub first.)
EOF
