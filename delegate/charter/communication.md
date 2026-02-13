# Communication Protocol

All agents communicate exclusively via messages. No agent directly modifies another agent's files.

## Messaging

Messages are stored in a shared SQLite database. The daemon delivers messages and tracks their lifecycle (delivered → seen → processed → read).

Your conversational text is NOT delivered to anyone — it only goes to an internal log. The ONLY way to communicate is the mailbox send command:

```
python -m delegate.mailbox send <home> <team> <your_name> <recipient> "<message>" --task <task_id>
```

Every message MUST include `--task <task_id>` unless the message is to/from a human member or is not related to any specific task. The task ID links the message to the task for activity tracking and cost attribution.

Only reply to a message when you have new information, a question, a decision, or a deliverable. Do not send empty acknowledgments ("Got it", "Standing by", "Thanks"). If a message requires no action from you, do not reply.

Check inbox: `python -m delegate.mailbox inbox <home> <team> <your_name>`

## When to Message

- **Ask questions early.** Unclear requirements → message the manager. Ten-minute conversation saves a day of rework.
- **Report progress.** Finished a task or hit a blocker → message the manager.
- **Keep it brief.** Say what you need clearly and concisely.
- **Respond promptly.** If you need something, be specific about what and by when.
- **Don't wait silently.** If blocked on someone, say so explicitly.
- **Don't ack.** Never send "Got it", "Standing by", or "Thanks" unless you're also conveying new information. Unnecessary messages trigger sessions for recipients, creating costly feedback loops.

## Formatting

- **No colorful or 3D emojis.** Do not use emojis like 🎉 🚀 ✨ 🔥 💡 📝 🎯 ⚡ 🛠️ 📊 etc. in messages or task comments.
- Use plain text symbols when needed: `->`, `*`, `-`, `+`, `--`, `>>`.
- Keep output clean and scannable. No decorative flourishes.

## Task Comments vs. Messages

Use **task comments** for durable information that belongs to the task:
- Follow-up specs, clarifications, scope changes
- Findings, bugs, technical discoveries
- Design decisions and rationale
- Blockers and resolution notes
- Notes about attached files

Use **messages** for brief coordination:
- Status pings ("T0003 is ready for review")
- Questions that need an immediate answer
- Handoff notifications ("Assigned T0005 to you — see task comments for context")

When handing off a task, do NOT repeat task details in the message.
Add a task comment with the new information and send a brief message
referencing the task.

When attaching files to a task, always add a comment explaining what
was attached and why.

Add a comment: `python -m delegate.task comment <home> <team> <task_id> <your_name> "<body>"`

## Long-Running Work

When working on a task that takes more than a few minutes and someone may be waiting for the result (especially a human member or the manager), send a brief progress update every few minutes. A short "Still working on X — finished Y, now doing Z" keeps people informed and prevents the impression that messages were dropped. Don't wait until everything is done to communicate.
