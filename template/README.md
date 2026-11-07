# Work queue

Agents work through this repo every night. You only touch two kinds of file.

## Ask for work

Create `inbox/<anything>/brief.md` (or just `inbox/<anything>.md`) in the web editor or with
`workq add "title"`. Say what you want, what good looks like, and any limits. Put files the
work needs in the same folder.

The runner renames the folder to `YYYY-MM-DD-title` when the night starts.

## Decide on finished work

The morning email lists items in `review/` and `blocked/`. Open the item folder, read
`result.md` and `out/`, then add `review.md`:

```
---
verdict: approve
---
Notes for the next round, if any.
```

| Folder | Verdicts |
| --- | --- |
| `review/` | `approve` (archive it), `rework` (re-plan with your notes), `reject` (archive as rejected) |
| `blocked/` | `retry` (re-plan with your notes; answer any `questions.md` here), `reject` |

Verdicts are acted on at the start of the next night's shift.

## Stop everything

Add a file called `PAUSE` at the top of the repo. Delete it to start again.
