#!/bin/bash
# Contract test: no Python source should reference JSON state files that have
# been removed. When a migration deletes a shared state file (as happened with
# trial-state.json and discovered-rss.json in cc680c4 / 050e1ac), all readers
# must be updated — otherwise the reader silently returns empty, and the bug
# only surfaces when downstream state gets wiped (as happened on 2026-04-21).
#
# Usage:   scripts/check-deleted-state-refs.sh
# Exit 0:  no forbidden refs
# Exit 1:  forbidden ref(s) found

set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."

# File names that no longer exist. Add new entries when removing shared state.
DELETED=(
    "trial-state.json"
    "discovered-rss.json"
)

# Paths excluded from the scan. The migration script documents these names in
# its header by design; the registry module's docstring and this test file
# itself reference them for historical context.
EXCLUDE_REGEX='(scripts/migrate_to_registry\.py|rss_registry\.py|tests/test_deleted_state_refs\.py|scripts/check-deleted-state-refs\.sh|unified-global-news-sender\.py:.*legacy trial-state\.json was removed)'

status=0
for name in "${DELETED[@]}"; do
    hits=$(grep -rn --include='*.py' --include='*.sh' "$name" . 2>/dev/null \
           | grep -v -E "$EXCLUDE_REGEX" || true)
    if [ -n "$hits" ]; then
        echo "FAIL: references to deleted state file '$name' found in:"
        echo "$hits" | sed 's/^/  /'
        status=1
    fi
done

if [ $status -eq 0 ]; then
    echo "OK: no forbidden references to deleted state files."
fi
exit $status
