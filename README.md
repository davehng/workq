# workq 0.2.2

A git repo holds a queue of knowledge-work briefs. Each night a runner script on a Linux
machine has Claude shape each brief into a plan, carry it out on the cheapest model that
fits, and check the result. Each morning a digest lists what is ready for review: by email,
or as a file in the repo when the machine can't send mail.
You approve, ask for rework, or reject by adding a small file to the item's folder.

Design doc: https://claude.ai/artifact/UErMHjrscWhxJkd1ekApyA

## Two ways to install

| | No root, one user (`install-user.sh`) | Root, two users (`install.sh`) |
| --- | --- | --- |
| Built for | RHEL 9 at work, or any Linux where you have no root | A VM you control (the home setup) |
| Needs root | No. Everything lives in your home folder | Yes, once, to install |
| Who runs Claude | You, the same user that runs the queue | A second user, `workq-agent`, through sudo |
| What keeps agents from your keys | A bubblewrap sandbox when the machine has one, plus Claude Code's restricted mode and deny rules | Linux file permissions between the two users (the agent user can reach only the clone it is working in, and only during its run), plus restricted mode |
| Schedule | `workq schedule on`: systemd user timers, or cron | systemd system timers |
| Config | `~/.config/workq/config.yaml` | `/etc/workq/config.yaml` |

The runner, prompts and queue repo are the same in both.

## What changed in 0.2.2

**Email is optional.** A machine with no mail program and no SMTP relay can run workq.

- Leave `digest.to` empty (`to: null`) and no mail is sent. The digest is written to
  `DIGEST.md` at the top of the queue repo instead, and pushed like any other change. Read
  it in the repo's web page; its links go to the item folders.
- `digest.repo_file` sets this: `auto` (the default) writes the file only when there is no
  email; a file name, or `true`, writes it as well as the email; `off` never writes it.
- `workq doctor` no longer warns about an empty `digest.to`, and never looks for a mail
  program when there is no address.
- Without email, problems used to be repeated in every digest. Now each is reported once,
  as with email.
- The no-root example config now starts with email off.

An install that already sends email is not changed by the upgrade.

**One fix, in every version before this.** If Claude Code failed the moment it started (a
wrong option, a refused `sudo`), the runner could be killed while handing over the prompt,
with no message. The run then counted as one that "ended without finishing". Now the
runner reports the failure and stops the shift, as it was meant to. `workq night | head`
no longer stops the shift either: the shift carries on without printing.

## What changed in 0.2.1

Fixes from a review of 0.2.0. Upgrade any 0.2.0 install; the first fix matters most.

- **A runner stopped part-way no longer breaks its stage.** In 0.2.0, stopping the runner
  while an agent worked (`systemctl stop`, a timeout, a reboot) left that stage's clone
  read-only, and every later run of the stage failed. Now the agent is stopped with the
  runner, the clone is unlocked on the way out, and every run repairs a clone left locked.
- **Two-user mode: the agent user can no longer write the runner's files.** The state
  folder was group-writable, so an agent able to run commands could have run programs as
  the runner user. Now the ledger, locks and other clones are private to the runner. The
  one clone an agent works in is opened to it for the length of that run and shut after.
- **Replans are counted.** An executor that keeps asking for a new plan is blocked after
  three plans, instead of looping every night.
- **Symbolic links are not allowed in the queue.** The runner deletes any it finds, never
  reads or writes through one, and fails a run that creates one.
- **The mail password is never sent unencrypted.** With `starttls` and `ssl` both off, a
  sign-in is refused unless the server is on the same machine.
- **A run that ends in an error is not used**, even if it left its output file, and the
  digest says why (for example "Reached maximum number of turns").
- **An item whose runs keep dying is blocked** after three ("runs ended without finishing").
- **One bad item can't stop a shift.** An unexpected error sets that item aside until the
  next shift and is reported in the digest. `workq night` exits 1 if Claude could not run.
- **The sandbox has its own process list**, so nothing outside it can be reached through `/proc`.
- Smaller: helper commands wait their turn for the shared clone; blank values under `env:`
  are fine; YAML aliases in agent-written files are refused; `plan.md`, `result.md` and
  `check.md` have a size cap (`max_text_mb`); a token in `repo_url` is never printed and
  `doctor` flags it; `schedule off` keeps your crontab lines if its end marker is missing;
  the `.git` check covers `HEAD`, the index and every ref; Claude runs in the config's time zone.

