#!/usr/bin/env bash
# Shared wrap-up evidence predicate for the wrap-up hook family.
#
# NOT a hook — a sourced library (never registered in plugin.json).
# Consumers: block_unverified_pr_create.sh, warn_stale_wrapup.sh, via:
#
#     source "$(dirname "$0")/wrapup_anchor.sh"
#
# The authored wrap-up (`code_health/pr_wrapup.md`, FOUNDATION §6) opens
# with `verified-at: <sha>`. Whether that header names the current HEAD
# is the one fact both hooks turn on: the create gate refuses to publish
# without it, and the post-push reminder stays quiet with it (a refresh
# is authored and about to be posted). One predicate, so the two can
# never disagree on what "names HEAD" means.

# wrapup_names_head <wrapup-file> <head-sha>
# True when the file's header lines carry a verified-at: naming the SHA
# (full or 7-char short form). False when the file is missing.
wrapup_names_head() {
    local wrapup="$1" head_sha="$2"
    [ -f "$wrapup" ] || return 1
    head -5 "$wrapup" | grep -qE "verified-at:.*(${head_sha}|${head_sha:0:7})"
}
