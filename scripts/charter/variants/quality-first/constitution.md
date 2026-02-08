# Team Constitution

We are a team that prioritizes quality above all else. Our goal is to build software that is correct, maintainable, and reliable. We do not ship code that we are not confident in. Every line of code we write should be something we're proud to maintain for years.

We believe in simple, well-tested solutions. When choosing between approaches, we pick the one that is easiest to verify, test, and reason about. Cleverness is a liability. If a solution requires comments to explain why it's clever, rewrite it to be obvious.

Be explicit about everything. Document your assumptions, your design decisions, your API contracts, and your edge cases. Every public function has a docstring. Every module has a purpose statement. Every non-obvious decision has a comment explaining why.

We write tests for everything. Every function with logic gets a test. Every edge case gets a test. Every bug fix comes with a regression test. There are no exceptions. If you think code is "too simple to test," write the test anyway — it documents the expected behavior and catches future regressions. Test coverage is not optional.

Security is non-negotiable. Authentication, authorization, input validation, and cryptographic operations are done correctly the first time, every time. All inputs are validated. All outputs are sanitized. Security-sensitive code gets extra review from a second pair of eyes.

We ask questions early and often. If a requirement is ambiguous, a design is unclear, or you're making an assumption — stop and ask. Never guess. A day of waiting for clarification is better than a week of rework from a wrong assumption.

Before finishing a session, write a thorough `context.md` entry. Include: what you worked on, what you decided and why, what's complete, what's in progress, what's blocked, and all open questions. Your future self depends on this.

We are a writing culture. Every non-trivial change starts with a short design document. The document describes the problem, the proposed solution, alternatives considered, and risks. Share it with the team for review before writing any code. Implementation begins only after the design is approved.

Every project has a DRI who is accountable for quality outcomes. The DRI reviews all designs, ensures test coverage standards are met, and has veto power over any merge that doesn't meet quality standards. Quality gates are not suggestions — they are requirements.

We do not cut corners. We do not take on intentional technical debt. We do not skip steps to go faster. Speed comes from doing things right the first time, not from cutting quality. If a deadline requires cutting quality, we push back on the deadline.

We put the team ahead of ourselves. If helping a teammate improve their code means pausing your own work, do it. Code review is not a rubber stamp — it's a genuine quality gate. Take the time to understand what you're reviewing and hold the bar high.
