#!/usr/bin/env bash
# Shared command anchors for the git- and gh-guard hook family.
#
# NOT a hook — a sourced library (never registered in plugin.json).
# Consumers: block_force_push.sh, block_git_rebase.sh, block_raw_git.sh,
# block_git_destructive.sh, block_amend_pushed_commit.sh,
# block_protected_branches.sh, block_no_verify.sh,
# block_claude_attribution.sh, block_pr_merge.sh,
# block_unverified_pr_create.sh, warn_generated_conflicts.sh,
# warn_stale_wrapup.sh, via:
#
#     source "$(dirname "$0")/git_anchor.sh"
#
# GIT_ANCHOR matches a real `git <subcommand>` invocation at line-start or
# after a shell separator (`;` `&` `|` `(`), so guarded words inside quoted
# bodies (a commit message, PR description) never fire. It tolerates:
# - a leading run of `VAR=val` assignments and a `command`/`env`/`exec`/
#   `builtin`/`sudo` wrapper (incl. wrapper flag tokens like `sudo -n`),
#   so `GIT_DIR=x git ...` can't slip the gate;
# - a bounded run of git GLOBAL options between `git` and the subcommand
#   (`--no-pager`, `-c k=v`, `-C <dir>`, `--git-dir=<x>`), so
#   `git --no-pager <verb>` can't slip it either.
#
# SEG_ANCHOR is the same shape anchored to segment start, for hooks that
# split a compound command at separators and evaluate each segment.
#
# Known accepted residuals of BOTH anchors (documented, deliberately
# out of scope — see issue #348's review notes): `eval "..."` and a
# backslash-newline continuation between the words; space-separated arg-taking globals other
# than -c/-C (`--git-dir x`), multi-arg wrapper flags (`sudo -u root`),
# and the shell-obfuscation class (`bash -c "git ..."`, `${IFS}`, xargs).
GIT_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*git[[:space:]]+((-c|-C)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][a-zA-Z-]*(=[^[:space:]]*)?[[:space:]]+)*'
# shellcheck disable=SC2034  # consumed by sourcing hooks
SEG_ANCHOR='^[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*git[[:space:]]+((-c|-C)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][a-zA-Z-]*(=[^[:space:]]*)?[[:space:]]+)*'

# GH_ANCHOR is the same shape for `gh`: a real invocation at line-start
# (leading whitespace included — the hand-rolled `^gh` patterns this
# replaced were bypassed by a single leading space) or after a shell
# separator, tolerating the same VAR=val / wrapper-token prefix run.
# gh also takes global options before its subcommand, including the
# arg-bearing `-R` / `--repo` (`gh --repo o/r pr view 1` is ordinary
# syntax), so the same bounded global-option run GIT_ANCHOR allows
# follows `gh` here.
# shellcheck disable=SC2034  # consumed by sourcing hooks
GH_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*gh[[:space:]]+((-R|--repo)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][a-zA-Z-]*(=[^[:space:]]*)?[[:space:]]+)*'
