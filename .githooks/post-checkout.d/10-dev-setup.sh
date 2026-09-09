#!/usr/bin/env bash
# forge-only post-checkout extension: same refresh as post-merge.d — a
# branch switch changes which entry points and hooks the tree declares.
# post-checkout's runner already skips file-level checkouts (branch flag).
FORGE_HOOK_NAME="post-checkout" exec "$(dirname "$0")/../post-merge.d/10-dev-setup.sh"
