# Contributing to forge

Forge is open source and maintained around its owner's own work. Until it
has been consolidated — expected in early 2027 — it does not take on new
directions. Contributions are welcome within that limit, and this page
says what the limit is. Below, a *maintainer* is anyone with write access
to this repository.

To adopt forge in your own repository, read the [README](README.md)
instead; this page is for changing forge itself.

## The rules

1. **Contributing means using forge.** Report a problem from a repository
   that runs forge: record it in your intake with `forge-intake new`, then
   file it with `/report-to-forge`, which captures versions and evidence,
   checks the record for private details and asks you to confirm, and
   searches for a duplicate first.
2. **Issues filed any other way are triaged, not lost.** An issue from
   someone who is not a maintainer that did not come through
   `/report-to-forge` is labelled, and a maintainer replies: we will fix
   it, we invite you to fix it, or we do not plan to and say why — you
   are welcome to argue the case.
3. **Outside pull requests are by invitation.** If you are not a
   maintainer, open a PR only on an issue a maintainer has invited you to work on,
   from your fork. Other PRs are closed with a pointer here.
   Contributors whose work lands are credited in [Contributors](#contributors).
4. **Nothing changes existing behaviour without the owner's approval.**
   Approval is a plan the owner validated with `/plan-issue` before work
   started. A PR that changes how an existing feature behaves without
   one is flagged in review. Fixes and additions must not break current
   users.
5. **Security problems go privately**, through GitHub's
   [private vulnerability reporting](https://github.com/misnaej/forge/security/advisories/new),
   never in a public issue.

## What to expect

There is no guaranteed response time; replies come when they come.

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
Contributions are accepted under the repository's [MIT licence](LICENSE).

## Working on forge

Forge is developed with forge: the same hooks, agents and checks it
ships run on its own repository.

```bash
git clone git@github.com:misnaej/forge.git
cd forge
./dev/setup.sh
```

`dev/setup.sh` creates the development environment and wires forge into
this checkout. It is safe to re-run, and it is the only install command
— never run `pip install` yourself. Then work in Claude Code with the
forge plugin:

- Every PR targets `main` and carries a changelog fragment
  (`changelog.d/<slug>.<type>.md`, first line `bump: patch|minor|major`).
  Never edit `CHANGELOG.md` or the plugin version.
- `git commit` runs the full pre-commit battery; `/pr` runs the reviews
  and posts the wrap-up and squash message. The owner merges.
- Releases are cut by the owner with `forge-changelog release-pr`.

Repository conventions live in [CLAUDE.md](CLAUDE.md) and the engineering
rules in [FOUNDATION.md](FOUNDATION.md). Release mechanics:
[docs/release-process.md](docs/release-process.md). Adding a pre-commit
step: [docs/step-invocation.md](docs/step-invocation.md).

## Contributors

- Jean Simonnet — owner