## What changed in 0.2

- **No-root install** for a single user, with `workq schedule` and `workq doctor`.
- **RHEL 9**: runs on its stock Python 3.9. PyYAML is bundled, so no Python packages are
  needed. The root installer knows `dnf` as well as `apt`.
- **RSA git keys.** Azure DevOps accepts only RSA SSH keys, so both installers now make an
  RSA 4096 key. (0.1 made an ed25519 key, which Azure DevOps refuses. An existing key is kept.)
- **Built-in SMTP** for the digest, since `msmtp` is not in the RHEL repos. A failed send now
  prints the reason.
- **HTTPS with a token** as an alternative to SSH, for networks that block port 22.
- **Sandbox** (bubblewrap) for single-user mode, and a stripped environment for Claude.
- **`.git` is protected** in both modes: locked while an agent runs, hooks are never run,
  and a clone whose `.git` changed is thrown away and the shift stopped.
- **More shifts per day** from one config line (`extra_shifts`), each with its own digest.
- Clock times in the config work with or without quotes.

## What's in the zip

| Path | What it is |
| --- | --- |
| `bin/workq` | The runner (Python 3.9+). Does all git work and every move. |
| `bin/agent-claude` | Two-user mode only: starts Claude as the agent user. |
| `lib/yaml/` | PyYAML 6.0.3 (MIT licence), used when the machine has none. |
| `template/` | The queue repo skeleton: folders, `CLAUDE.md` house rules, the three stage prompts. |
| `etc/` | Example configs for both modes, credential file examples, deny rules, sudo rule. |
| `systemd/` | System units for the root install. (The no-root install writes its own.) |
| `install-user.sh` | No-root install into your home folder. |
| `install.sh` | Root install with two users, for apt or dnf systems. |
| `tests/` | End-to-end tests with a fake `claude` and a local git repo. |

## Install without root (RHEL 9)

### What must already be on the machine

| Needed | On RHEL 9 | If missing |
| --- | --- | --- |
| Python 3.9 or later | Installed by default | Admin: `dnf install python3` |
| git 2.28 or later | Often installed; not on minimal builds | Admin: `dnf install git` |
| A way to reach the repo | SSH out on port 22, or HTTPS | Use HTTPS with a token (below) |
| A way to run on a schedule | cron (usually there), or systemd user timers with lingering | Admin: `loginctl enable-linger YOU`, or cron access |
| Claude Code | Installs into your home, no root | See step 2 |
| Optional: bubblewrap | Sometimes installed | Admin: `dnf install bubblewrap` |

Nothing else is needed: no Python packages, no mail program, no sudo.

### Steps

1. **Install.** Copy the zip to the machine, then:

   ```bash
   unzip workq.zip && cd workq
   ./install-user.sh
   ```

   It copies the program to `~/.local/share/workq`, links `~/.local/bin/workq`, writes
   starter config in `~/.config/workq` (private to you), and makes a git key at
   `~/.ssh/workq_key`. Your own SSH keys and git settings are left alone. It prints the
   public key.

2. **Claude Code.** If it isn't installed:
   `curl -fsSL https://claude.ai/install.sh | bash` (installs to `~/.local/bin`).
   Behind a proxy, set `HTTPS_PROXY` first.

3. **Repo access.** Create an empty repo for the queue, then either:
   - **SSH:** add the printed public key. Azure DevOps: User settings → SSH public keys.
     GitHub: repo Settings → Deploy keys, with write access.
   - **HTTPS with a token** (when port 22 is blocked): make a personal access token with
     Code read and write, then

     ```bash
     echo 'https://anything:YOUR_TOKEN@dev.azure.com' > ~/.config/workq/git-credentials
     chmod 600 ~/.config/workq/git-credentials
     ```

     and in the config set `repo_url: https://dev.azure.com/ORG/PROJECT/_git/workq-queue`
     and `git.credential_file: ~/.config/workq/git-credentials`. Tokens expire: note the date.

4. **Claude token.** On any machine with a browser run `claude setup-token`, then put the
   token in `~/.config/workq/claude.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`. The runner gives
   it to the Claude process only.

