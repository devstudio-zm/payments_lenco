#!/usr/bin/env bash
# Two concurrent settlers for one successful Lenco collection.
#   race.sh <site>             both settlers return cleanly and exactly 1 payment entry exists
#   race.sh <site> --unlocked  must go wrong: a duplicate entry, or (on MariaDB with
#                              innodb_snapshot_isolation) a settler that crashes with a
#                              write conflict, which Lenco would see as a failed webhook
# Run from the bench directory on a test site (allow_tests). See race.py.
set -euo pipefail
SITE="${1:?site}"
UNLOCKED=0; [[ "${2:-}" == "--unlocked" ]] && UNLOCKED=1
M=payments_lenco.tests.race

read -r _ PR REF < <(bench --site "$SITE" execute "$M.prepare" 2>/dev/null | grep '^RACE ')
AT=$(python3 -c 'import time; print(time.time() + 6)')
OUT=$(mktemp -d)
for i in 1 2; do
  bench --site "$SITE" execute "$M.racer" --kwargs "{\"reference\": \"$REF\", \"at\": $AT, \"unlocked\": $UNLOCKED}" >"$OUT/$i" 2>&1 &
done
wait
CLEAN=$(cat "$OUT"/1 "$OUT"/2 | grep -c '^RACER' || true)
rm -rf "$OUT"
N=$(bench --site "$SITE" execute "$M.count" --kwargs "{\"payment_request\": \"$PR\"}" 2>/dev/null | grep '^PAYMENT_ENTRIES' | awk '{print $2}')
echo "Payment Request $PR: $N payment entries, $CLEAN of 2 settlers returned cleanly (unlocked=$UNLOCKED)"
if [[ $UNLOCKED == 1 ]]; then
  if [[ "$N" -gt 1 || "$CLEAN" -lt 2 ]]; then echo "PASS: without the lock the race goes wrong, so the test can fail"
  else echo "FAIL: nothing went wrong without the lock; the test cannot detect the bug"; exit 1; fi
else
  if [[ "$N" == 1 && "$CLEAN" == 2 ]]; then echo "PASS: settled exactly once and both settlers returned cleanly"
  else echo "FAIL: expected 1 entry and 2 clean settlers"; exit 1; fi
fi
