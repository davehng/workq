# workq

A git repo holds a queue of knowledge-work briefs. Each night a runner script on a Linux
VM has Claude shape each brief into a plan, carry it out on the cheapest model that fits,
and check the result. Each morning you get one email listing what is ready for review.
You approve, ask for rework, or reject by adding a small file to the item's folder.

## 

## What's in the repository

|Path|What it is|
|-|-|
|`bin/workq`|The runner (Python 3, needs PyYAML). Does all git work and every move.|
|`bin/agent-claude`|Starts Claude Code as the agent user with the agent's own token.|
|`template/`|The queue repo skeleton: folders, `CLAUDE.md` house rules, the three stage prompts.|
|`etc/`|Example config, agent credentials file, Claude Code managed settings, sudo rule.|
|`systemd/`|The service, the 22:00 night timer and the 06:30 digest timer.|
|`install.sh`|Sets the above up on an Ubuntu or Debian VM.|
|`tests/`|End-to-end tests with a fake `claude` and a local git repo.|

## Try it on your own machine first (about 10 minutes)

You need git, Python 3 with PyYAML (`pip install pyyaml` or `apt install python3-yaml`),
and Claude Code installed and signed in (`claude` works in a terminal). Nothing is
installed system-wide and no VM is needed. This runs Claude as you, with real models, so
each brief costs a little (the test brief below came to about US$0.42 estimated).

```bash
unzip workq.zip \&\& cd workq

# 1. The tests: no model calls, about a minute.
python3 -m unittest discover -s tests

# 2. A throwaway queue in a local folder.
mkdir -p \~/workq-trial \&\& cd \~/workq-trial
git init -q --bare -b main queue.git
cat > config.yaml <<EOF
repo\_url: $HOME/workq-trial/queue.git
state\_dir: $HOME/workq-trial/state
timezone: Australia/Perth
claude: {agent\_user: null, bin: claude}
digest: {to: null}
EOF
export WORKQ\_CONFIG=$HOME/workq-trial/config.yaml
WQ=\~/path/to/workq/bin/workq       # where you unzipped it

$WQ init
$WQ add "Summarise this week's notes" -m "Turn the notes below into five bullet points and one recommendation.
<paste some notes here>"

# 3. Run a shift now, ending in two hours (it stops early when nothing is left).
$WQ night --until $(date -d '+2 hours' +%H:%M)     # macOS: date -v+2H +%H:%M

# 4. Look at the result, as you would in the web UI.
git clone -q queue.git view \&\& ls view/review/\*/
$WQ digest --stdout
```

To decide on it, add `review.md` to the item folder in your clone, push, then run the file
stage (the night shift starts with it):

```bash
cd view
for d in review/\*/; do printf -- "---\\nverdict: approve\\n---\\nLooks good.\\n" > "$d/review.md"; done
git add -A \&\& git commit -qm review \&\& git push -q \&\& cd ..
$WQ run file \&\& $WQ status
```

Try `rework` with notes to watch it go round again, or put a vague brief in to see the
shaper ask questions (it lands in `blocked/`; answer with `verdict: retry` and your notes).

## Set up on a VM (home, GitHub)

1. **VM.** Ubuntu 22.04 or 24.04, small is fine (1 vCPU, 2 GB RAM). Set its clock:
`sudo timedatectl set-timezone Australia/Perth`.
2. **Repo.** Create an empty private GitHub repo (no README), for example `workq-queue`.
3. **Install.** Copy the zip over, unzip, and run `sudo ./install.sh`. It creates two users
(`workq` runs the queue and holds the git key; `workq-agent` runs Claude and holds the
Claude token), puts the program in `/opt/workq`, and prints a deploy key.
4. **Deploy key.** In the GitHub repo: Settings → Deploy keys → Add, paste the key, tick
"Allow write access".
5. **Claude Code for the agent user.**
`sudo -u workq-agent -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'`
Then on your own computer run `claude setup-token`, and paste the token into
`/etc/workq/agent.env` as `CLAUDE\_CODE\_OAUTH\_TOKEN=...` (`sudo nano /etc/workq/agent.env`).
An Anthropic API key in `ANTHROPIC\_API\_KEY` works too.
6. **Config.** `sudo nano /etc/workq/config.yaml`: set `repo\_url`, `web\_url`, `digest.to`,
`digest.from`, and look over the caps. Every setting is explained in the file.
7. **Mail.** The digest is sent with `msmtp`. Create `/home/workq/.msmtprc` (owner `workq`,
mode 600). For Gmail, make an app password first:

```
   defaults
   auth on
   tls on
   tls\_trust\_file /etc/ssl/certs/ca-certificates.crt
   account default
   host smtp.gmail.com
   port 587
   from you@gmail.com
   user you@gmail.com
   password your-app-password
   ```

