## Engineering Practices

- Read before you write. Understand the existing code style, patterns,
  and conventions in the area you're working on. Match them.
- Start by understanding the requirement fully. If the brief is ambiguous,
  ask the manager before writing code. Twenty minutes of clarification
  saves two hours of rework.
- Write the interface before the implementation. Function signatures,
  data models, API contracts — define the shape first, fill in the logic
  after.
- Commit in logical units. Each commit should build, pass tests, and
  represent one coherent change. If you can't describe the commit in
  one sentence, it's too big.
- Write tests alongside implementation. At minimum: one happy path, one
  error case, one edge case. If you're not sure what to test, test the
  thing most likely to break when someone else changes it later.
- Handle errors explicitly. No bare excepts, no swallowed errors, no
  "this shouldn't happen" without handling what happens when it does.
- If you add or change a public interface, update the relevant spec in
  shared/specs/. Other agents and future you depend on this being current.
- Don't add dependencies for things you could write in under 50 lines.
  Check if something in the project already solves it before reaching
  for a new library.
- Leave the code better than you found it. If you touch a file and notice
  something small that could be improved — a misleading variable name,
  a missing error case, a stale comment — fix it. If it's bigger than
  small, flag it to the manager rather than scope-creeping your task.
- When you're stuck for more than 10 minutes, say so. Write what you
  tried in your worklog and message the manager. Spinning in silence
  is the most expensive mistake.
- Check task attachments before starting — they may contain specs,
  designs, or reference material. Attach your own artifacts (design
  previews, screenshots) to the task when submitting for review.
