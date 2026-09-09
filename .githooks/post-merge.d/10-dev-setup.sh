#!/usr/bin/env bash
# forge-only post-merge extension: keep this clone's env at the latest forge.
#
# Every merge can add an entry point, move a shipped hook, or change the
# plugin manifest; an editable install picks up code but not metadata, so
# the env goes stale silently and the next commit runs old gates.
# `./dev/setup.sh` is forge's one idempotent refresh (CLAUDE.md), and it
# runs here in the foreground so the very next command sees a fresh env.
# Consumers never get this file: `install-forge-githooks` only writes the
# named hook wrappers, and `.d/` scripts are repo-tracked (FOUNDATION §16).
#
# Skips: `FORGE_NO_AUTO_SETUP=1` (this shell only), any non-interactive
# context per forge.run_context (FOUNDATION §15 — CI, scripted pulls), and
# clones without dev/setup.sh.
set -uo pipefail

[ "${FORGE_NO_AUTO_SETUP:-}" = "1" ] && exit 0
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -x "$REPO_ROOT/dev/setup.sh" ] || exit 0

# forge.run_context owns "non-interactive" (FOUNDATION §15). The probe
# answers 0 (non-interactive → skip) or 3 (interactive → run); any other
# code means forge itself is not importable, and then a tty probe decides
# rather than a second copy of the CI markers.
python3 -c 'import sys; from forge.run_context import is_non_interactive; sys.exit(0 if is_non_interactive() else 3)' 2>/dev/null
probe=$?
if [ "$probe" -eq 0 ]; then
    exit 0
elif [ "$probe" -ne 3 ] && [ ! -t 1 ]; then
    exit 0
fi

echo "[forge] ${FORGE_HOOK_NAME:-post-merge}: refreshing this clone's env (./dev/setup.sh) so it runs the latest forge — set FORGE_NO_AUTO_SETUP=1 to skip once." >&2
(cd "$REPO_ROOT" && ./dev/setup.sh) || echo "[forge] ./dev/setup.sh failed — the env may be stale; re-run it by hand." >&2
exit 0
