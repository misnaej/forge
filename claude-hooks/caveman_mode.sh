#!/usr/bin/env bash
# Put the main conversation (SessionStart) and every helper agent
# (SubagentStart) into caveman full grammar: every word an agent writes is
# re-read on every later request, so terse traffic is cheaper traffic.
# Text people read stays plain English (FOUNDATION §8). Grammar and levels
# from the caveman plugin (https://github.com/JuliusBrussee/caveman); forge
# ships only the instruction, so that plugin is not required.
#
# The hook only delivers the instruction; it cannot make a model obey it.
# `forge-agent-profile --tokens` measures whether reports shrink.
#
# Opt-out via [tool.forge.hooks] in the repo's pyproject.toml:
#   caveman = false        # deliver nothing
#   (unset / true)         # on (default)
set -e
INPUT=$(cat)
EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // empty')
case "$EVENT" in
    SessionStart | SubagentStart) ;;
    *) exit 0 ;;
esac

# Defaults to on when pyproject / python / the key is absent or unreadable.
ENABLED=$(
    python3 - "${CLAUDE_PROJECT_DIR:-.}/pyproject.toml" 2>/dev/null <<'PY' || echo on
import sys, tomllib

try:
    with open(sys.argv[1], "rb") as fh:
        val = (
            tomllib.load(fh)
            .get("tool", {})
            .get("forge", {})
            .get("hooks", {})
            .get("caveman", True)
        )
except (OSError, tomllib.TOMLDecodeError):
    val = True
print("off" if val is False else "on")
PY
)
[ "$ENABLED" = "off" ] && exit 0

CONTEXT='[forge] Caveman full for agent traffic (your replies, briefs, reports): drop articles, filler, pleasantries, hedging; fragments OK; short synonyms; lead with the verdict. Keep whole words (no abbreviations). Never alter code, symbols, paths, commands, names, numbers or error text. Plain concise English instead for text people read: PR descriptions, issue and comment bodies, commit messages, human-facing docs, security warnings and confirmations of irreversible actions.'

jq -n --arg event "$EVENT" --arg context "$CONTEXT" \
    '{hookSpecificOutput: {hookEventName: $event, additionalContext: $context}}'
