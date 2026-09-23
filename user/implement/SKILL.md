---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams.

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

Once done, use /code-review to review the work.

Before starting: update main to the latest (`git fetch origin` + fast-forward main), create a new worktree with a new branch off main, and do all the work in that worktree. Commit your work to that branch.
