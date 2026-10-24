#!/usr/bin/env bash
# Offline smoke test for tools/auto_update.sh.
#
# Uses throwaway local git repos only — no network, no root, no systemd, and it
# never touches the real checkout. Because auto_update.sh decides everything
# from git state, the interesting branches (up to date / update available /
# dry run / wrong branch) are all reproducible here.
#
#   bash tests/test_auto_update.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/tools/auto_update.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }
has()  { if grep -q -- "$2" <<<"$1"; then ok "$3"; else bad "$3 — missing '$2' in: $1"; fi; }

[ -f "$SCRIPT" ] || { echo "missing $SCRIPT"; exit 1; }
bash -n "$SCRIPT" || { echo "syntax error in $SCRIPT"; exit 1; }

# --- fixture: a bare "origin", a seed clone to push from, a work checkout -----
git config --global init.defaultBranch main >/dev/null 2>&1
git -C "$TMP" init --quiet --bare origin.git
git -C "$TMP" clone --quiet origin.git seed 2>/dev/null   # empty first clone is expected
git -C "$TMP/seed" config user.email t@example.com
git -C "$TMP/seed" config user.name test
echo v1 > "$TMP/seed/README.md"
git -C "$TMP/seed" add README.md
git -C "$TMP/seed" commit --quiet -m v1
git -C "$TMP/seed" push --quiet origin main

git -C "$TMP" clone --quiet origin.git work
git -C "$TMP/work" config user.email t@example.com
git -C "$TMP/work" config user.name test
mkdir -p "$TMP/work/tools" "$TMP/work/dashboard" "$TMP/work/tests"
cp "$SCRIPT" "$TMP/work/tools/auto_update.sh"
# Minimal real Python targets so the updater's compile + unittest gates run
# offline in this throwaway checkout.
printf 'VALUE = 1\n' > "$TMP/work/dashboard/app.py"
printf 'import unittest\nclass Smoke(unittest.TestCase):\n def test_ok(self): self.assertEqual(1, 1)\n' > "$TMP/work/tests/test_smoke.py"
# Stub installer: the real one needs root, apt and systemd.
printf '#!/usr/bin/env bash\necho "stub install.sh ran"\n' > "$TMP/work/install.sh"
chmod 755 "$TMP/work/install.sh"

head_of() { git -C "$TMP/work" rev-parse HEAD; }
origin_head() { git -C "$TMP/origin.git" rev-parse main; }
run_update() { ECON_UPDATE_LOCK="$TMP/update.lock" "$@" bash "$TMP/work/tools/auto_update.sh" 2>&1; }

echo "test_auto_update.sh"

# 1. up to date -> no-op, exit 0, no restart
before="$(head_of)"
out="$(run_update)"
code=$?
check "up to date exits 0" "$code" "0"
check "up to date leaves HEAD alone" "$(head_of)" "$before"
has "$out" "nothing to do" "up to date is reported"

# 2. new commit on origin -> reset to it and run install.sh
echo v2 > "$TMP/seed/README.md"
git -C "$TMP/seed" commit --quiet -am v2
git -C "$TMP/seed" push --quiet origin main
out="$(run_update)"
code=$?
check "update exits 0" "$code" "0"
check "work tree follows origin" "$(head_of)" "$(origin_head)"
has "$out" "update available" "update is detected"
has "$out" "stub install.sh ran" "install.sh runs on change"
has "$out" "update complete" "success is logged"

# 3. failing test suite -> do not install, restore the previous checkout
echo v3 > "$TMP/seed/README.md"
git -C "$TMP/seed" commit --quiet -am v3
git -C "$TMP/seed" push --quiet origin main
before="$(head_of)"
printf 'import unittest\nclass Smoke(unittest.TestCase):\n def test_bad(self): self.fail("expected failure")\n' > "$TMP/work/tests/test_smoke.py"
out="$(run_update)"
code=$?
check "failed test gate exits non-zero" "$([ "$code" -ne 0 ] && echo yes)" "yes"
check "failed test gate restores previous HEAD" "$(head_of)" "$before"
has "$out" "test gate failed" "failed tests are reported"
has "$out" "previous checkout restored" "rollback is reported"
if grep -q "stub install.sh ran" <<<"$out"; then
  bad "failed tests must not run install.sh"
else
  ok "failed tests do not run install.sh"
fi
printf 'import unittest\nclass Smoke(unittest.TestCase):\n def test_ok(self): self.assertEqual(1, 1)\n' > "$TMP/work/tests/test_smoke.py"

# 4. dry run -> reports the pending update but changes nothing
echo v4 > "$TMP/seed/README.md"
git -C "$TMP/seed" commit --quiet -am v4
git -C "$TMP/seed" push --quiet origin main
before="$(head_of)"
out="$(run_update env ECON_UPDATE_DRY_RUN=1)"
code=$?
check "dry run exits 0" "$code" "0"
check "dry run leaves HEAD alone" "$(head_of)" "$before"
has "$out" "would reset --hard" "dry run reports intent only"

# 5. wrong branch -> refuses instead of merging main into it
git -C "$TMP/work" checkout --quiet -b feature
out="$(run_update)"
code=$?
check "wrong branch exits non-zero" "$([ "$code" -ne 0 ] && echo yes)" "yes"
has "$out" "expected 'main'" "wrong branch is refused"

# 6. a hand edit in the checkout is discarded by the next run (documented rule)
git -C "$TMP/work" checkout --quiet main
echo "local hack" >> "$TMP/work/README.md"
out="$(run_update)"
check "local edits do not block the update" "$(head_of)" "$(origin_head)"
if grep -q "local hack" "$TMP/work/README.md"; then
  bad "reset --hard discards local edits"
else
  ok "reset --hard discards local edits"
fi

# 7. lock held by an earlier run -> this run skips instead of overlapping
exec 9>"$TMP/update.lock"
flock -n 9 || bad "test harness could not take the lock"
out="$(run_update)"
code=$?
has "$out" "another update is still running" "a held lock makes the run skip"
check "a skipped run still exits 0" "$code" "0"
exec 9>&-
rm -f "$TMP/update.lock"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
