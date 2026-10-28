bump: minor

- FOUNDATION §14 adds "Newer issues get more attention": within a tier, a recently filed issue is triaged and recommended before an older one, judged by filing date (`createdAt`), never `updatedAt`, which every label or comment bumps. `issue-triage`'s `recommend-next` now weights by filing date instead of last update.
