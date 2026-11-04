bump: patch

- The `caveman_mode` instruction now says a helper's report back to the agent that started it is agent traffic too, so it is written in caveman full even when a person may also read it (live testing showed helpers kept writing reports in full prose). `forge-agent-profile --tokens` adds a `long-report` waste flag for reports of 2,000 characters or more, so whether reports actually shrink shows up in `/perf`.
