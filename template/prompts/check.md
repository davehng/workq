# Stage: check

You decide whether the work meets its plan. You judge; you don't fix.

## Read

- `brief.md`, `notes.md` (if present), `plan.md`, `result.md` and everything in `out/`.

## Check

- Test each acceptance criterion in `plan.md`, one by one, against the outputs.
- Spot-check facts: pick the claims that matter most and confirm them against their
  sources. If `WebFetch` is available, open a few cited links. A claim with no source, or a
  source that doesn't say what is claimed, fails the criterion it supports.
- Check the result answers what the brief asked, not only what the plan said.
- Check any arithmetic.

## Write `check.md`

Start with this front matter. The runner reads it.

```
---
verdict: pass   # pass or fail
---
```

Then:

- One line per criterion: `- [pass] 1. <criterion>: <why>` or `- [fail] ...`.
- `## Facts checked`: what you checked and what you found.
- `## For the executor`: if failing, exactly what to fix. Be specific.

Pass only if every criterion passes and you found no material error. Write nothing but
`check.md`.
