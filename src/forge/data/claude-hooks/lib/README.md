# claude-hooks/lib — shared sourced libraries

NOT hooks — sourced libraries, never registered in `plugin.json` or
`hooks.toml`. Each hook sources the lib it needs fail-closed:

```bash
CMD_LIB="$(dirname "$0")/lib/command_utils.sh"
if [ ! -r "$CMD_LIB" ]; then
    echo "BLOCKED: command-utils lib missing at $CMD_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$CMD_LIB"
```

Only `exit 2` is a block signal in the PreToolUse contract, so a missing lib
must deny, never silently disarm the guard.

## command_utils.sh

The command-parsing half of the guard family, moved verbatim from
`git_anchor.sh` (which sources it and adds the git/gh anchors and the
`guard_begin` watchdog on top):

- `parse_or_block <jq-filter>` — print jq's answer for the payload in
  `$INPUT`, or deny (exit 2) on an unparseable payload.
- `parse_command_segment` — the single extraction point for
  `.tool_input.command` in every guard hook; prints the command (empty when
  the tool call carries none), denies on an unparseable payload.
- `command_positions [--words] [--no-redir] <command>` — the
  command-positions view of a Bash command: quoted spans blanked (a mention
  is not a command), `$(…)` kept as `(…)` so substitutions are scanned,
  redirections blanked with `--no-redir` so segments split cleanly at
  `;` `&` `|` `(` `)`. See the long comment above the scanner in this file.
- `guard_unredir <text>` — the redirection-blanking pass alone.
- `GUARD_WRAPPERS`, `GUARD_NEUTRAL`, `_GUARD_UNREDIR_AWK`, `_GUARD_CMDPOS_AWK`
  — the scanner's inputs.

When the awk scanner itself is unavailable, `command_positions` falls back
to the raw text; guards that need the views' precision must therefore also
scan the raw command (see `block_vibe_config.sh` for the pattern).
