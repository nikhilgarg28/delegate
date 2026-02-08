# Communication Protocol

All agents communicate exclusively via messages. No agent directly modifies another agent's files. This constraint keeps the system predictable and auditable.

## How Messaging Works

Each team member has an inbox and outbox managed as Maildir directories:

- `inbox/new/` — unread messages delivered to you
- `inbox/cur/` — messages you've already read
- `outbox/new/` — messages you've sent, waiting to be routed
- `outbox/cur/` — messages that have been delivered

The daemon routes messages: it watches every member's outbox and delivers each message to the recipient's inbox.

## Sending Messages

Your conversational text is NOT delivered to anyone — it is only written to an internal log. The ONLY way to communicate with another team member or the director is by running the mailbox send command:

```
python -m scripts.mailbox send <root> <your_name> <recipient> "<message>"
```

For every message you receive, you should respond by running the send command. Do not just compose a reply in your head — actually execute the command.

## Checking Your Inbox

To see your unread messages:

```
python -m scripts.mailbox inbox <root> <your_name>
```

## When to Message

- **Ask questions early.** If something is unclear, message the manager or a relevant teammate. A ten-minute conversation now saves a day of rework later.
- **Report progress.** When you finish a task or hit a blocker, message the manager.
- **Keep it brief.** Say what you need to say clearly and concisely.

## Message Etiquette

- Respond promptly to messages in your inbox.
- If you need something from a teammate, be specific about what you need and by when.
- If you're blocked on someone, say so explicitly — don't wait silently.