5. **Config.** Edit `~/.config/workq/config.yaml`: `repo_url`, `web_url` and the caps.
   Email is off to start with; see "The digest without root" below. Every setting is explained in the file. For a company proxy or
   certificate bundle, use the `env:` section.

6. **Check.** `workq doctor --live` tests each part and makes one tiny real call to Claude.
   Fix every `FAIL` line; each says what to do. `WARN` lines are choices, not faults.

7. **Fill the repo.** `workq init` (skip if the queue repo is already set up).

8. **Run one shift by hand.** `workq add "Title" -m "..."`, then `workq night`. It ends when
   the queue is empty. Then `workq digest`, and look for `DIGEST.md` in the repo (or your inbox).

9. **Turn on the schedule.** `workq schedule on`, and `workq schedule show` to see it.

### Scheduling without root

`workq schedule on` reads the times from the config (`window.start`, `digest.at`,
`extra_shifts`, all in the config's `timezone`) and picks a route:

- **systemd user timers**, if your account has lingering on. Without lingering, user timers
  stop when you log out, so workq doesn't choose them. It tries `loginctl enable-linger`
  for you; on most servers that needs an admin, once: `loginctl enable-linger YOU`.
  Logs: `journalctl --user -u 'workq-*'`.
- **cron**, otherwise. It adds a marked block at the end of your crontab and leaves your
  other lines alone. Logs: `~/.local/state/workq/logs/cron.log`. If the machine's clock is
  in another zone the block starts with `CRON_TZ=`, which also applies to any lines you
  add below the block, so add yours above it.

Run `workq schedule on` again after changing times in the config. `workq schedule off`
removes it. Force a route with `--scheduler systemd` or `--scheduler cron`.

A midday shift is one config line, then `workq schedule on`:

```yaml
extra_shifts:
  - {start: "12:00", until: "15:00", digest: true}
```

Caps are counted from one 22:00 to the next, so every shift in that day shares them.

### The digest without root

With `digest.to` empty, which is how the config starts, no mail is sent and none of the
mail settings matter. Each digest run writes `DIGEST.md` at the top of the queue repo and
pushes it. Bookmark that file in the repo's web page. The file is replaced each time; git
history keeps the old ones. Agents can't change it: the runner throws away anything an
agent writes outside its own files.

To get email as well, or instead, set `digest.to` to your address and pick a route.
Set `digest.smtp.host` and the digest is sent by the runner itself; no mail program is
needed. For a server that needs sign-in, set `user` and put the password alone in a
private file named by `password_file`. A sign-in is only made over an encrypted
connection (`starttls` or `ssl`). Otherwise `digest.send_command` is used
(`/usr/sbin/sendmail -t` by default on this install). Microsoft 365 is turning off
password sign-in for SMTP, so at work ask IT which route to use: an internal relay that
needs no sign-in is the simplest fit for `smtp.host`.

### What protects your keys in single-user mode

Claude runs as you, so file permissions can't separate it from your git key and tokens.
These do, in order of strength:

1. **Sandbox**, when bubblewrap is installed and allowed (`claude.sandbox: auto`). Each run
   sees the whole system read-only, and an empty home folder, `/tmp` and workq config. Only
   the clone it is working in is writable, and that clone's `.git` is read-only. It has its
   own process list, so it can't see or reach anything running outside.
   `workq doctor` says whether it is working. Set `sandbox: bwrap` to refuse to run without it.
2. **Restricted mode** (`--restricted`), always on: Claude's file tools can't leave the
   clone, and tools that run commands are removed unless `bash_allow` grants one.
3. **Deny rules** in `~/.config/workq/claude-settings.json`: named secret paths are refused.
4. **A stripped environment**: Claude gets `HOME`, `PATH`, language and proxy settings, its
   own token, and nothing else from your shell.
5. **The guard**: anything written outside the stage's own files is thrown away.

Without the sandbox, keep `bash_allow` empty. A command an agent runs is a program running
as you, and layers 2 and 3 don't bind it.

### Upgrade or remove

Upgrade: unzip the new version and run `./install-user.sh` again. Config, keys and state
are kept. Remove: `./install-user.sh --uninstall` takes away the program and the schedule
and keeps your config, state and key.

## Install with root (two users)

For a VM you control. Ubuntu, Debian, or RHEL 9 and its rebuilds.

1. **VM.** Small is fine (1 vCPU, 2 GB RAM). Set its clock:
   `sudo timedatectl set-timezone Australia/Perth`. The system timers use the machine's clock.
2. **Repo.** Create an empty private repo (no README), for example `workq-queue`.
3. **Install.** Copy the zip over, unzip, and run `sudo ./install.sh`. It creates two users
   (`workq` runs the queue and holds the git key; `workq-agent` runs Claude and holds the
   Claude token), puts the program in `/opt/workq`, and prints a public key.
4. **Key.** GitHub: repo Settings → Deploy keys → Add, with write access. Azure DevOps:
   User settings → SSH public keys.
5. **Claude Code for the agent user.**
   `sudo -u workq-agent -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'`
   Then on your own computer run `claude setup-token`, and paste the token into
   `/etc/workq/agent.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`.
6. **Config.** `sudo nano /etc/workq/config.yaml`: set `repo_url`, `web_url`, the `digest`
   section, and look over the caps.
7. **Mail.** Optional: with `digest.to: null` the digest is written to `DIGEST.md` in the
   queue repo and this step can be skipped. For email, either set `digest.smtp` in the
   config, or use `msmtp` (apt systems): create
   `/home/workq/.msmtprc`, then
   `sudo chown workq:workq /home/workq/.msmtprc && sudo chmod 600 /home/workq/.msmtprc`.
   For Gmail, make an app password first:

   ```
   defaults
   auth on
   tls on
   tls_trust_file /etc/ssl/certs/ca-certificates.crt
   account default
   host smtp.gmail.com
   port 587
   from you@gmail.com
   user you@gmail.com
   password your-app-password
   ```
8. **Check and fill.** `sudo -u workq workq doctor`, then `sudo -u workq workq init`.
9. **Run it by hand once.** Add a brief (`sudo -u workq workq add "Title" -m "..."`), then:

   ```bash
   sudo systemctl start workq@night        # a full shift; ends when the queue is empty
   journalctl -u workq@night -e            # what it did
   sudo systemctl start workq@digest       # the digest
   ```
10. **Turn on the timers.**
    `sudo systemctl enable --now workq-night.timer workq-digest.timer`

**Upgrading a 0.1 or 0.2.0 install:** unzip this version and run `sudo ./install.sh`
again. Config, credentials, keys and timers are kept, and nothing in the queue repo
changes. The first run tightens the permissions on `/var/lib/workq`.

## Try it on your own machine first (about 10 minutes)

You need git, Python 3.9 or later, and Claude Code installed and signed in. Nothing is
installed and no schedule is set. This runs Claude as you, with real models, so each brief
costs a little (the test briefs came to about US$0.40 each, estimated).

```bash
unzip workq.zip && cd workq

# 1. The tests: no model calls, a minute or two.
python3 -m unittest discover -s tests

# 2. A throwaway queue in a local folder.
mkdir -p ~/workq-trial && cd ~/workq-trial
git init -q --bare -b main queue.git
cat > config.yaml <<EOF
repo_url: $HOME/workq-trial/queue.git
state_dir: $HOME/workq-trial/state
timezone: Australia/Perth
claude: {agent_user: null, bin: claude}
digest: {to: null}
EOF
export WORKQ_CONFIG=$HOME/workq-trial/config.yaml
WQ=~/path/to/workq/bin/workq       # where you unzipped it

$WQ doctor --live
$WQ init
$WQ add "Summarise this week's notes" -m "Turn the notes below into five bullet points and one recommendation.
<paste some notes here>"

# 3. Run a shift now, ending in two hours (it stops early when nothing is left).
$WQ night --until $(date -d '+2 hours' +%H:%M)     # macOS: date -v+2H +%H:%M

# 4. Look at the result, as you would in the web UI.
git clone -q queue.git view && ls view/review/*/
$WQ digest --stdout
```

To decide on it, add `review.md` to the item folder in your clone, push, then run the file
stage (each shift starts with it):

```bash
cd view
for d in review/*/; do printf 'verdict: approve\nLooks good.\n' > "$d/review.md"; done
git add -A && git commit -qm review && git push -q && cd ..
$WQ run file && $WQ status
```

## Daily use

| You want to | Do this |
| --- | --- |
| Ask for work | Add `inbox/<name>/brief.md` (or `inbox/<name>.md`) in the web UI, or `workq add "Title" -m "..."` |
| See what is ready | Read the digest email, or `DIGEST.md` at the top of the repo when email is off |
| Approve | Add `review.md` to the item in `review/`, containing the line `verdict: approve` |
| Ask for changes | `verdict: rework`, with your notes on the lines below |
| Answer a blocked item | Edit `brief.md` if needed, then `review.md` with `verdict: retry` and notes |
| Drop it | `verdict: reject` (archived, marked rejected) |
| Stop everything | Add a `PAUSE` file at the repo root (or `workq pause`) |

`review.md` needs the `verdict:` prefix; a bare `approve` is reported as a bad verdict.
Verdicts are acted on at the start of the next shift.

## Commands

| Command | Does |
| --- | --- |
| `workq doctor [--live]` | Check the setup; `--live` adds one tiny real call to Claude |
| `workq init` | Fill an empty remote repo with the queue skeleton |
| `workq add TITLE [-m TEXT] [-f FILE]` | Add a brief to `inbox/` |
| `workq run STAGE` | One run of `file`, `shape`, `exec-haiku`, `exec-sonnet`, `exec-opus` or `check` |
| `workq night [--until HH:MM]` | A shift: all stages in turn until nothing moves, a cap, or the end time |
| `workq digest [--stdout]` | Build the digest: email it, write `DIGEST.md` in the repo, or both. `--stdout` only prints it |
| `workq status` | Items per folder and runs against the caps |
| `workq schedule show\|on\|off` | No-root install: set up, show or remove the timers |
| `workq pause` / `workq resume` | Add or remove `PAUSE` |

The config is found at `$WORKQ_CONFIG`, then `~/.config/workq/config.yaml`, then
`/etc/workq/config.yaml`. On a root install, run commands as the `workq` user:
`sudo -u workq workq status`.

## How it behaves

- **Attempts** are counted per stage (`shape_attempts`, `execute_attempts`,
  `check_attempts` in `meta.yaml`). Three failures at one stage blocks the item. Your
  `rework` or `retry` resets them.
- **Failed checks**: first fail retries at the same tier; second steps up a tier; a failed
  Opus result, or a third execute attempt, blocks the item.
- **Guard**: after each run, any file written outside the stage's own files (plan, result
  and `out/`, or check) is thrown away and the run counts as failed.
