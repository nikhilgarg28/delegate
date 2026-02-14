# Code Review

## Workspace Isolation

Each agent works in their own git worktree, created automatically for tasks with a registered repo. Worktrees live in `~/.delegate/teams/<team>/agents/<agent>/worktrees/<repo>-T<NNNN>/`. Registered repos are symlinks in `~/.delegate/repos/` pointing to the real local repo. No clones — worktrees are created directly against the local repo.

## Branches

All work on feature branches: `delegate/<team_id>/<team>/T<NNNN>` (e.g., `delegate/3f5776/myteam/T0012`). The `<team_id>` is a 6-char hex ID generated at team creation to prevent branch collisions if a team is deleted and recreated. The team name is included for human readability. No direct pushes to main.

## Merge Flow

1. Agent completes work → sets task to `in_review`. Manager reassigns to a peer reviewer.
2. Reviewer reviews diff (base_sha → branch tip), runs tests, checks quality.
3. Approved → `in_approval`, manager reassigns to human. Rejected → `in_progress`, manager reassigns to DRI with feedback.
4. Human approves (manual) or auto-merge (auto repos).
5. Merge worker rebases onto main (or falls back to squash-reapply if rebase conflicts), runs tests. True content conflicts → task becomes `merge_failed`, manager notified with conflict details and `git reset --soft` instructions for the DRI.
6. Clean rebase/squash + tests pass → fast-forward merge → `done`, worktree cleaned up.

## Review Standards

Your approval means "this is correct, readable, tested, and consistent." Don't approve code with known bugs — every known issue is blocking. Your job is to find problems, not to confirm things work.

**Actually test the code.** Check out the branch, run the full test suite (not just tests related to the change), verify behavior, trigger edge cases. Don't just read the diff.

**Visual Inspection** For tasks involving UI, if you can, spin up a browser 
(potentially headless), click around on things, verify nothing is broken.

**Check task attachments** for specs or design references before reviewing. If the task involves UI and playwright is available, take screenshots and do a visual pass.

## Review Focus

1. **Correctness** — does it work? Did you verify by running it?
2. **Readability** — understandable without author explaining?
3. **Test coverage** — important paths tested? Edge cases covered?
4. **Consistency** — matches specs and conventions?
5. **Safety** — missing error handling? Exposed secrets? Unsanitized input? Auth gaps?

Don't rubber-stamp. If you aren't sure, dig deeper or ask.

## Review Report

Add your review as a comment on the task using the following format:
- **PASS**: what you verified and why you're confident.
- **FAIL**: specific issues with file, line, and description.

After that, send a brief message to the manager sharing your overall verdict and
concise summary.


Review turnaround: under 30 minutes when possible. Raise concerns as specific questions with suggested alternatives.
