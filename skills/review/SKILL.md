---
name: review
description: Fetch and address PR review comments - pr-manager categorizes comments, then implement fixes and post replies. Use when the user wants to handle PR review feedback.
---

# Handle PR Review Comments

## Flow

1. **Detect PR number** if not in `$ARGUMENTS`:
   ```bash
   gh pr view --json number --jq '.number'
   ```

2. **Fetch and categorize comments** via `pr-manager`:
   ```
   Agent(subagent_type="pr-manager", prompt="Fetch and categorize all review comments for PR #<number>. Categorize each as: actionable fix, question, nit, or out-of-scope.")
   ```

3. After pr-manager reports the categorized comments, **implement fixes** for actionable items.

4. After each fix (or each logical batch of fixes), **commit** using the `/commit` skill.

5. **Reply to EVERY comment** with the resolution — see "Mandatory reply step" below. This is not optional.

## Mandatory reply step (FOUNDATION §6)

After fixes are committed, you MUST post a reply to **every single comment** in the review — actionable fixes, deferrals, questions, out-of-scope items. No exceptions.

The contract is one of these three forms:

### A. Fixed
```
✅ **Resolved in commit `<short_hash>`**

<one or two sentences: what was changed, where (file:line if useful).>
```

### B. Deferred / out-of-scope
```
⏸ **Deferred — filed as `<issue_url>`**

<one sentence on why this is out of scope for this PR.>
```

### C. Won't fix / decision documented
```
ℹ️ **Decision documented in `<file>`** (commit `<short_hash>`)

<one sentence: the design call, with link to where it lives.>
```

### How to post

GitHub's REST endpoint for replying to a review comment:

```bash
gh api repos/<owner>/<repo>/pulls/<PR#>/comments/<comment_id>/replies \
    --method POST \
    -f body="<reply text>"
```

Fetch comment IDs via:

```bash
gh api repos/<owner>/<repo>/pulls/<PR#>/comments \
    --jq '.[] | {id, path, line, body: (.body | .[0:80])}'
```

### Verification

Before declaring the review done, verify every comment ID has a reply:

```bash
# Each top-level comment should have at least one reply (in_reply_to set).
gh api repos/<owner>/<repo>/pulls/<PR#>/comments \
    --jq 'group_by(.in_reply_to_id // .id) | map({comment: .[0].id, replies: (length - 1)})'
```

Any row with `replies: 0` is an unreplied comment — fix before closing the review.

## Why this matters

A PR review without replies leaves reviewers guessing what changed in response to their feedback. Posting per-comment replies with commit hashes makes the review auditable, gives the reviewer a click-through to the fix, and closes the loop. **Skipping replies is non-compliance with FOUNDATION §6** — the comment is not addressed until the reviewer can see the explicit resolution.
