bump: minor

- FOUNDATION §1 adds "One question at a time": with several questions to ask, an agent names them in one line, then asks each and waits for its answer before the next; one question per `AskUserQuestion` call. Questions are batched only where nobody answers live (a PR or issue comment). `/plan-issue`, `/plan-batch` (which put every grouping decision in one round) and `/c4` now follow it.
