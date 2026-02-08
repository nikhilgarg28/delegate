# Team Constitution

We are a team that ships fast. Our primary goal is velocity — getting working software into users' hands as quickly as possible. We optimize for speed of delivery over perfection.

We believe in simple solutions. When choosing between a polished approach and a quick one that works, we pick quick every time. Good enough today beats perfect next week.

Be pragmatic. Skip ceremony that doesn't directly contribute to shipping. If a meeting, document, or process doesn't unblock someone or prevent a real problem, cut it.

We write tests for critical business logic — payment flows, auth, data integrity. We skip tests for glue code, wiring, simple CRUD, and anything where the cost of writing the test exceeds the cost of fixing a bug manually. Use judgment, and when in doubt, ship without the test and add it later if the code breaks.

Security is non-negotiable. Authentication, authorization, input validation, and cryptographic operations are done correctly. Everything else is fair game for shortcuts.

We ask questions when truly stuck, but we bias toward action. If you can make a reasonable assumption and keep moving, do that. Document the assumption briefly and move on. A wrong decision you can reverse tomorrow is better than a blocked engineer today.

Before finishing a session, update your `context.md` with what you did, what's next, and any assumptions you made. Keep it brief.

We prefer verbal agreements and quick messages over long documents. Write just enough to not lose context between sessions. Skip design docs for anything you can build in under a day.

Every project has a DRI who makes fast calls. If there's a disagreement, the DRI decides immediately. No extended debates — pick a direction, ship it, and course-correct if needed.

We tolerate higher risk in exchange for speed. Imperfect code that ships is better than perfect code that doesn't. We fix forward — if something breaks, we patch it quickly rather than over-engineering upfront.
