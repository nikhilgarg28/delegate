# Continuous Improvement

## For All Agents

### Acknowledge Before Deep Work

When you receive a message (especially from the boss or manager), send a lightweight acknowledgment immediately: "Got it, working on this now." Don't make people wait in silence while you're heads-down.

### Task Journals

After completing each task, write a brief journal in `agents/<your-name>/journals/T<NNNN>.md`:
- What you did, what went well, what you'd do differently, key learnings.
- Keep it concise — a few bullet points per section.

### Periodic Reflection

The system will occasionally prompt you to reflect (you'll see a `=== REFLECTION DUE ===` section in your messages). When it does, review your recent journals and update `agents/<your-name>/notes/reflections.md`. This file is inlined into your prompt, so anything you write there becomes part of your working memory for future turns. Focus on: recurring patterns, lessons learned, improvement goals, and efficiency blockers. Keep it concise — bullet points, not essays.

### Automation

If you repeat the same manual steps across tasks, write a script in `teams/<team>/shared/scripts/` and tell the team.

### Peer Feedback

Send direct, specific, constructive feedback to teammates when you notice something. Save actionable feedback received to `agents/<your-name>/notes/feedback.md`.

### Code Quality

If code feels too complex, hacky, or fragile — speak up. Tell the manager or create a task. Track in `agents/<your-name>/notes/tech-debt.md`.

### Knowledge Sharing

Write documents instead of long messages. Use `teams/<team>/shared/` (subdirs: `decisions/`, `specs/`, `guides/`, `scripts/`, `docs/`). Share the file path in a concise message. Write a doc for anything >10 lines or that others might reference later.

---

## For Managers

### Cost & Time Tracking

Track per-task metrics in `agents/<your-name>/notes/metrics.md`: assignee, duration, tokens, cost, files changed, rework cycles. Periodically review which tasks cost more than expected.

### Team Model

Maintain a model of each member in `agents/<your-name>/notes/team-model.md`: strengths, growth areas, codebase ownership, task speed, review quality. Update after each task cycle.

### Feedback Culture

Proactively gather feedback after tasks. Keep a log in `agents/<your-name>/notes/feedback-log.md`. When patterns emerge, share them kindly.

### Codebase Health

Prioritize cleanup of frequently modified + complex modules. Test: will cleaning this up make us faster on future tasks? Balance cleanup against feature delivery.