- **`.git`**: read-only while an agent runs. If it has changed afterwards, the clone is
  thrown away, the shift stops and the digest says so.
- **Symbolic links**: deleted wherever they appear in the queue repo. A run that makes
  one fails.
- **Runs that end in an error** (turn limit, budget, API fault) count as failed attempts,
  whatever files they left.
- **Replans**: an executor may send an item back for a new plan until it has had three
  plans; then the item is blocked.
- **Runs that never finish**: if the runner is killed or the machine goes down mid-run,
  the agent is stopped too and the next run of that stage repairs the clone. An item this
  happens to three times is blocked.
- **Unexpected errors**: the item is set aside until the next shift and the digest reports
  it. The rest of the shift goes on.
- **Caps**: runs and estimated dollars per tier, from one 22:00 to the next. Shape and
  check count as opus.
- **Usage limits**: the attempt is given back and the shift sleeps until the limit resets,
  if that is before the shift ends.
- **Broken setup** (Claude won't start, or can't sign in): the attempt is given back and
  the shift stops with exit code 1, so a bad token doesn't push every item into `blocked/`.
- **Logs**: each item's `log/` holds the raw output of every run. In the state folder,
  `ledger.jsonl` has one line per run and `problems.jsonl` feeds the digest's Problems section.

## If something goes wrong

