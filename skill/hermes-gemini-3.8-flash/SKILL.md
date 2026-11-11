---
name: workq-intake
description: Interview the user  about knowledge work he wants done later, write it up as a brief, and add it to the inbox of his workq-queue git repo. Use when he says "queue this", "new work item", "add to workq", or asks for research, analysis or writing to be done later rather than answered now.
---

# workq intake

You turn the user's request into a brief that another AI can plan and carry out overnight without asking him anything. You interview him, write ONE file, and push it to git. You do NOT do the work yourself.

## When to start

- Start when the user says "queue this", "new work item", "add to workq" or something with the same meaning.
- If he asks for research, analysis or writing and you can't tell whether he wants it now or later, ask: "Answer this now, or queue it for tonight's run?"

## Rules

1. Add exactly one new file: `inbox/YYYY-MM-DD-slug/brief.md`. Never change, move or delete any other file in the repo.
2. Never write or push anything until the user has approved the brief (Step 3).
3. No secrets in the brief: no passwords, API keys, tokens, or bank or card numbers. Git keeps them forever. If the user gives you one, leave it out and tell him you did.
4. Never use `git push --force`, and never change git history.
5. If a command fails in a way these steps don't cover, stop and show the user the error. Don't try other commands.

## Step 1: Interview

Your goal is to fill every section of the brief template below.

- Ask at most 3 questions per message.
- Use what the user has already told you. Ask only for what is missing.
- Two or three rounds is usually enough.

Make sure you know:

- **Ask:** the task or question, in one sentence.
- **Deliverable:** what should come back (an answer, report, table, comparison, draft or list), and how long.
- **Why:** what it is for: the decision or use it supports, and who will read it.
- **Good looks like:** 2 to 5 checks the user would use to judge the result. Each must be something you can verify by looking at the result ("names 3 options, each with a price in AUD and a source", "under one page"). Not qualities like "thorough" or "high quality".
- **Inputs and sources:** links, pasted text or data, sources to use or avoid, and whether web research is allowed.
- **Limits:** scope, format, country or currency, time period, what is out of scope, and a date it is needed by, if any.

If the user says "you decide" or doesn't know, choose something sensible, write it under Assumptions, and mark it "(assumed)".

## Step 2: Draft

Write the brief using the template below, exactly.

- Plain words, short sentences.
- The first line MUST be `# <Title>`: the queue uses it as the item's name in the morning email.
- Copy anything the user pasted word for word under `## Material`.
- Keep the brief under 20,000 characters. If their material is bigger, tell him to add it to the folder as a file himself after you push.

## Step 3: Approve

Show the user the whole draft and the folder name you plan to use. Ask: "Shall I queue this? Say yes, or tell me what to change." Change it and show it again until he says yes.

## Step 4: Slug

- 2 to 4 words, lowercase, joined with hyphens. Only `a-z`, `0-9` and `-`.
- Name the subject, not the action: `laptop-choice`, `home-battery-payback`, `japan-rail-pass`.
- At most 40 characters.

## Step 5: Sync, write, commit, push

The repo is `~/workq-queue`, branch `main`. Run each block in order.

**(a) Sync to the remote.** This throws away anything in the folder that isn't on the remote. That is intended.

```
cd ~/workq-queue
git fetch origin
git checkout -q -B main origin/main
git reset --hard origin/main
git clean -fdx
```

**(b) Choose the folder.** Put the slug from Step 4 in place of `<slug>`.

```
SLUG=<slug>
DATE=$(TZ=Australia/Perth date +%F)
git ls-files | grep -q "/$DATE-$SLUG/" && echo taken || echo free
```

If it prints `taken`, add `-2` to the slug (then `-3`, and so on) and run it again until it prints `free`. Then:

```
DIR="inbox/$DATE-$SLUG"
mkdir -p "$DIR"
```

**(c) Write the file.** Write the approved brief, unchanged, to `$DIR/brief.md`. Use your file-writing tool, or a heredoc with a QUOTED marker so `$` signs stay as typed:

```
cat > "$DIR/brief.md" <<'EOF'
<the approved brief>
EOF
```

**(d) Check.**

```
git add "$DIR/brief.md"
git status --porcelain
```

The output must be exactly one line: `A  inbox/<date>-<slug>/brief.md`. If you see anything else, stop and tell the user.

**(e) Commit and push.**

```
git commit -q -m "intake: $DATE-$SLUG"
git push origin HEAD:main
```

**(f) If the push is rejected** (someone else pushed first): go back to (a), then do (b) to (e) again with the same approved brief. Try at most 3 times, then stop and tell the user.

**(g) Confirm it is on the remote.**

```
git fetch origin
git ls-tree -r --name-only origin/main | grep "$DIR/brief.md"
```

It must print the path. If it prints nothing, tell the user it didn't reach the remote.

## Step 6: Report

Tell the user in one or two lines: the folder name, that it will be picked up at the next 22:00 run, and that the result will be in the 06:30 email after that.

## Brief template

```
# <Title: a short noun phrase, 3 to 8 words>

## Ask
<One or two sentences: what to do.>

## Deliverable
<What should come back, in what form, and how long.>

## Why
<What it is for: the decision or use it supports, and who will read it.>

## Good looks like
1. <A check you can verify by looking at the result>
2. <...>

## Inputs and sources
<Links, where to look, sources to avoid. Say whether web research is allowed.>

## Limits
<Scope, format, country, currency, time period, out of scope. Date needed by, if any.>

## Assumptions
<Anything decided without the user, each marked (assumed). Write "None" if none.>

## Material
<Pasted text or data, word for word. Write "None" if none.>
```

## Example

> **the user:** queue this: work out if a home battery is worth it for us
>
> **You:** Sure. Three questions first:
> 1. What should come back: a yes or no with reasons, or a full payback table?
> 2. What is your solar setup and power bill (system size, bill per quarter, feed-in rate)?
> 3. Any batteries you're already looking at, or should it pick?
>
> **the user:** payback table and a recommendation. 6.6 kW, about $450 a quarter, feed-in 2.25c. Look at the Powerwall 3 and something cheaper. Web is fine, WA rebates matter.
>
> **You:** Here's the brief. I'll put it in `inbox/2026-11-08-home-battery-payback/`. Shall I queue this? Say yes, or tell me what to change.

```
# Home battery payback for our house

## Ask
Work out whether adding a home battery to our solar system pays for itself, and recommend yes or no.

## Deliverable
A one-page note: the recommendation first, then a payback table for two batteries, then the main risks.

## Why
To decide whether to buy a battery this summer.

## Good looks like
1. Compares the Tesla Powerwall 3 and one cheaper battery, with installed prices in AUD and a source for each.
2. Shows simple payback in years for each battery, with the sum shown.
3. Includes any WA state rebate, with a source and its date.
4. Lists every assumption used in the sums.

## Inputs and sources
Web research allowed. Use current Australian prices and WA government pages for rebates.

## Limits
Western Australia, AUD, current prices. Under 600 words plus the table. Out of scope: changing electricity retailer.

## Assumptions
- Our power use stays as it is now. (assumed)

## Material
- Solar system: 6.6 kW
- Power bill: about $450 a quarter
- Feed-in tariff: 2.25c per kWh
```
