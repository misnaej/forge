bump: patch

- An emergency bypass is no longer burned by a publish that fails: `forge-pr-create` reserves it before creating the PR and spends it only once the PR exists, recording the PR number itself. A failure with no PR hands the bypass back for a retry; an unclear outcome keeps it reserved and names the recovery.
- `forge-emergency record-pr` refuses a bypass that no publish used and refuses to re-point a PR already recorded.
- `forge-emergency start` refuses while any earlier emergency is still recorded, so its ledger can no longer be orphaned.
- `forge-emergency end` refuses to quietly cancel an interrupted publish; a human settles it with `FORGE_EMERGENCY_ACK=1 forge-emergency end --abandon`. `status` reports the new CLAIMED state.
- The internal `forge-emergency consume` subcommand is removed; `forge-pr-create` was its only caller.