Run `workq doctor` first. Common cases on a locked-down machine:

| Sign | Cause and fix |
| --- | --- |
| `cannot reach the queue repo`, connection timed out | Outbound SSH is blocked. Use HTTPS with a token |
| Azure DevOps refuses the key | It accepts only RSA keys. `~/.ssh/workq_key` from this installer is RSA; a key from elsewhere may not be |
| Claude fails with a certificate error | Company TLS inspection. Add to `env:` `NODE_EXTRA_CA_CERTS: /etc/pki/tls/certs/ca-bundle.crt` |
| Claude can't connect | Set `HTTPS_PROXY` and `NO_PROXY` in `env:` |
| `Claude Code does not start inside bubblewrap` | It needs a folder the sandbox hides. Add it to `claude.sandbox_binds`, or set `sandbox: off` |
| Nothing ran overnight, systemd timers | Lingering is off: `workq schedule show` says so |
| Nothing ran overnight, cron | Check `crontab -l` and `~/.local/state/workq/logs/cron.log`; the cron service may be stopped |
| `Permission denied` running `workq` itself | The home folder is mounted `noexec`. This install can't work there; ask for a folder that allows programs |
| Digest not sent, or `DIGEST.md` not written | The reason is printed and is in the next digest's Problems section |
| No digest anywhere | `digest.to` is empty and `digest.repo_file` is `off`: `workq doctor` says so |

