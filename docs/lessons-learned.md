# Lessons Learned — Workflow & Self-Improvement

Rules and patterns collected from project experience. Referenced from [`CLAUDE.md`](../CLAUDE.md). Update this file after every correction or discovery.

---

## 1. Plan Mode for Verification

- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity
- Before implementing, define what "done" looks like — including acceptance criteria and verification steps

## 2. Subagent Strategy

- Use subagents liberally to keep the main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

## 3. Self-Improvement Loop

- After ANY correction from the user: update this file (`docs/lessons-learned.md`) with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for the relevant project

## 4. Verification Before Done

- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

## 5. Demand Elegance (Balanced)

- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

## 6. Autonomous Bug Fixing

- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update this file (`docs/lessons-learned.md`) after corrections

---

## Project-Specific Lessons

> Add entries here as you discover patterns specific to this codebase.

<!-- Example format:
### YYYY-MM-DD — Short title
**Mistake:** What went wrong
**Root cause:** Why it happened
**Rule:** The rule that prevents recurrence
-->