8. **Fill the repo.** `sudo -u workq workq init`
9. **Run it by hand once.** Add a brief (`sudo -u workq workq add "Title" -m "..."`, or in
the GitHub web editor), then:

```bash
   sudo systemctl start workq@night        # a full shift; ends when the queue is empty
   journalctl -u workq@night -e            # what it did
   sudo systemctl start workq@digest       # the email
   ```

10. **Turn on the timers.**
`sudo systemctl enable --now workq-night.timer workq-digest.timer`

## Work instance (Azure DevOps)

Same steps on a separate VM, with these changes:

* `repo\_url: git@ssh.dev.azure.com:v3/ORG/PROJECT/workq-queue`. Add the printed key under
User settings → SSH public keys, on a service account if IT provides one.
* `web\_url: https://dev.azure.com/ORG/PROJECT/\_git/workq-queue?version=GBmain\&path=`
* If branch policy forces pull requests on `main`, set `branch: queue` and create that
branch first.
* Claude: Perenti's Team seat through `claude setup-token`, once IT confirms unattended use
is allowed. Runs share that seat's five-hour and weekly allowance with your own use, so
set the opus `runs` cap low to start.
* Mail: Microsoft 365 is switching off password sign-in for SMTP, so ask IT which route to
use (OAuth, a Graph app or a relay) and set `digest.send\_command` to match. Any command
that reads a whole message on stdin works.

## Daily use

|You want to|Do this|
|-|-|
|Ask for work|Add `inbox/<name>/brief.md` (or `inbox/<name>.md`) in the web UI, or `workq add "Title" -m "..."`|
|Approve|Add `review.md` with `verdict: approve` to the item in `review/`|
|Ask for changes|`verdict: rework` plus your notes|
|Answer a blocked item|Edit `brief.md` if needed, then `review.md` with `verdict: retry` and notes|
|Drop it|`verdict: reject` (archived, marked rejected)|
|Stop everything|Add a `PAUSE` file at the repo root (or `workq pause`)|

Verdicts are acted on at the start of the next shift. The queue repo's own `README.md`
says the same for anyone who opens it.

## Commands

|Command|Does|
|-|-|
|`workq init`|Fill an empty remote repo with the queue skeleton|
|`workq add TITLE \[-m TEXT] \[-f FILE]`|Add a brief to `inbox/`|
|`workq run STAGE`|One run of `file`, `shape`, `exec-haiku`, `exec-sonnet`, `exec-opus` or `check`|
|`workq night \[--until HH:MM]`|The overnight shift: all stages in turn until nothing moves, a cap, or the end time|
|`workq digest \[--stdout]`|Send (or print) the morning email|
|`workq status`|Items per folder and tonight's runs against the caps|
|`workq pause` / `workq resume`|Add or remove `PAUSE`|

On the VM, run them as the `workq` user: `sudo -u workq workq status`.

## How it behaves

* **Attempts** are counted per stage (`shape\_attempts`, `execute\_attempts`,
`check\_attempts` in `meta.yaml`). Three failures at one stage blocks the item. Your
`rework` or `retry` resets them.
* **Failed checks**: first fail retries at the same tier; second steps up a tier; a failed
Opus result, or a third execute attempt, blocks the item.
* **Guard**: after each run, any file written outside the stage's own files (plan, result
and `out/`, or check) is thrown away and the run counts as failed.
* **Caps**: nightly runs and estimated dollars per tier. Shape and check count as opus.
* **Usage limits**: the attempt is given back and the shift sleeps until the limit resets,
if that is before 06:00.
* **Broken setup** (Claude won't start, or can't sign in): the attempt is given back and
the shift stops, so a bad token doesn't push every item into `blocked/`.
* **Logs**: each item's `log/` holds the raw output of every run. On the VM,
`/var/lib/workq/ledger.jsonl` has one line per run and `problems.jsonl` feeds the
digest's Problems section.

## What was tested, and what wasn't

Tested:

* 26 end-to-end tests with a fake `claude` against a local git repo: the full path to
archive, retries and tier step-up, blocking, questions and retry, replan, the guard
(writes outside the item, edits to `meta.yaml`, stray files, planted briefs), output
size limit, timeouts, usage limits, broken setup, pause, caps, the shift's end time,
rework and reject, a bad verdict, an owner push during a run, and the digest.
* `install.sh` on Ubuntu 24.04, then a shift run as the `workq` user with Claude started as
`workq-agent` through sudo. Each user was refused the other's credentials, and the
sudo rule allowed only the wrapper.
* One real brief through every stage with real models (Opus shape, Sonnet execute, Opus
check), then approval to the archive and the digest.
* The systemd units pass `systemd-analyze verify`.

Not tested: GitHub or Azure DevOps over SSH (tests used a local repo), real email sending,
the timers firing under a running systemd, a real usage limit (the wording Claude Code
uses was simulated), and real models running as the separate agent user.

