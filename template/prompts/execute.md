# Stage: execute

You do the work the plan describes.

## Read first

- `plan.md`: your instructions. Follow its steps and meet every acceptance criterion.
- `brief.md` and any files beside it.
- `notes.md` if present: the owner's feedback. Act on it.
- `check.md` if present: the checker failed an earlier attempt. Fix every point it raised.

## Write

- `result.md`, starting with this front matter:

```
---
replan: false
---
```

  Then, as the first line after the front matter, one sentence that sums up the outcome.
  It goes into the owner's morning email, so make it say something ("Supplier B is cheapest
  over three years by about 12%", not "I compared the suppliers"). Then:
  - `## What I did`
  - `## Criteria`: each acceptance criterion from the plan, and where the output meets it.
  - `## Outputs`: the files in `out/`, one line each.
  - `## Loose ends`: anything unfinished, uncertain or worth a second look.
- `out/...`: any longer deliverable the plan lists.

If the plan can't work (a wrong assumption, missing input, a criterion that can't be met),
don't force it. Write `result.md` with `replan: true` in the front matter and explain what
is wrong. The item goes back to be re-planned.

Don't change `plan.md`. Write nothing outside `result.md` and `out/`.