## What was tested, and what wasn't

Tested:

- 86 end-to-end tests with a fake `claude` against a local git repo. They pass on Python
  3.13 with system PyYAML and on Python 3.9 with the bundled PyYAML, as root and as an
  unprivileged user. They cover the queue stages, the guard, caps, usage limits, the
  stripped environment, the sandbox (home, `/tmp`, config and state hidden; `.git` and the
  system read-only), `.git` locking and swap detection, hooks never run, the token file for
  HTTPS, built-in SMTP against a local test server, cron and systemd scheduling against
  stand-in `crontab` and `systemctl`, and `doctor`.
- New in 0.2.1, 20 of those tests cover the fixes: the runner stopped with TERM and with
  KILL in the middle of a run, an item whose runs never finish, two-user folder modes
  (with a stand-in `sudo`), tightening a state folder left by an older version, the replan
  loop, links made by an agent and links pushed to the repo, the mail password refusal,
  the lock on `init`, `add`, `pause` and `status`, blank and numeric `env:` values, the
  time zone handed to Claude, an unexpected error in one item, YAML aliases and a broken
  `meta.yaml`, a run that ends in an error after writing its file, the size cap, tokens in
  `repo_url`, a crontab block with its end marker missing, bad reset times, and a changed
  git ref.
- New in 0.2.2, 16 tests cover the digest without email: the file is written and pushed
  with no mail program run, problems are reported once, links with and without `web_url`,
  email alone, email and file together, neither, `--stdout`, bad file names, text from
  briefs that tries to add links or HTML, `doctor`, a shift after the file exists, an
  agent that edits the file, a refused push, and a folder or a link in the file's place.
  Three more cover the fix: a Claude that exits before reading its prompt, `status` into a
  closed pipe, and a shift whose output nobody reads.
- `install.sh` on Ubuntu 24.04 with the real `workq` and `workq-agent` users: three briefs
  through a shift; the agent user tried to list the state folder, write the ledger, make
  files, edit git config in its own and another clone, and read a clone at rest, and each
  was refused; a state folder with the old open modes was tightened on the next run; the
  runner was stopped mid-run and the agent went with it.
- `install-user.sh` from a fresh unzip as a new user with no sudo rights, on Python 3.9
  with no PyYAML: install, `doctor`, a full shift with the sandbox on, `schedule on`
  (cron), `schedule off` and uninstall.
- One real brief through every stage with real models (Opus, Sonnet, Opus) in single-user
  mode with `sandbox: bwrap`, restricted mode and the deny rules. The runner was stopped
  while real Claude was planning: Claude stopped with it, `.git` was unlocked, and the next
  shift picked the brief up and finished it.
- The generated systemd user units pass `systemd-analyze verify`.

Not tested:

- **RHEL 9 itself.** No RHEL image could be fetched in the build environment. Python 3.9
  was tested; RHEL's own git, cron (`CRON_TZ`) and SELinux were not. The sandbox was
  tested with bubblewrap 0.9, not RHEL 9's 0.6. Every flag workq passes exists in 0.6, but
  run `workq doctor` on the RHEL box before you trust it.
- systemd user timers firing, and lingering, under a real login session.
- The `dnf` path of `install.sh`.
- GitHub or Azure DevOps over SSH or HTTPS (tests used a local repo; the token file was
  tested with git's own credential lookup).
- SMTP with TLS or sign-in, and Microsoft 365.
- Real models as an unprivileged user inside the sandbox (the real run was as root; the
  unprivileged runs used the fake `claude`).
- A real usage limit (the wording Claude Code uses was simulated).
