#!/usr/bin/env bash
# econ dashboard — unattended updater, meant to be run from cron.
#
# Keeps this checkout in sync with ONE remote branch (default: main) and re-runs
# install.sh when the commit actually changed. install.sh is what restarts the
# service, so an update goes live exactly the way the manual deploy does.
#
#   sudo ./tools/auto_update.sh                     # update if origin/main moved
#   ECON_UPDATE_DRY_RUN=1 ./tools/auto_update.sh    # report only, change nothing
#
# Environment overrides:
#   ECON_UPDATE_BRANCH=main              branch to track (default: main)
#   ECON_UPDATE_DRY_RUN=1                report what would happen; touch nothing
#   ECON_UPDATE_LOCK=/path/to.lock       lock file (default /var/lock/econ-update.lock)
#
# Safety gates:
#   * Every changed revision must pass Python compilation and the offline
#     unittest suite before install.sh can restart the production service.
#     A failing revision is rolled back to the previous local HEAD.
#   * install.sh retains its import smoke test as a final pre-restart gate.
#   * `git reset --hard origin/<branch>` matches the documented manual deploy
#     (`git fetch --all && git reset --hard origin/main && ./install.sh`), so
#     ANY hand edit inside /opt/econ is discarded on the next run. Edit in git,
#     never on the server.
#   * The service is restarted ONLY when the commit actually changed, so an
#     idle cron run never disturbs the dashboard.
#   * Runs are serialised with flock, so a slow apt step cannot overlap the next
#     scheduled run.

set -uo pipefail

BRANCH="${ECON_UPDATE_BRANCH:-main}"
DRY_RUN="${ECON_UPDATE_DRY_RUN:-0}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${ECON_UPDATE_LOCK:-/var/lock/econ-update.lock}"

log() { printf '%s [econ-update] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*"; exit 1; }
short() { cut -c1-8 <<<"$1"; }

current_branch() { git -C "$APP_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null; }
local_head() { git -C "$APP_DIR" rev-parse HEAD 2>/dev/null; }
remote_head() { git -C "$APP_DIR" rev-parse --verify "refs/remotes/origin/$1" 2>/dev/null; }

main() {
  command -v git >/dev/null 2>&1 || die "git not found in PATH"
  [ -d "$APP_DIR/.git" ] || die "$APP_DIR is not a git checkout"

  if [ "$(id -u)" -ne 0 ]; then
    log "WARNING: not running as root — install.sh will refuse unless this checkout is owned by you"
  fi

  # One run at a time; skip (do not queue) if the previous one is still going.
  exec 9>"$LOCK_FILE" 2>/dev/null || die "cannot open lock file $LOCK_FILE"
  flock -n 9 || { log "another update is still running; skipping this run"; exit 0; }

  local on_branch
  on_branch="$(current_branch)"
  [ -n "$on_branch" ] || die "cannot determine the current branch of $APP_DIR"
  [ "$on_branch" = "$BRANCH" ] || die "checkout is on '$on_branch', expected '$BRANCH' — check out $BRANCH or set ECON_UPDATE_BRANCH"

  log "checking $APP_DIR against origin/$BRANCH"
  git -C "$APP_DIR" fetch --quiet --all || die "git fetch failed (offline, DNS, or credentials?)"

  local before after
  before="$(local_head)"
  after="$(remote_head "$BRANCH")"
  [ -n "$after" ] || die "origin/$BRANCH does not exist after fetch"

  if [ "$before" = "$after" ]; then
    log "up to date at $(short "$after"); nothing to do"
    exit 0
  fi

  log "update available: $(short "$before") -> $(short "$after")"
  if [ "$DRY_RUN" = "1" ]; then
    log "dry run: would reset --hard to origin/$BRANCH and re-run install.sh"
    exit 0
  fi

  # Force convergence with the remote branch: identical to the manual deploy and
  # the only way an unattended job can guarantee it never blocks on local edits.
  git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH" || die "git reset --hard failed"
  log "working tree now at $(short "$(local_head)")"

  log "running Python compile and offline test gates"
  if ! (
    cd "$APP_DIR" &&
    python3 -m py_compile dashboard/*.py tests/*.py &&
    python3 -m unittest discover -s tests
  ); then
    log "test gate failed; rolling back to $(short "$before")"
    git -C "$APP_DIR" reset --hard --quiet "$before" || die "test failed and rollback reset failed"
    die "test gate failed; previous checkout restored and service was not restarted"
  fi

  if ! "$APP_DIR/install.sh"; then
    log "install.sh failed; rolling checkout back to $(short "$before")"
    git -C "$APP_DIR" reset --hard --quiet "$before" || die "install failed and rollback reset failed"
    if "$APP_DIR/install.sh"; then
      die "install.sh failed; previous checkout restored and reinstalled"
    fi
    die "install.sh and rollback install both failed; inspect service and logs"
  fi
  log "update complete at $(short "$(local_head)")"
}

# Sourcing the file (tests) must not run an update.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
