# Continuous Improvement

This document outlines practices for learning, reflection, feedback, and automation. Every team member should follow these practices to build a culture of continuous improvement.

## For All Agents

### 0. Acknowledge Before Deep Work

When you receive a message (especially from the boss or manager), **send a lightweight acknowledgment immediately** before diving into deep work. A quick "Got it, working on this now" or "Acknowledged, will have this shortly" lets the sender know you're on it. Don't make people wait in silence while you're heads-down for 10+ minutes.

### 1. Task Journals

After completing each task, write a brief journal entry capturing what happened and what you learned.

**Location:** `agents/<your-name>/journals/T<NNNN>.md`

**Template:**
```markdown
# T0017 — Build task detail side panel
**Date:** 2026-02-09
**Duration:** ~45 min
**Tokens:** ~12,000

## What I did
- [Brief summary of the work]

## What went well
- [Things that worked, good decisions]

## What I would do differently
- [Mistakes, inefficiencies, missed steps]

## Learnings
- [Specific takeaways to remember for future tasks]
```

Keep entries concise — a few bullet points per section is plenty. The goal is to capture lessons while they're fresh, not to write an essay.

### 2. Periodic Reflection

Every ~5 tasks (or when prompted by your manager), review your journal entries and update your reflections file.

**Location:** `agents/<your-name>/notes/reflections.md`

Look for:
- **Recurring patterns** — mistakes or successes that repeat
- **Improvement goals** — concrete things to work on
- **Growing skills** — areas where you're getting better
- **Blockers to efficiency** — things that slow you down that could be fixed

### 3. Automation

If you find yourself repeating the same manual steps across tasks:

- Write a shell script to automate it
- Save to `teams/<team>/shared/scripts/` (or `teams/<team>/scripts/` for team-wide scripts)
- Add a comment header explaining what it does and how to use it
- Tell the team about it so others benefit

Example: A pre-submit check that verifies your branch isn't stale before sending for review.

### 4. Peer Feedback

When you notice something that could help a teammate improve:

- **Send them a direct message** with your observation — be kind, specific, and constructive
- Focus on behaviors and patterns, not personality
- Include a concrete suggestion, not just criticism

When you receive feedback:
- Reflect on it honestly
- Save actionable feedback to `agents/<your-name>/feedback/from-<sender>-<date>.md`
- Consider updating your reflections.md with insights from the feedback

### 5. Code Quality Advocacy

If you touch a part of the codebase that feels too complex, hacky, or fragile:

- **Speak up** — tell the manager (or create a task yourself if you are the manager)
- Be specific: which file, what's wrong, what improvement you'd suggest
- Track observations in `agents/<your-name>/notes/tech-debt.md`

The goal is to keep the codebase healthy over time, not to accumulate tech debt silently.

### 6. Knowledge Sharing

Use the team's shared directory for internal knowledge sharing:

**Location:** `teams/<team>/shared/`

Structure:
- `shared/scripts/` — automation scripts anyone can use
- `shared/docs/` — team documentation, guides, patterns
- `shared/<agent-name>/` — agent-specific files others might find useful (avoids collision)

These files are not checked into git — they're for internal team knowledge. Use them for:
- Documenting patterns you've discovered
- Sharing useful code snippets or configurations
- Keeping reference material accessible to the team

---

## For Managers

### 7. Cost and Time Tracking

Track metrics for every merged task to understand team velocity and cost efficiency.

**Location:** `agents/<your-name>/notes/metrics.md`

Track per task:
- Assignee, duration, token count, cost
- Number of files changed, lines added/removed
- Number of rebase/rework cycles required

Periodically ask: which tasks cost more than expected? Where can we reduce tokens without sacrificing quality?

### 8. Team Model

Maintain a mental model of each team member's strengths, growth areas, and codebase ownership.

**Location:** `agents/<your-name>/notes/team-model.md`

For each person track:
- **Strengths** — what they do well
- **Growth areas** — where they can improve
- **Context/Ownership** — what parts of the codebase they know best
- **Task speed** — typical completion time
- **Review quality** — how thorough their work is

Update after each task cycle. Use this model to improve task assignments and code review pairings.

### 9. Feedback Culture

Proactively build a culture of feedback:

- After tasks, ask assignees what went well and what was hard
- Periodically ask people for feedback about work quality and review quality of teammates
- Keep a running log in `agents/<your-name>/notes/feedback-log.md`
- When patterns emerge, synthesize and share them kindly with the recipient
- Track whether feedback leads to improvement

### 10. Direct Communication

Encourage agents to communicate directly with each other for:
- Technical questions about code they own
- Design clarifications between designer and implementer
- Peer code review discussions
- Knowledge transfer

Not every message needs to go through the manager. A healthy team communicates laterally.

### 11. Codebase Health

Prioritize cleanup of modules that are:
- **Frequently modified** (high churn) — changes here affect many tasks
- **Complex/hacky** (high cognitive load) — slows down everyone who touches it
- **Both** (highest priority for cleanup)

The test for prioritization: will cleaning this up make us move faster on future tasks? If yes, create a task for it. Balance cleanup work against feature delivery — aim for sustainable pace.
