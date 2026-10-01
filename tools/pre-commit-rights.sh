#!/usr/bin/env bash
# Refuse to commit any change that would put a non-public-domain work in git.
#
# The rights gate in CI already fails the push; this stops the bad commit from
# existing locally in the first place, so a later force-push or a forgotten
# workflow cannot leak it either.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

STAGED_BOOKS="$(git diff --cached --name-only --diff-filter=ACMR | grep -E '^books/.*/book\.json$' || true)"

if [ -z "$STAGED_BOOKS" ]; then
  exit 0
fi

echo "pre-commit: staged book metadata detected, verifying licences..."
if ! python3 tools/check_rights.py; then
  cat >&2 <<'EOF'

pre-commit: BLOCKED.

A staged book.json declares a licence that is not public domain, matches the
denied-author list, or claims public domain without a supportable death year.

Unstage the offending book, or fix books/<slug>/book.json. If the work has no
free Vietnamese edition, that is expected: set status=blocked-no-vi-source and
leave it without audio. Do not machine-translate to make it look legitimate.

See CONTRACT.md, section "A declared licence is a claim, not proof".
EOF
  exit 1
fi

exit 0