#!/usr/bin/env bash
# Wire git to use .githooks/ for hooks. Idempotent.
#
# Usage:  bash .githooks/install.sh
# Called by dev/setup.sh and runnable directly by contributors.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

CURRENT="$(git config --get core.hooksPath || true)"
if [ "$CURRENT" = ".githooks" ]; then
    echo "✓ core.hooksPath already .githooks"
else
    git config core.hooksPath .githooks
    echo "✓ core.hooksPath → .githooks"
fi

chmod +x .githooks/pre-commit 2>/dev/null || true
