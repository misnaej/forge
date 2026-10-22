bump: patch

Internal: the places that shorten long text before it reaches a PR body, a CI log, a terminal or a report now share one helper, and the test suite runs real git through one shared helper. Every shortened output is byte-for-byte what it was before; nothing a consumer sees changes.
