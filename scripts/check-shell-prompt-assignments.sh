#!/bin/bash
# Contract check: shell scripts with multi-line variable assignments
# (VAR="<newline>...\n...") must include a runtime guard that aborts
# cleanly if the assignment accidentally fails to populate the variable.
#
# Why: on 2026-04-22, rss-source-discovery.sh line 97 contained an
# unescaped "The Guardian World" inside a multi-line double-quoted PROMPT
# string. bash parsed it as a command-prefix assignment (PROMPT=<value>
# Guardian <args>) instead of a shell-variable assignment. `bash -n`
# passed — the syntax was technically valid. PROMPT was never set, and
# the script crashed ~100 lines later with an opaque "unbound variable"
# error. A one-line guard (`: "${PROMPT:?...}"`) immediately after the
# assignment would have failed fast with a clear diagnostic.
#
# Usage:   scripts/check-shell-prompt-assignments.sh
# Exit 0:  every multi-line assignment has a guard somewhere in the file
# Exit 1:  one or more missing guards

set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."

status=0
shopt -s nullglob

for sh in scripts/*.sh; do
    case "$sh" in
        scripts/check-*.sh) continue ;;
    esac

    # Find multi-line assignments in any of these forms:
    #   VAR="…       export VAR="…       (with optional leading whitespace)
    #   var="…       export var="…       (any case)
    # awk strips escaped quotes (\") before counting real quotes; an odd count
    # on the opening line means the string is still open (multi-line). A state
    # machine then finds the closing line so we can report start:var:end.
    starts=$(awk '
        BEGIN { inside = 0 }
        {
            if (inside == 1) {
                line = $0
                gsub(/\\"/, "", line)
                m = gsub(/"/, "", line)
                if (m % 2 == 1) {
                    print start_nr ":" var ":" NR
                    inside = 0
                }
            } else if (/^[[:space:]]*(export[[:space:]]+)?[a-zA-Z_][a-zA-Z0-9_]*="/) {
                line = $0
                gsub(/\\"/, "", line)
                m = gsub(/"/, "", line)
                if (m % 2 == 1) {
                    tmp = $0
                    gsub(/^[[:space:]]*(export[[:space:]]+)?/, "", tmp)
                    match(tmp, /^[a-zA-Z_][a-zA-Z0-9_]*/)
                    var = substr(tmp, RSTART, RLENGTH)
                    start_nr = NR
                    inside = 1
                }
            }
        }
    ' "$sh")

    [ -z "$starts" ] && continue

    while IFS=: read -r lineno var endlineno; do
        [ -z "$var" ] && continue

        # Accept any of these guard forms after the closing quote of the
        # assignment (not from the opening line, to avoid matching guard-like
        # text inside the multi-line string itself):
        #   : "${VAR:?msg}"           fail-fast on empty/unset
        #   [ -z "${VAR:-}" ] ...     manual check
        #   [[ -z "${VAR:-}" ]] ...   (same, bash [[)
        #   [ -n "${VAR:-}" ] || ...  positive form
        #   [[ -n "${VAR:-}" ]] ...
        # Also accept bare "$VAR" in the patterns (without ${...}).
        guard_regex="\\\$\\{${var}:\\?|-[zn][[:space:]]+\"?\\\$\\{?${var}[:-}\" ]"

        guard_start=$((endlineno + 1))
        if ! tail -n +"$guard_start" "$sh" | grep -Eq "$guard_regex"; then
            echo "FAIL: $sh:$lineno  multi-line assignment '$var=\"...\"' has no runtime guard."
            echo "      Add immediately after the closing quote:"
            echo "        : \"\${$var:?$var assignment failed — check for unescaped double quotes in the multi-line string}\""
            status=1
        fi
    done <<< "$starts"
done

if [ $status -eq 0 ]; then
    echo "OK: all multi-line shell variable assignments have runtime guards."
fi
exit $status
