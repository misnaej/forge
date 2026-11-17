#!/usr/bin/env bash
# Block edits to protected files (tokens, git config, ruff config, env files)
set -e
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Hard-block: secrets and local config (no legitimate edit reason)
if echo "$FILE_PATH" | grep -qE '(\.env|\.hf_token|git\.config\.local|gitconfig\.local)$'; then
    echo "BLOCKED: $FILE_PATH is protected. If the user wants to edit it, they should do so manually outside Claude Code." >&2
    exit 2
fi

# Soft-block: ruff config (changes possible but require user agreement)
if echo "$FILE_PATH" | grep -qE '(ruff\.toml)$'; then
    echo "BLOCKED: Ruff config files are protected. Explain the proposed change to the user and let them edit manually if they agree." >&2
    exit 2
fi
