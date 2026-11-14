#!/usr/bin/env python3
"""End-to-end tests for workq, using a fake `claude` and a local bare git repo.

Run from the package folder:  python3 -m unittest discover -s tests -v
Needs: git and Python 3.9 or later. Takes a minute or two. Calls no model, sends no
mail, and never touches your real crontab or systemd (fakes stand in for both).
Sandbox tests are skipped where bubblewrap isn't available.
"""
import datetime as dt
import importlib.machinery
import importlib.util
import fcntl
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
WORKQ = PKG / "bin" / "workq"
FAKE = HERE / "fake_claude.py"

try:
    if os.environ.get("WORKQ_BUNDLED_YAML"):
        raise ImportError
    import yaml
except ImportError:
    sys.path.insert(0, str(PKG / "lib" / "vendor"))
    import yaml


def load_runner():
    """Import bin/workq as a module, for the few tests that call its functions."""
    loader = importlib.machinery.SourceFileLoader("workq_runner", str(WORKQ))
    spec = importlib.util.spec_from_loader("workq_runner", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def bwrap_ok():
    exe = shutil.which("bwrap")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Env:
    def __init__(self, **overrides):
        self.root = Path(tempfile.mkdtemp(prefix="workq-test-"))
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.state = self.root / "state"
        self.calls = self.root / "claude-calls.jsonl"
        self.mail = self.root / "mail.txt"
        fake_state = self.root / "fake-state"
        fake_state.mkdir()
        sendmail = self.root / "sendmail.sh"
        sendmail.write_text(f"#!/bin/sh\ncat > {self.mail}\n")
        sendmail.chmod(0o755)
        cfg = {
            "repo_url": str(self.remote),
            "state_dir": str(self.state),
            "web_url": "https://example.test/q/tree/main",
            "limit_wait_min": 0.05,
            "claude": {"bin": str(FAKE), "agent_user": None, "restricted": True, "sandbox": "off",
                       "sandbox_binds": [str(PKG)],
                       "pass_env": ["FAKE_CLAUDE_LOG", "FAKE_CLAUDE_STATE", "FAKE_REMOTE",
                                    "FAKE_HELP_FLAGS", "WORKQ_BUNDLED_YAML"]},
            "digest": {"to": "me@example.test", "from": "workq@example.test",
                       "allowed_domains": ["example.test"], "send_command": str(sendmail)},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        self.config = self.root / "config.yaml"
        self.config.write_text(yaml.safe_dump(cfg))
        # A private home and tool folder, so nothing here reads or changes the real ones.
        self.home = self.root / "home"
        (self.home / ".ssh").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, WORKQ_CONFIG=str(self.config), FAKE_CLAUDE_LOG=str(self.calls),
                        FAKE_CLAUDE_STATE=str(fake_state), FAKE_REMOTE=str(self.remote),
                        HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        PATH=f"{self.bin}:{os.environ['PATH']}")
        self.workq("init")

    def tool(self, name, script):
        """Put a stand-in program (crontab, systemctl, ...) first on the PATH."""
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return path

    def workq(self, *args, check=True):
        proc = subprocess.run([sys.executable, str(WORKQ), *args], env=self.env,
                              capture_output=True, text=True, timeout=300)
        if check and proc.returncode != 0:
            raise AssertionError(f"workq {args} failed:\n{proc.stdout}\n{proc.stderr}")
        return proc

    def night(self, minutes=180, check=True):
        until = (dt.datetime.now() + dt.timedelta(minutes=minutes)).strftime("%H:%M")
        return self.workq("night", "--until", until, check=check)

    def start(self, *args):
        """Start workq in the background, for tests that kill it part-way."""
        return subprocess.Popen([sys.executable, str(WORKQ), *args], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for_call(self, count, timeout=30):
        """Wait until the fake Claude has been started `count` times; return that call."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            calls = self.claude_calls()
            if len(calls) >= count:
                return calls[count - 1]
            time.sleep(0.1)
        raise AssertionError(f"fake Claude was not started {count} time(s)")

    def set_config(self, **changes):
        cfg = yaml.safe_load(self.config.read_text())
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        self.config.write_text(yaml.safe_dump(cfg))

    def add(self, title, *directives):
        self.workq("add", title, "-m", "\n".join(directives))

    def clone(self):
        work = self.root / f"view-{time.time_ns()}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(work)], check=True)
        return work

    def where(self):
        """Map of item name -> folder it sits in."""
        work = self.clone()
        out = {}
        for brief in work.rglob("brief.md"):
            folder = brief.parent.parent.relative_to(work).as_posix()
            out[brief.parent.name] = folder
        return out, work

    def item(self, title_slug):
        where, work = self.where()
        for name, folder in where.items():
            if name.endswith(title_slug):
                path = work / folder / name
                meta_file = path / "meta.yaml"
                meta = yaml.safe_load(meta_file.read_text()) if meta_file.is_file() else {}
                return folder, path, meta
        raise AssertionError(f"no item matching {title_slug}: {where}")

    def push_file(self, rel, text, delete=False):
        work = self.clone()
        path = work / rel
        if delete:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "-c", "user.name=owner", "-c", "user.email=o@x", "commit", "-qm",
                        f"owner edits {rel}"], cwd=work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=work, check=True)

    def claude_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(l) for l in self.calls.read_text().splitlines()]

    def problems(self):
        path = self.state / "problems.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def flag(argv, name):
    return argv[argv.index(name) + 1]


class WorkqTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    # ---- the main path

    def test_happy_path_to_archive(self):
        e = self.env
        e.add("Compare suppliers", "fake-tier: haiku")
        e.night()
        folder, path, meta = e.item("compare-suppliers")
        self.assertEqual(folder, "review")
        self.assertEqual(meta["tier"], "haiku")
        self.assertEqual((meta["shape_attempts"], meta["execute_attempts"], meta["check_attempts"]), (1, 1, 1))
        self.assertEqual(meta["runs"], 3)
        self.assertTrue((path / "out" / "report.md").exists())
        self.assertEqual(len(list((path / "log").glob("*.json"))), 3)

        calls = e.claude_calls()
        self.assertEqual([c["stage"] for c in calls], ["shape", "execute", "check"])
        shape, execute, check = (c["argv"] for c in calls)
        self.assertEqual(flag(shape, "--model"), "opus")
        self.assertEqual(flag(execute, "--model"), "haiku")
        # The plan asked for 999 turns and $99; the stage ceiling wins.
        self.assertEqual(flag(execute, "--max-turns"), "30")
        self.assertEqual(flag(execute, "--max-budget-usd"), "0.50")
        # The plan asked for Bash, which isn't allowed by default.
        self.assertEqual(flag(execute, "--tools"), "Read,Write,WebSearch")
        self.assertNotIn("Bash", " ".join(execute))
        self.assertEqual(flag(shape, "--tools"), "Read,Glob,Grep,Write,Edit")
        self.assertEqual(flag(check, "--tools"), "Read,Glob,Grep,Write,Edit,WebFetch")
        for argv in (shape, execute, check):
            self.assertIn("--restricted", argv)
            self.assertEqual(flag(argv, "--permission-mode"), "dontAsk")
            # The prompt goes in on stdin, never as an argument after the variadic flags.
            self.assertNotIn("Work item folder", " ".join(argv[-3:]))

        e.push_file(f"review/{path.name}/review.md", "---\nverdict: approve\n---\nGood work.\n")
        e.night()
        folder, path, meta = e.item("compare-suppliers")
        month = dt.date.today().strftime("%Y-%m")
        self.assertEqual(folder, f"archive/{month}")
        self.assertEqual(meta["status"], "approved")
        self.assertFalse((path / "review.md").exists())
        self.assertIn("Good work.", (path / "notes.md").read_text())
        index = (path.parent.parent / "INDEX.md").read_text()
        self.assertIn(meta["id"], index)
        self.assertIn("approved", index)

    def test_loose_brief_file_is_tidied(self):
        e = self.env
        e.push_file("inbox/quick question.md", "# Quick question\n\nfake-tier: haiku\n")
        e.night()
        folder, path, meta = e.item("quick-question")
        self.assertEqual(folder, "review")
        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}-quick-question$")
        self.assertTrue((path / "brief.md").exists())
        self.assertEqual(meta["title"], "Quick question")

    # ---- check failures, retries and escalation

    def test_check_fail_retries_then_steps_up(self):
        e = self.env
        e.add("Escalate me", "fake-tier: haiku", "fake-check: fail,fail,pass")
        e.night()
        folder, path, meta = e.item("escalate-me")
        self.assertEqual(folder, "review")
        self.assertEqual(meta["tier"], "sonnet")
        self.assertEqual(meta["execute_attempts"], 3)
        self.assertEqual(meta["check_fails"], 2)
        models = [flag(c["argv"], "--model") for c in e.claude_calls() if c["stage"] == "execute"]
        self.assertEqual(models, ["haiku", "haiku", "sonnet"])
        self.assertEqual(len(list((path / "log").glob("check-*.md"))), 2)

    def test_three_failed_checks_block(self):
        e = self.env
        e.add("Never good", "fake-tier: haiku", "fake-check: fail,fail,fail")
        e.night()
        folder, path, meta = e.item("never-good")
        self.assertEqual(folder, "blocked")
        self.assertIn("3 execute attempts", meta["blocked_reason"])

    def test_opus_fail_twice_blocks(self):
        e = self.env
        e.add("Hard one", "fake-tier: opus", "fake-check: fail,fail")
        e.night()
        folder, _, meta = e.item("hard-one")
        self.assertEqual(folder, "blocked")
        self.assertEqual(meta["blocked_reason"], "check failed at opus")
        self.assertEqual(meta["execute_attempts"], 2)

    def test_bad_check_verdict_fails_check_stage(self):
        e = self.env
        e.add("Vague checker", "fake-check: bad")
        e.night()
        folder, _, meta = e.item("vague-checker")
        self.assertEqual(folder, "blocked")
        self.assertEqual(meta["check_attempts"], 3)
        self.assertIn("verdict must be pass or fail", meta["blocked_reason"])

    # ---- shaping outcomes

    def test_questions_block_then_retry_after_brief_edit(self):
        e = self.env
        e.add("Unclear ask", "fake-shape: questions")
        e.night()
        folder, path, meta = e.item("unclear-ask")
        self.assertEqual(folder, "blocked")
        self.assertEqual(meta["blocked_reason"], "shaper has questions")
        e.workq("digest")
        mail = e.mail.read_text()
        self.assertIn("Which year should I use?", mail)
        # The owner answers by editing the brief and asking for a retry.
        brief = (path / "brief.md").read_text().replace("fake-shape: questions", "fake-shape: plan")
        e.push_file(f"blocked/{path.name}/brief.md", brief)
        e.push_file(f"blocked/{path.name}/review.md", "verdict: retry\nUse 2026, in AUD.\n")
        e.night()
        folder, path, meta = e.item("unclear-ask")
        self.assertEqual(folder, "review")
        self.assertIn("Use 2026, in AUD.", (path / "notes.md").read_text())
        self.assertEqual(meta["shape_attempts"], 1)  # counters reset on retry

    def test_bad_tier_blocks_after_three(self):
        e = self.env
        e.add("Odd tier", "fake-shape: badtier")
        e.night()
        folder, _, meta = e.item("odd-tier")
        self.assertEqual(folder, "blocked")
        self.assertEqual(meta["shape_attempts"], 3)
        self.assertIn("no valid tier", meta["blocked_reason"])

    def test_replan_goes_back_to_shaping(self):
        e = self.env
        e.add("Needs replan", "fake-exec: replan-once")
        e.night()
        folder, _, meta = e.item("needs-replan")
        self.assertEqual(folder, "review")
        self.assertEqual(meta["shape_attempts"], 2)
        stages = [h["to"] for h in meta["history"]]
        self.assertEqual(stages, ["inbox", "ready/sonnet", "inbox", "ready/sonnet", "check", "review"])

    # ---- the guard

    def test_guard_reverts_write_outside_item(self):
        e = self.env
        e.add("Escape artist", "fake-shape: escape")
        e.night()
        folder, path, meta = e.item("escape-artist")
        self.assertEqual(folder, "blocked")
        self.assertIn("does not own: CLAUDE.md", meta["blocked_reason"])
        work = e.clone()
        self.assertNotIn("hacked", (work / "CLAUDE.md").read_text())
        self.assertFalse((path / "plan.md").exists())

    def test_guard_reverts_meta_edit(self):
        e = self.env
        e.add("Meta meddler", "fake-shape: touch-meta")
        e.run_once = e.workq("run", "shape")
        folder, _, meta = e.item("meta-meddler")
        self.assertEqual(folder, "inbox")
        self.assertNotEqual(meta["id"], "hacked")
        self.assertEqual(meta["shape_attempts"], 1)

    def test_guard_rejects_stray_files(self):
        e = self.env
        e.add("Messy", "fake-shape: stray")
        e.workq("run", "shape")
        folder, path, _ = e.item("messy")
        self.assertEqual(folder, "inbox")
        self.assertFalse((path / "scratch.md").exists())
        self.assertFalse((path / "plan.md").exists())

    def test_executor_cannot_plant_briefs(self):
        e = self.env
        e.add("Planter", "fake-exec: escape")
        e.night()
        where, work = e.where()
        self.assertFalse((work / "inbox" / "evil.md").exists())
        self.assertFalse(any("evil" in name for name in where))
        folder, _, meta = e.item("planter")
        self.assertEqual(folder, "blocked")

    def test_oversized_output_fails(self):
        e = Env(max_output_mb=1)
        try:
            e.add("Huge", "fake-exec: bigout")
            e.night()
            folder, path, meta = e.item("huge")
            self.assertEqual(folder, "blocked")
            self.assertIn("over 1 MB", meta["blocked_reason"])
            self.assertFalse((path / "out").exists())
        finally:
            e.cleanup()

    # ---- limits, time and pausing

    def test_usage_limit_gives_attempt_back_and_waits(self):
        e = self.env
        e.add("Limited", "fake-exec: limit-once")
        start = time.time()
        e.night()
        folder, _, meta = e.item("limited")
        self.assertEqual(folder, "review")
        self.assertEqual(meta["execute_attempts"], 1)
        self.assertTrue(any("usage limit" in p["problem"] for p in e.problems()))
        self.assertGreater(time.time() - start, 1.5)  # it slept until the reset

    def test_timeout_counts_as_failure(self):
        e = Env(stages={"exec-sonnet": {"tier": "sonnet", "timeout_min": 0.03,
                                        "max_turns": 10, "budget_usd": 1}})
        try:
            e.add("Slow", "fake-exec: timeout")
            e.night()
            folder, _, meta = e.item("slow")
            self.assertEqual(folder, "blocked")
            self.assertIn("time limit", meta["blocked_reason"])
        finally:
            e.cleanup()

    def test_broken_claude_stops_shift_without_using_attempts(self):
        broken = self.env.root / "broken-claude.sh"
        broken.write_text("#!/bin/sh\necho \"error: unknown option '--restricted'\" >&2\nexit 1\n")
        broken.chmod(0o755)
        e = Env(claude={"bin": str(broken), "agent_user": None, "restricted": True})
        try:
            e.add("First")
            e.add("Second")
            self.assertEqual(e.night(check=False).returncode, 1)   # a stopped shift is a failed one
            for slug in ("first", "second"):
                folder, _, meta = e.item(slug)
                self.assertEqual(folder, "inbox")
                self.assertEqual(meta["shape_attempts"], 0)
            runs = [json.loads(l) for l in (e.state / "ledger.jsonl").read_text().splitlines()]
            self.assertEqual(len(runs), 1)  # stopped after the first failure
            self.assertTrue(any("unknown option" in p["problem"] for p in e.problems()))
        finally:
            e.cleanup()

    def test_pause_stops_everything(self):
        e = self.env
        e.add("Paused work")
        e.workq("pause")
        e.night()
        self.assertEqual(e.item("paused-work")[0], "inbox")
        self.assertEqual(e.claude_calls(), [])
        e.workq("resume")
        e.night()
        self.assertEqual(e.item("paused-work")[0], "review")

    def test_nightly_cap_stops_a_tier(self):
        e = Env(caps={"opus": {"runs": 1, "usd": 100}})
        try:
            e.add("Capped")
            e.night()
            folder, _, _ = e.item("capped")
            self.assertEqual(folder, "check")  # shape used the one opus run; check waits
            self.assertEqual([c["stage"] for c in e.claude_calls()], ["shape", "execute"])
        finally:
            e.cleanup()

    def test_shift_does_not_start_runs_it_cannot_finish(self):
        e = self.env
        e.add("Too late")
        e.night(minutes=5)  # shape needs 15 minutes
        self.assertEqual(e.item("too-late")[0], "inbox")
        self.assertEqual(e.claude_calls(), [])

    # ---- review handling

    def test_rework_resets_and_runs_again(self):
        e = self.env
        e.add("Rework me", "fake-check: fail,pass")
        e.night()
        _, path, meta = e.item("rework-me")
        self.assertEqual(meta["execute_attempts"], 2)
        e.push_file(f"review/{path.name}/review.md", "---\nverdict: rework\n---\nAdd a chart.\n")
        e.night()
        folder, path, meta = e.item("rework-me")
        self.assertEqual(folder, "review")
        self.assertEqual(meta["execute_attempts"], 2)  # reset, then fail+pass again
        self.assertIn("Add a chart.", (path / "notes.md").read_text())

    def test_reject_archives_as_rejected(self):
        e = self.env
        e.add("Reject me")
        e.night()
        _, path, _ = e.item("reject-me")
        e.push_file(f"review/{path.name}/review.md", "verdict: reject\n")
        e.workq("run", "file")
        folder, _, meta = e.item("reject-me")
        self.assertTrue(folder.startswith("archive/"))
        self.assertEqual(meta["status"], "rejected")

    def test_bad_review_verdict_is_reported(self):
        e = self.env
        e.add("Typo review")
        e.night()
        _, path, _ = e.item("typo-review")
        e.push_file(f"review/{path.name}/review.md", "verdict: aprove\n")
        e.workq("run", "file")
        self.assertEqual(e.item("typo-review")[0], "review")
        self.assertTrue(any("aprove" in p["problem"] for p in e.problems()))

    # ---- git

    def test_owner_push_during_run_is_merged(self):
        e = self.env
        e.add("Racer", "fake-exec: race")
        e.night()
        where, work = e.where()
        self.assertEqual(e.item("racer")[0], "review")
        # The owner's brief arrived mid-run and was then shaped like any other.
        self.assertTrue(any(name.endswith("owner-added") for name in where))

    def test_init_refuses_non_empty_repo(self):
        proc = self.env.workq("init", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not empty", proc.stderr)

    # ---- digest

    def test_digest_contents(self):
        e = self.env
        e.add("Digest me", "fake-tier: haiku")
        e.add("Stuck", "fake-shape: questions")
        e.night()
        e.workq("digest")
        mail = e.mail.read_text()
        self.assertIn("To: me@example.test", mail)
        self.assertIn("Subject: workq digest", mail)
        self.assertIn("FOR REVIEW (1)", mail)
        self.assertIn("Supplier B is cheapest", mail)
        self.assertIn("https://example.test/q/tree/main/review/", mail)
        self.assertIn("BLOCKED (1)", mail)
        self.assertIn("shaper has questions", mail)
        self.assertIn("LAST NIGHT", mail)
        self.assertIn("opus: 3/15 runs", mail)
        # Problems are reported once: the next digest starts from now.
        e.workq("digest")
        self.assertIn("PROBLEMS (0)", e.mail.read_text())


class TinySMTP(threading.Thread):
    """Just enough of an SMTP server to receive one message on localhost."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.data = ""
        self.commands = []
        self.lines = []

    def run(self):
        conn, _ = self.sock.accept()
        fh = conn.makefile("rwb")

        def say(text):
            fh.write((text + "\r\n").encode())
            fh.flush()

        say("220 tiny")
        in_data = False
        for raw in fh:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if in_data:
                if line == ".":
                    in_data = False
                    say("250 ok")
                else:
                    self.data += line + "\n"
                continue
            self.commands.append(line.split(" ")[0].upper())
            self.lines.append(line)
            if line.upper().startswith(("EHLO", "HELO")):
                say("250 tiny")
            elif line.upper() == "DATA":
                in_data = True
                say("354 go")
            elif line.upper() == "QUIT":
                say("221 bye")
                break
            else:
                say("250 ok")
        conn.close()
        self.sock.close()


class SingleUserTests(unittest.TestCase):
    """What 0.2 added for a no-root, one-user install."""

    def setUp(self):
        self.envs = []

    def tearDown(self):
        for e in self.envs:
            e.cleanup()

    def make(self, **overrides):
        e = Env(**overrides)
        self.envs.append(e)
        return e

    # ---- what Claude is started with

    def test_claude_gets_a_scrubbed_environment_and_safe_flags(self):
        e = self.make()
        env_file = e.root / "claude.env"
        env_file.write_text("# token\nexport CLAUDE_CODE_OAUTH_TOKEN='tok-123'\nEMPTY=\n")
        settings = e.root / "settings.json"
        settings.write_text(json.dumps({"permissions": {"deny": ["Read(~/.ssh/**)"]}}))
        cfg = yaml.safe_load(e.config.read_text())
        cfg["claude"].update(env_file=str(env_file), settings=str(settings),
                             config_dir=str(e.root / "claude-home"))
        cfg["env"] = {"HTTPS_PROXY": "http://proxy.example.test:8080"}
        e.config.write_text(yaml.safe_dump(cfg))
        e.env.update(SSH_AUTH_SOCK="/tmp/agent.sock", AWS_SECRET_ACCESS_KEY="nope", MY_PASSWORD="nope")
        e.add("Env check")
        e.workq("run", "shape")
        call = e.claude_calls()[0]
        seen = call["env"]
        for name in ("SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "MY_PASSWORD", "WORKQ_CONFIG", "EMPTY"):
            self.assertNotIn(name, seen)
        self.assertEqual(seen["CLAUDE_CODE_OAUTH_TOKEN"], "tok-123")
        self.assertEqual(seen["HTTPS_PROXY"], "http://proxy.example.test:8080")
        self.assertEqual(seen["CLAUDE_CONFIG_DIR"], str(e.root / "claude-home"))
        self.assertEqual(seen["DISABLE_AUTOUPDATER"], "1")
        self.assertIn("HOME", seen)
        argv = call["argv"]
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--restricted", argv)
        self.assertEqual(json.loads(flag(argv, "--settings"))["permissions"]["deny"], ["Read(~/.ssh/**)"])
        # Single-user files are private.
        self.assertEqual(os.stat(e.state / "ledger.jsonl").st_mode & 0o077, 0)

    def test_missing_claude_gives_attempt_back_and_stops(self):
        e = self.make(claude={"bin": "/nonexistent/claude"})
        e.add("No claude")
        self.assertEqual(e.night(check=False).returncode, 1)
        folder, _, meta = e.item("no-claude")
        self.assertEqual(folder, "inbox")
        self.assertEqual(meta["shape_attempts"], 0)
        self.assertTrue(any("cannot start Claude Code" in p["problem"] for p in e.problems()))

    # ---- the sandbox

    @unittest.skipUnless(bwrap_ok(), "bubblewrap not available here")
    def test_sandbox_hides_home_secrets_and_state(self):
        e = self.make(claude={"sandbox": "bwrap"})
        env_file = e.root / "claude.env"
        env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=tok-123\n")
        cfg = yaml.safe_load(e.config.read_text())
        cfg["claude"]["env_file"] = str(env_file)
        e.config.write_text(yaml.safe_dump(cfg))
        (e.home / ".ssh" / "id_test").write_text("PRIVATE KEY")
        (e.root / "tmp-secret.txt").write_text("secret")
        e.add("Probe", "fake-exec: probe")
        e.night()
        folder, path, _ = e.item("probe")
        self.assertEqual(folder, "review")
        probe = dict(line.split("=", 1) for line in (path / "out" / "probe.txt").read_text().split())
        self.assertEqual(probe["ssh_key"], "hidden")
        self.assertEqual(probe["config"], "hidden")
        self.assertEqual(probe["ledger"], "hidden")
        self.assertEqual(probe["tmp_secret"], "hidden")
        self.assertEqual(probe["git_write"], "blocked")
        self.assertEqual(probe["etc_write"], "blocked")
        self.assertEqual(probe["proc_leak"], "hidden")       # no way round through /proc
        self.assertEqual(probe["token"], "tok-123")          # Claude itself still gets its token
        self.assertFalse((e.home / "planted.txt").exists())  # writes to "home" went nowhere
        self.assertFalse(Path("/etc/workq-probe").exists())
        log = json.loads(sorted((path / "log").glob("*-exec-sonnet.json"))[0].read_text())
        self.assertEqual(log["sandbox"], "bwrap")

    def test_without_sandbox_the_probe_sees_everything(self):
        # The contrast case: shows the probe really detects exposure when nothing hides it.
        e = self.make()
        (e.home / ".ssh" / "id_test").write_text("PRIVATE KEY")
        e.add("Probe", "fake-exec: probe-read")
        e.night()
        _, path, _ = e.item("probe")
        probe = dict(line.split("=", 1) for line in (path / "out" / "probe.txt").read_text().split())
        self.assertEqual(probe["ssh_key"], "visible")
        self.assertEqual(probe["config"], "visible")
        self.assertEqual(probe["ledger"], "visible")
        self.assertEqual(probe["proc_leak"], "visible")

    def test_required_sandbox_missing_claims_nothing(self):
        e = self.make(claude={"sandbox": "bwrap"})
        e.tool("bwrap", "exit 1\n")
        e.add("Needs sandbox")
        proc = e.workq("run", "shape", check=False)
        self.assertNotEqual(proc.returncode, 0)
        folder, _, meta = e.item("needs-sandbox")
        self.assertEqual(folder, "inbox")
        self.assertEqual(meta.get("shape_attempts", 0), 0)
        self.assertEqual(e.claude_calls(), [])
        self.assertTrue(any("bubblewrap is missing" in p["problem"] for p in e.problems()))

    # ---- .git protection

    def test_agent_cannot_plant_a_git_hook(self):
        e = self.make()
        e.add("Hooker", "fake-exec: githook")
        e.night(check=False)
        self.assertFalse((e.root / "hook-ran").exists())
        folder, _, _ = e.item("hooker")
        if os.geteuid() == 0:
            # Root ignores read-only bits, so the write lands and is caught afterwards.
            self.assertEqual(folder, "ready/sonnet")
            self.assertTrue(any(".git folder" in p["problem"] for p in e.problems()))
        else:
            self.assertEqual(folder, "review")

    def test_swapped_git_folder_is_caught_and_clone_discarded(self):
        e = self.make()
        e.add("Swapper", "fake-exec: gitswap")
        self.assertEqual(e.night(check=False).returncode, 1)
        self.assertTrue(any(".git folder" in p["problem"] for p in e.problems()))
        self.assertFalse((e.state / "clones" / "exec-sonnet").exists())
        self.assertFalse(Path("/tmp/pwned").exists())
        self.assertEqual(e.item("swapper")[0], "ready/sonnet")
        # The queue still works afterwards, from a fresh clone.
        e.workq("status")

    def test_runner_never_runs_repo_hooks(self):
        e = self.make()
        e.add("Hooks off")
        e.workq("run", "shape")
        hooks = e.state / "clones" / "exec-sonnet" / ".git" / "hooks"
        e.workq("run", "exec-sonnet")   # creates that clone
        (hooks / "post-commit").write_text(f"#!/bin/sh\ntouch {e.root}/hook-ran\n")
        (hooks / "post-commit").chmod(0o755)
        (hooks / "pre-push").write_text(f"#!/bin/sh\ntouch {e.root}/hook-ran\n")
        (hooks / "pre-push").chmod(0o755)
        e.add("Hooks off two")
        e.workq("run", "shape")
        e.workq("run", "exec-sonnet")
        self.assertFalse((e.root / "hook-ran").exists())

    # ---- git credentials

    def test_https_token_file_is_used_by_git(self):
        e = self.make()
        cred = e.root / "git-credentials"
        cred.write_text("https://anything:s3cret-token@dev.azure.example\n")
        cred.chmod(0o600)
        runner = load_runner()
        cfg = runner.load_config(e.config)
        cfg["git"]["credential_file"] = str(cred)
        cfg["git"]["ssh_key"] = "/home/me/.ssh/my key"
        repo = runner.Repo(cfg, e.root)
        out = subprocess.run([*repo.base, "credential", "fill"], env=dict(repo.env, HOME=str(e.home)),
                             input="protocol=https\nhost=dev.azure.example\n\n",
                             capture_output=True, text=True).stdout
        self.assertIn("password=s3cret-token", out)
        self.assertEqual(repo.env["GIT_SSH_COMMAND"],
                         "ssh -i '/home/me/.ssh/my key' -o IdentitiesOnly=yes -o BatchMode=yes "
                         "-o StrictHostKeyChecking=accept-new")

    # ---- mail

    def test_built_in_smtp_sends_the_digest(self):
        server = TinySMTP()
        server.start()
        e = self.make(digest={"to": "me@example.test", "from": "workq@example.test",
                              "smtp": {"host": "127.0.0.1", "port": server.port, "starttls": False}})
        e.add("Mail me", "fake-tier: haiku")
        e.night()
        e.workq("digest")
        server.join(timeout=10)
        self.assertIn("MAIL", server.commands)
        self.assertIn("To: me@example.test", server.data)
        self.assertIn("Subject: workq digest", server.data)
        self.assertIn("FOR REVIEW (1)", server.data)
        self.assertFalse(e.mail.exists())   # the command route was not used

    def test_failed_send_says_why_and_exits_nonzero(self):
        e = self.make()
        bad = e.root / "badmail.sh"
        bad.write_text("#!/bin/sh\necho 'relay access denied' >&2\nexit 75\n")
        bad.chmod(0o755)
        cfg = yaml.safe_load(e.config.read_text())
        cfg["digest"]["send_command"] = str(bad)
        e.config.write_text(yaml.safe_dump(cfg))
        proc = e.workq("digest", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("relay access denied", proc.stderr)
        self.assertIn("FOR REVIEW", proc.stdout)   # the body is still printed

    def test_smtp_refused_says_why(self):
        e = self.make(digest={"to": "me@example.test", "from": "w@example.test",
                              "smtp": {"host": "127.0.0.1", "port": 1, "starttls": False}})
        proc = e.workq("digest", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SMTP to 127.0.0.1", proc.stderr)

    # ---- schedule

    CRONTAB = 'F="{root}/crontab.txt"\nif [ "$1" = "-l" ]; then [ -f "$F" ] && cat "$F" && exit 0; echo "no crontab for test" >&2; exit 1; fi\ncat > "$F"\n'

    def cron_env(self, **overrides):
        e = self.make(**overrides)
        e.tool("crontab", self.CRONTAB.format(root=e.root))
        e.tool("systemctl", 'echo "Failed to connect to bus: No medium found" >&2\nexit 1\n')
        e.tool("loginctl", "exit 1\n")
        return e

    def test_schedule_with_cron_when_systemd_user_timers_cannot_be_used(self):
        e = self.cron_env(timezone="Asia/Tokyo", extra_shifts=[{"start": "12:00", "until": "15:00"}])
        (e.root / "crontab.txt").write_text("5 4 * * * /usr/bin/true  # mine\n")
        out = e.workq("schedule", "on").stdout
        self.assertIn("Scheduled with cron", out)
        text = (e.root / "crontab.txt").read_text()
        lines = text.splitlines()
        self.assertEqual(lines[0], "5 4 * * * /usr/bin/true  # mine")   # yours stay, and stay first
        self.assertIn("CRON_TZ=Asia/Tokyo", lines)
        night = [l for l in lines if l.startswith("0 22 * * * ")]
        self.assertEqual(len(night), 1)
        self.assertIn(f"{WORKQ} --config {e.config} night >> ", night[0])
        self.assertTrue(any(l.startswith("30 6 * * * ") and " digest >> " in l for l in lines))
        noon = [l for l in lines if l.startswith("0 12 * * * ")][0]
        self.assertRegex(noon, r"night --until 15:00 >> \S+ 2>&1; .* digest >> ")
        # Running it again replaces the block rather than adding a second one.
        e.workq("schedule", "on")
        again = (e.root / "crontab.txt").read_text()
        self.assertEqual(again.count("workq schedule <<<"), 1)
        self.assertEqual(again, text)
        self.assertIn("Installed: cron", e.workq("schedule", "show").stdout)
        # Asking for a route that can't work leaves the current schedule alone.
        proc = e.workq("schedule", "on", "--scheduler", "systemd", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((e.root / "crontab.txt").read_text(), text)
        e.workq("schedule", "off")
        self.assertEqual((e.root / "crontab.txt").read_text(), "5 4 * * * /usr/bin/true  # mine\n")

    def test_cron_lines_run_from_a_bare_cron_environment(self):
        # cron gives a job almost no environment; the scheduled line must still work.
        e = self.cron_env()
        e.workq("schedule", "on")
        line = [l for l in (e.root / "crontab.txt").read_text().splitlines() if " night >> " in l][0]
        command = line.split(" ", 5)[5].replace(" night >> ", " status >> ")
        python_dir = os.path.dirname(sys.executable)
        bare = {"HOME": str(e.home), "LOGNAME": "test", "SHELL": "/bin/sh",
                "PATH": f"{python_dir}:/usr/bin:/bin"}
        proc = subprocess.run(["/bin/sh", "-c", command], env=bare, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("inbox", (e.state / "logs" / "cron.log").read_text())

    def test_schedule_with_systemd_user_timers_when_lingering_is_on(self):
        e = self.make(timezone="Australia/Perth", extra_shifts=[{"start": "12:00", "until": "15:00"}])
        e.tool("systemctl", f'echo "$@" >> "{e.root}/systemctl.log"\nexit 0\n')
        e.tool("loginctl", 'echo "Linger=yes"\n')
        e.tool("crontab", self.CRONTAB.format(root=e.root))
        out = e.workq("schedule", "on").stdout
        self.assertIn("systemd user timers", out)
        units = e.home / ".config" / "systemd" / "user"
        self.assertEqual(sorted(p.name for p in units.iterdir()), [
            "workq-digest.service", "workq-digest.timer", "workq-night.service", "workq-night.timer",
            "workq-shift-1200.service", "workq-shift-1200.timer"])
        self.assertIn("OnCalendar=*-*-* 22:00:00 Australia/Perth", (units / "workq-night.timer").read_text())
        self.assertIn("OnCalendar=*-*-* 06:30:00 Australia/Perth", (units / "workq-digest.timer").read_text())
        shift = (units / "workq-shift-1200.service").read_text()
        self.assertIn(f"ExecStart=-{WORKQ} --config {e.config} night --until 15:00", shift)
        self.assertIn(f"ExecStart={WORKQ} --config {e.config} digest", shift)
        calls = (e.root / "systemctl.log").read_text()
        self.assertIn("--user daemon-reload", calls)
        self.assertIn("--user enable --now workq-night.timer workq-digest.timer workq-shift-1200.timer", calls)
        self.assertFalse((e.root / "crontab.txt").exists())
        e.workq("schedule", "off")
        self.assertEqual(list(units.iterdir()), [])
        self.assertIn("--user disable --now", (e.root / "systemctl.log").read_text())

    def test_lingering_off_falls_back_to_cron_and_says_so(self):
        e = self.make()
        e.tool("systemctl", "exit 0\n")
        e.tool("loginctl", 'echo "Linger=no"\n')
        e.tool("crontab", self.CRONTAB.format(root=e.root))
        out = e.workq("schedule", "on").stdout
        self.assertIn("Scheduled with cron", out)
        self.assertIn("enable-linger", out)
        self.assertFalse((e.home / ".config" / "systemd" / "user").exists())

    def test_no_scheduler_at_all_explains_what_to_ask_for(self):
        e = self.make()
        e.tool("systemctl", "exit 1\n")
        e.tool("loginctl", "exit 1\n")
        e.tool("crontab", 'echo "You (test) are not allowed to use this program (crontab)" >&2\nexit 1\n')
        proc = e.workq("schedule", "on", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("enable-linger", proc.stderr)
        self.assertIn("not allowed", proc.stderr)

    def test_unquoted_clock_times_in_the_config_still_work(self):
        e = self.cron_env()
        text = e.config.read_text() + "window:\n  start: 22:00\n  end: 06:00\ndigest_at_check: 12:30\n"
        e.config.write_text(text)
        self.assertEqual(yaml.safe_load(text)["window"]["start"], 1320)   # what YAML makes of 22:00
        out = e.workq("schedule", "show").stdout
        self.assertIn("22:00  night", out)

    # ---- doctor

    def test_doctor_passes_on_a_good_setup_and_flags_a_readable_secret(self):
        e = self.cron_env()
        env_file = e.root / "claude.env"
        env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=tok\n")
        env_file.chmod(0o600)
        cfg = yaml.safe_load(e.config.read_text())
        cfg["claude"]["env_file"] = str(env_file)
        e.config.write_text(yaml.safe_dump(cfg))
        proc = e.workq("doctor", "--live", check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for expected in ("single-user mode", "Claude Code has every option workq uses",
                         "a Claude credential is set", "queue repo reachable, branch main exists",
                         "live test: Claude answered", "nothing is scheduled yet"):
            self.assertIn(expected, proc.stdout)
        env_file.chmod(0o644)
        proc = e.workq("doctor", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("can be read by other users", proc.stdout)

    def test_doctor_reports_an_old_claude_and_an_unreachable_repo(self):
        e = self.cron_env()
        e.env["FAKE_HELP_FLAGS"] = "--strict-mcp-config"      # this "Claude" has no --restricted
        cfg = yaml.safe_load(e.config.read_text())
        cfg["repo_url"] = str(e.root / "nowhere.git")
        e.config.write_text(yaml.safe_dump(cfg))
        proc = e.workq("doctor", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("lacks --restricted", proc.stdout)
        self.assertIn("cannot reach the queue repo", proc.stdout)
        self.assertIn("no Claude credential found", proc.stdout)

    def test_bundled_yaml_is_used_when_asked(self):
        e = self.cron_env()
        e.env["WORKQ_BUNDLED_YAML"] = "1"
        self.assertIn("bundled PyYAML", e.workq("doctor", check=False).stdout)
        e.add("Bundled", "fake-tier: haiku")
        e.night()
        self.assertEqual(e.item("bundled")[0], "review")


def alive(pid):
    """True if the process exists and is not a finished one waiting to be collected."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def gone_within(pid, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


class FixesIn021Tests(unittest.TestCase):
    """One test or more for each fault found in the 0.2.0 review."""

    SLOW = {"exec-sonnet": {"tier": "sonnet", "timeout_min": 1, "max_turns": 10, "budget_usd": 1}}

    def setUp(self):
        self.envs = []

    def tearDown(self):
        for e in self.envs:
            e.cleanup()

    def make(self, **overrides):
        e = Env(**overrides)
        self.envs.append(e)
        return e

    # ---- 1. a runner stopped part-way through a run

    def slow_item(self, e):
        e.add("Slow", "fake-exec: timeout")     # the fake sleeps 30 seconds
        e.workq("run", "shape")
        return e.state / "clones" / "exec-sonnet" / ".git"

    def unslow(self, e):
        _, path, _ = e.item("slow")
        e.push_file(f"ready/sonnet/{path.name}/brief.md", "# Slow\n\nNow quick.\n")

    def test_sigterm_stops_the_agent_unlocks_the_clone_and_the_stage_still_works(self):
        e = self.make(stages=self.SLOW)
        git = self.slow_item(e)
        runner = e.start("run", "exec-sonnet")
        agent = e.wait_for_call(2)["pid"]
        self.assertFalse(os.stat(git).st_mode & 0o222)      # locked while the agent works
        runner.send_signal(signal.SIGTERM)
        runner.communicate(timeout=40)
        self.assertEqual(runner.returncode, 143)
        self.assertTrue(gone_within(agent, 5), "the agent outlived the runner")
        self.assertTrue(os.stat(git).st_mode & 0o200)       # unlocked on the way out
        self.unslow(e)
        self.assertEqual(e.workq("run", "exec-sonnet").returncode, 0)
        folder, _, meta = e.item("slow")
        self.assertEqual(folder, "check")
        self.assertEqual(meta["unfinished"], 1)
        self.assertFalse(any("Permission denied" in p["problem"] for p in e.problems()))

    def test_sigkill_leaves_a_locked_clone_that_the_next_run_repairs(self):
        e = self.make(stages=self.SLOW)
        git = self.slow_item(e)
        runner = e.start("run", "exec-sonnet")
        agent = e.wait_for_call(2)["pid"]
        runner.kill()                                       # no chance to clean up
        runner.communicate(timeout=20)
        self.assertFalse(os.stat(git).st_mode & 0o222)      # still locked: this is what 0.2.0 tripped on
        self.assertTrue(gone_within(agent, 5), "the agent outlived a killed runner")
        self.unslow(e)
        self.assertEqual(e.workq("run", "exec-sonnet").returncode, 0)
        self.assertEqual(e.item("slow")[0], "check")
        self.assertEqual(e.problems(), [])

    def test_item_whose_runs_never_finish_is_blocked_after_three(self):
        e = self.make(stages=self.SLOW)
        self.slow_item(e)
        for n in (2, 3, 4):
            runner = e.start("run", "exec-sonnet")
            e.wait_for_call(n)
            runner.kill()
            runner.communicate(timeout=20)
        e.workq("run", "exec-sonnet")
        folder, _, meta = e.item("slow")
        self.assertEqual(folder, "blocked")
        self.assertIn("3 runs ended without finishing", meta["blocked_reason"])
        self.assertEqual(len(e.claude_calls()), 4)          # the fourth run was not started

    # ---- 2. two-user mode: the agent user and the runner's state

    def two_user(self, **overrides):
        """Two-user mode with stand-ins: `sudo` and the wrapper run the fake as this user."""
        e = self.make(**overrides)
        wrapper = e.root / "wrapper.sh"
        wrapper.write_text(f"#!/bin/sh\numask 002\nexec {FAKE} \"$@\"\n")
        wrapper.chmod(0o755)
        e.tool("sudo", "shift 4\nexec \"$@\"\n")          # drops: -n -H -u USER
        e.set_config(claude={"agent_user": "someone-else", "wrapper": str(wrapper)})
        return e

    def test_two_user_mode_opens_only_the_working_clone_to_the_agent(self):
        e = self.two_user()
        e.add("Perms", "fake-exec: perms")
        e.night()
        folder, path, _ = e.item("perms")
        self.assertEqual(folder, "review")
        seen = dict(line.split("=") for line in (path / "out" / "perms.txt").read_text().split())
        self.assertEqual(seen["state"], "0o710")            # may pass through, not list or write
        self.assertEqual(seen["clones"], "0o710")
        self.assertEqual(seen["ledger"], "0o600")
        self.assertEqual(seen["other_clone"], "0o700")      # every other clone is shut
        self.assertEqual(seen["clone"], "0o2770")           # its own clone is open for the run
        self.assertEqual(seen["itemdir"], "0o2770")
        self.assertEqual(seen["brief"], "0o660")
        self.assertEqual(seen["git"], "0o550")              # readable, never writable
        self.assertEqual(int(seen["gitconfig"], 8) & 0o222, 0)
        self.assertEqual(seen["umask"], "0o2")
        # At rest every clone is shut again, and nothing in the state folder is group-writable.
        for clone in (e.state / "clones").iterdir():
            self.assertEqual(stat.S_IMODE(clone.stat().st_mode), 0o700, clone.name)
        for entry in e.state.iterdir():
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode) & 0o027, 0, entry.name)
        self.assertNotIn("sharedrepository", (e.state / "clones" / "shape" / ".git" / "config").read_text().lower())

    def test_state_left_group_writable_by_an_older_version_is_tightened(self):
        e = self.two_user()
        e.add("Old state")
        e.workq("run", "shape")
        # Put things back the way 0.1 and 0.2.0 left them.
        clone = e.state / "clones" / "shape"
        os.chmod(e.state, 0o2770)
        os.chmod(e.state / "ledger.jsonl", 0o664)
        os.chmod(clone, 0o2775)
        os.chmod(clone / ".git", 0o2775)
        os.chmod(clone / ".git" / "config", 0o664)
        subprocess.run(["git", "config", "core.sharedRepository", "group"], cwd=clone, check=True)
        e.add("Second")
        e.workq("run", "shape")
        self.assertEqual(stat.S_IMODE(e.state.stat().st_mode), 0o710)
        self.assertEqual(stat.S_IMODE((e.state / "ledger.jsonl").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(clone.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((clone / ".git" / "config").stat().st_mode) & 0o022, 0)
        self.assertNotIn("sharedrepository", (clone / ".git" / "config").read_text().lower())

    # ---- 3. replan loop

    def test_executor_that_always_asks_for_a_new_plan_is_blocked(self):
        e = self.make()
        e.add("Loop", "fake-exec: replan-always")
        e.night()
        folder, _, meta = e.item("loop")
        self.assertEqual(folder, "blocked")
        self.assertEqual(meta["shape_attempts"], 3)
        self.assertIn("asked for a new plan after 3 plans", meta["blocked_reason"])
        self.assertEqual(len(e.claude_calls()), 6)          # three plans, three tries, then stop

    # ---- 4. symbolic links

    def test_links_made_by_an_agent_are_removed_and_never_written_through(self):
        e = self.make()
        e.add("Linker", "fake-exec: symlink")
        e.night()
        folder, path, meta = e.item("linker")
        self.assertEqual(folder, "blocked")
        self.assertIn("does not own", meta["blocked_reason"])
        self.assertEqual(list((e.root / "outside").iterdir()), [])     # nothing landed outside the repo
        work = e.clone()
        links = [p for p in work.rglob("*") if p.is_symlink() and ".git" not in p.parts]
        self.assertEqual(links, [])
        self.assertTrue((path / "log").is_dir())                       # the real log folder is back
        self.assertEqual(len(list((path / "log").glob("*-exec-sonnet*.json"))), 3)

    def test_link_pushed_into_the_repo_is_deleted_not_followed(self):
        e = self.make()
        victim = e.root / "victim.txt"
        victim.write_text("ORIGINAL\n")
        work = e.clone()
        item = work / "inbox" / "2026-11-10-linked"
        item.mkdir(parents=True)
        (item / "brief.md").write_text("# Linked\n\nfake-tier: haiku\n")
        os.symlink(victim, item / "meta.yaml")       # the runner writes meta.yaml: aim it elsewhere
        os.symlink(victim, work / "inbox" / "loose.md")
        for cmd in (["add", "-A"], ["-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "links"],
                    ["push", "-q", "origin", "HEAD"]):
            subprocess.run(["git", *cmd], cwd=work, check=True)
        e.night()
        self.assertEqual(victim.read_text(), "ORIGINAL\n")
        folder, path, meta = e.item("linked")
        self.assertEqual(folder, "review")
        self.assertFalse((path / "meta.yaml").is_symlink())
        self.assertEqual(meta["tier"], "haiku")
        self.assertTrue(any("removed symbolic links" in p["problem"] for p in e.problems()))
        modes = subprocess.run(["git", "ls-tree", "-r", "HEAD"], cwd=path.parents[1],
                               capture_output=True, text=True).stdout
        self.assertNotIn("120000", modes)             # git's mode for a symbolic link

    # ---- 5. mail password

    def test_mail_password_is_not_sent_unencrypted(self):
        e = self.make(digest={"to": "me@example.test", "from": "w@example.test",
                              "smtp": {"host": "smtp.example.test", "port": 25, "starttls": False,
                                       "user": "me", "password_file": "/dev/null"}})
        proc = e.workq("digest", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to send the mail password unencrypted", proc.stderr)

    # ---- smaller faults

    def test_helper_commands_wait_for_their_lock_then_give_up(self):
        e = self.make(lock_wait_s=1)
        holder = open(e.state / "locks" / "tools.lock", "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.time()
        proc = e.workq("add", "While locked", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("another workq command is still running", proc.stderr)
        self.assertGreater(time.time() - started, 0.9)
        e.set_config(lock_wait_s=20)
        threading.Timer(1.0, holder.close).start()          # closing the file lets go of the lock
        e.workq("add", "After the lock")
        self.assertEqual(e.item("after-the-lock")[0], "inbox")
        self.assertRaises(AssertionError, e.item, "while-locked")

    def test_blank_and_numeric_env_values_in_the_config(self):
        e = self.make(env={"NO_PROXY": None, "EMPTY": "", "RETRIES": 3, "HTTPS_PROXY": "http://proxy.example.test:1"})
        e.add("Env values")
        e.workq("run", "shape")
        seen = e.claude_calls()[0]["env"]
        self.assertEqual(seen["RETRIES"], "3")
        self.assertEqual(seen["HTTPS_PROXY"], "http://proxy.example.test:1")
        self.assertNotIn("EMPTY", seen)

    def test_claude_runs_in_the_config_time_zone(self):
        e = self.make(timezone="Asia/Tokyo")
        e.add("Zone")
        e.workq("run", "shape")
        self.assertEqual(e.claude_calls()[0]["env"]["TZ"], "Asia/Tokyo")

    def test_unexpected_error_sets_one_item_aside_and_the_shift_goes_on(self):
        e = self.make()
        e.push_file("inbox/2026-01-01-poison/brief.md", "# Poison\n")
        e.push_file("inbox/2026-01-01-poison/meta.yaml/keep", "meta.yaml is a folder here\n")
        e.add("Healthy", "fake-tier: haiku")
        proc = e.night()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(e.item("healthy")[0], "review")
        self.assertEqual(e.item("poison")[0], "inbox")
        hits = [p for p in e.problems() if "unexpected error, item set aside" in p["problem"]]
        self.assertEqual(len(hits), 1)                      # reported once, not once per pass

    def test_yaml_aliases_and_broken_meta_are_harmless(self):
        e = self.make()
        e.add("Bomb", "fake-shape: alias")
        started = time.time()
        e.workq("run", "shape")
        self.assertLess(time.time() - started, 20)
        folder, _, _ = e.item("bomb")
        self.assertEqual(folder, "inbox")
        self.assertTrue(any("no valid tier" in p["problem"] for p in e.problems()))
        runner = load_runner()
        self.assertEqual(runner.front_matter("---\na: &x [1]\ntier: *x\n---\nbody")[0], {})
        broken = e.root / "item"
        broken.mkdir()
        (broken / "meta.yaml").write_text("history: nope\nshape_attempts: lots\nruns: -4\ncost_usd: [1]\n")
        meta = runner.load_meta(broken)
        self.assertEqual((meta["history"], meta["shape_attempts"], meta["runs"], meta["cost_usd"]), ([], 0, 0, 0.0))
        (broken / "meta.yaml").write_text("[not, a, mapping\n")
        self.assertEqual(runner.load_meta(broken)["execute_attempts"], 0)

    def test_run_that_ends_in_an_error_is_not_used_and_says_why(self):
        e = self.make()
        e.add("Out of turns", "fake-shape: error-after-plan")
        e.workq("run", "shape")
        folder, path, meta = e.item("out-of-turns")
        self.assertEqual(folder, "inbox")                   # 0.2.0 sent this on with a half-written plan
        self.assertFalse((path / "plan.md").exists())
        self.assertEqual(meta["shape_attempts"], 1)
        self.assertTrue(any("Reached maximum number of turns" in p["problem"] for p in e.problems()))

    def test_oversized_text_file_is_refused(self):
        e = self.make(max_text_mb=0.002)
        e.add("Big plan", "fake-shape: bigplan")
        e.workq("run", "shape")
        self.assertEqual(e.item("big-plan")[0], "inbox")
        self.assertTrue(any("plan.md is over 0.002 MB" in p["problem"] for p in e.problems()))

    def test_token_in_the_repo_url_is_never_printed(self):
        e = self.make()
        e.tool("crontab", "exit 1\n")
        e.tool("systemctl", "exit 1\n")
        e.tool("loginctl", "exit 1\n")
        e.env["NO_PROXY"] = e.env["no_proxy"] = "127.0.0.1"
        e.set_config(repo_url="https://buildbot:s3cret-t0ken@127.0.0.1:9/q.git")
        doctor = e.workq("doctor", check=False)
        self.assertIn("repo_url has a password or token in it", doctor.stdout)
        run = e.workq("run", "shape", check=False)
        status = e.workq("status", check=False)
        everything = doctor.stdout + doctor.stderr + run.stdout + run.stderr + status.stdout + status.stderr
        everything += (e.state / "problems.jsonl").read_text()
        self.assertNotIn("s3cret-t0ken", everything)
        self.assertIn("https://***@127.0.0.1:9/q.git", everything)
        runner = load_runner()
        self.assertEqual(runner.redact("see https://tok@host/x and git@github.com:me/q.git"),
                         "see https://***@host/x and git@github.com:me/q.git")

    def test_crontab_block_without_its_end_marker_keeps_your_lines(self):
        runner = load_runner()
        ours = "0 22 * * * /home/me/.local/share/workq/bin/workq --config /home/me/.config/workq/config.yaml night >> /x 2>&1"
        text = "\n".join(["1 1 * * * mine-above", runner.CRON_BEGIN, "CRON_TZ=Australia/Perth", ours,
                          "2 2 * * * mine-below", ""])
        self.assertEqual(runner.strip_cron_block(text), "1 1 * * * mine-above\n2 2 * * * mine-below\n")
        whole = "\n".join(["1 1 * * * mine", runner.CRON_BEGIN, ours, runner.CRON_END, "3 3 * * * later", ""])
        self.assertEqual(runner.strip_cron_block(whole), "1 1 * * * mine\n3 3 * * * later\n")

    def test_odd_reset_times_fall_back_to_the_default_wait(self):
        runner = load_runner()
        e = self.make()
        cfg = runner.load_config(e.config)
        for text in ("limit reached, resets 5:75", "resets 31", "limit reached"):
            wait = runner.parse_reset(text, cfg) - time.time()
            self.assertAlmostEqual(wait, float(cfg["limit_wait_min"]) * 60, delta=5, msg=text)
        self.assertEqual(runner.parse_reset("usage limit reached|1900000000", cfg), 1900000000.0)

    def test_changed_git_ref_is_caught(self):
        e = self.make()
        e.add("Ref writer", "fake-exec: githead")
        proc = e.night(check=False)
        folder, _, _ = e.item("ref-writer")
        if os.geteuid() == 0:       # root ignores the read-only lock, so the change lands and is caught
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(folder, "ready/sonnet")
            self.assertTrue(any(".git folder" in p["problem"] for p in e.problems()))
        else:                       # anyone else is stopped by the lock
            self.assertEqual(folder, "review")


class DigestWithoutMailTests(unittest.TestCase):
    """0.2.2: email is optional. With none set, the digest is a file in the queue repo."""

    NO_MAIL = {"to": None, "send_command": "/nonexistent/sendmail -t"}

    def setUp(self):
        self.envs = []

    def tearDown(self):
        for e in self.envs:
            e.cleanup()

    def make(self, **overrides):
        e = Env(**overrides)
        self.envs.append(e)
        return e

    def log_subjects(self, e):
        return subprocess.run(["git", "log", "--format=%s"], cwd=e.clone(), capture_output=True,
                              text=True, check=True).stdout.splitlines()

    def test_no_email_writes_the_digest_to_the_repo(self):
        e = self.make(digest=self.NO_MAIL)
        e.add("Digest me", "fake-tier: haiku")
        e.add("Stuck", "fake-shape: questions")
        e.night()
        proc = e.workq("digest")
        self.assertIn("digest written to DIGEST.md", proc.stdout + proc.stderr)
        self.assertFalse(e.mail.exists())                       # no mail program was run
        text = (e.clone() / "DIGEST.md").read_text()
        self.assertTrue(text.startswith("# workq digest, "))
        self.assertRegex(text, r"Written at \d\d:\d\d")
        self.assertIn("1 for review, 1 blocked, 0 waiting in inbox", text)
        self.assertIn("## For review (1)", text)
        self.assertIn("- **Digest me** (haiku, new)", text)
        self.assertIn("Supplier B is cheapest", text)
        self.assertRegex(text, r"\[review/\S+-digest-me\]\(https://example\.test/q/tree/main/review/\S+-digest-me\)")
        self.assertIn("## Blocked (1)", text)
        self.assertIn("shaper has questions", text)
        self.assertIn("## Last night (shift ", text)
        self.assertIn("- opus: 3/15 runs", text)
        self.assertIn("add `review.md`", text)
        self.assertTrue(any(l.startswith("workq: digest ") for l in self.log_subjects(e)))

    def test_problems_are_reported_once_without_email(self):
        e = self.make(digest=self.NO_MAIL)
        e.add("Wanders", "fake-tier: haiku", "fake-exec: escape")
        e.night()
        e.workq("digest")
        first = (e.clone() / "DIGEST.md").read_text()
        self.assertNotIn("## Problems (0)", first)
        e.workq("digest")
        self.assertIn("## Problems (0)", (e.clone() / "DIGEST.md").read_text())

    def test_links_stay_inside_the_repo_when_there_is_no_web_url(self):
        e = self.make(digest=self.NO_MAIL, web_url="")
        e.add("Digest me", "fake-tier: haiku")
        e.night()
        e.workq("digest")
        text = (e.clone() / "DIGEST.md").read_text()
        self.assertRegex(text, r"\[(review/\S+-digest-me)\]\(\1\)")

    def test_email_set_means_no_file_unless_asked(self):
        e = self.make()                                          # the test default has digest.to set
        e.workq("digest")
        self.assertIn("Subject: workq digest", e.mail.read_text())
        self.assertFalse((e.clone() / "DIGEST.md").exists())
        e.set_config(digest={"repo_file": "STATUS.md"})
        e.mail.unlink()
        e.workq("digest")
        self.assertIn("Subject: workq digest", e.mail.read_text())
        work = e.clone()
        self.assertTrue((work / "STATUS.md").is_file())
        self.assertFalse((work / "DIGEST.md").exists())

    def test_repo_file_true_writes_the_file_as_well_as_the_email(self):
        e = self.make(digest={"repo_file": True})
        e.workq("digest")
        self.assertTrue(e.mail.exists())
        self.assertTrue((e.clone() / "DIGEST.md").is_file())

    def test_no_email_and_no_file_prints_and_changes_nothing(self):
        e = self.make(digest=dict(self.NO_MAIL, repo_file=False))
        before = self.log_subjects(e)
        proc = e.workq("digest")
        self.assertIn("FOR REVIEW (0)", proc.stdout)
        self.assertEqual(self.log_subjects(e), before)
        self.assertFalse(e.mail.exists())
        doctor = e.workq("doctor", check=False).stdout
        self.assertIn("the digest goes nowhere", doctor)

    def test_stdout_never_writes_the_file(self):
        e = self.make(digest=self.NO_MAIL)
        before = self.log_subjects(e)
        proc = e.workq("digest", "--stdout")
        self.assertIn("FOR REVIEW (0)", proc.stdout)
        self.assertEqual(self.log_subjects(e), before)

    def test_bad_file_names_are_refused(self):
        for name in ("../DIGEST.md", "review/DIGEST.md", "/tmp/x.md", "CLAUDE.md", "PAUSE",
                     "inbox", ".git", "a b.md"):
            e = self.make(digest=dict(self.NO_MAIL, repo_file="DIGEST.md"))
            e.set_config(digest={"repo_file": name})
            proc = e.workq("digest", check=False)
            self.assertNotEqual(proc.returncode, 0, name)
            self.assertIn("digest.repo_file", proc.stderr, name)

    def test_text_from_briefs_cannot_add_links_or_html_to_the_file(self):
        e = self.make(digest=self.NO_MAIL)
        e.add("See [here](http://evil.test) <img src=x> *now*", "fake-shape: questions")
        e.night()
        e.workq("digest")
        text = (e.clone() / "DIGEST.md").read_text()
        self.assertIn(r"\[here\](http://evil.test) \<img src=x\> \*now\*", text)
        self.assertNotIn("[here](http://evil.test)", text)
        self.assertNotRegex(text, r"(?<!\\)<img")              # every < is escaped

    def test_doctor_is_content_without_email(self):
        e = self.make(digest=self.NO_MAIL)
        out = e.workq("doctor", check=False).stdout
        self.assertIn("digest is written to DIGEST.md in the queue repo (no email", out)
        self.assertNotIn("sendmail", out)
        self.assertFalse([l for l in out.splitlines() if "digest" in l and not l.startswith("[ok")])

    def test_the_file_does_not_get_in_the_way_of_a_shift(self):
        e = self.make(digest=self.NO_MAIL)
        e.workq("digest")
        e.add("After digest", "fake-tier: haiku")
        e.night()
        folder, _path, _meta = e.item("after-digest")
        self.assertEqual(folder, "review")
        e.workq("digest")
        work = e.clone()
        self.assertIn("**After digest**", (work / "DIGEST.md").read_text())
        self.assertEqual(e.problems(), [])

    def test_an_agent_cannot_change_the_file(self):
        e = self.make(digest=self.NO_MAIL)
        e.workq("digest")
        before = (e.clone() / "DIGEST.md").read_text()
        e.add("Meddler", "fake-tier: haiku", "fake-exec: digest-edit")
        e.night(check=False)
        self.assertEqual((e.clone() / "DIGEST.md").read_text(), before)
        self.assertTrue(any("DIGEST.md" in p["problem"] for p in e.problems()))

    def test_failed_push_is_reported_and_tried_again(self):
        e = self.make(digest=self.NO_MAIL)
        hook = e.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'no pushes today' >&2\nexit 1\n")
        hook.chmod(0o755)
        proc = e.workq("digest", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("digest not written: could not push DIGEST.md", proc.stderr)
        self.assertIn("FOR REVIEW", proc.stdout)                # the body is still printed
        hook.unlink()
        e.workq("digest")
        text = (e.clone() / "DIGEST.md").read_text()
        self.assertIn("could not push DIGEST.md", text)         # the miss shows in the next digest

    def test_a_folder_with_the_same_name_is_reported(self):
        e = self.make(digest=self.NO_MAIL)
        e.push_file("DIGEST.md/keep.txt", "in the way\n")
        proc = e.workq("digest", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("DIGEST.md is a folder", proc.stderr)

    def test_a_link_with_the_same_name_is_replaced_not_followed(self):
        e = self.make(digest=self.NO_MAIL)
        victim = e.root / "victim.txt"
        victim.write_text("keep me\n")
        work = e.clone()
        os.symlink(victim, work / "DIGEST.md")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-qm", "link"],
                       cwd=work, check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=work, check=True)
        e.workq("digest", check=False)
        e.workq("digest")
        self.assertEqual(victim.read_text(), "keep me\n")
        path = e.clone() / "DIGEST.md"
        self.assertFalse(path.is_symlink())
        self.assertIn("# workq digest", path.read_text())

    def test_schedule_still_runs_the_digest_without_email(self):
        e = self.make(digest=self.NO_MAIL)
        store = e.root / "crontab.txt"
        e.tool("crontab", f'if [ "$1" = "-l" ]; then cat {store} 2>/dev/null; exit 0; fi\ncat > {store}\n')
        e.workq("schedule", "on", "--scheduler", "cron")
        self.assertRegex(store.read_text(), r"(?m)^30 6 \* \* \* .* digest >> ")


class BrokenPipeTests(unittest.TestCase):
    """A closed pipe must never kill the runner part-way through its work."""

    def make(self, **overrides):
        e = Env(**overrides)
        self.addCleanup(e.cleanup)
        return e

    def closed_stdout(self, e, *args):
        """Run workq with its output going to a pipe nobody reads."""
        r, w = os.pipe()
        os.close(r)
        try:
            return subprocess.run([sys.executable, str(WORKQ), *args], env=e.env, stdout=w,
                                  stderr=subprocess.PIPE, text=True, timeout=120)
        finally:
            os.close(w)

    def test_runner_is_not_killed_when_claude_exits_without_reading_its_prompt(self):
        # A Claude that fails at once (bad option, sudo refusal) closes its stdin before
        # the prompt is written. With SIGPIPE left at its default, that write killed the
        # runner in silence. The stand-in Claude records how its parent treats SIGPIPE.
        e = self.make()
        seen = e.root / "sigign.txt"
        broken = e.root / "broken-claude.sh"
        broken.write_text(f"#!/bin/sh\ngrep SigIgn /proc/$PPID/status > {seen}\n"
                          "echo \"error: unknown option '--restricted'\" >&2\nexit 1\n")
        broken.chmod(0o755)
        e.set_config(claude={"bin": str(broken), "agent_user": None, "sandbox": "off"})
        e.add("First")
        proc = e.night(check=False)
        self.assertEqual(proc.returncode, 1)
        mask = int(seen.read_text().split()[1], 16)
        self.assertTrue(mask & (1 << (signal.SIGPIPE - 1)), "runner must ignore SIGPIPE")
        self.assertEqual(e.item("first")[0], "inbox")
        self.assertNotIn("running", e.item("first")[2])

    def test_status_into_a_closed_pipe_leaves_quietly(self):
        e = self.make()
        e.add("First")
        proc = self.closed_stdout(e, "status")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    def test_shift_keeps_working_when_nobody_reads_its_output(self):
        e = self.make()
        e.add("First", "fake-tier: haiku")
        until = (dt.datetime.now() + dt.timedelta(minutes=180)).strftime("%H:%M")
        proc = self.closed_stdout(e, "night", "--until", until)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(e.item("first")[0], "review")


class MailAllowlistTests(unittest.TestCase):
    """0.2.3: mail goes only to addresses in digest.allowed_domains."""

    DOMAINS = ["company.com.au", "anothercompany.com"]

    def make(self, to, domains=DOMAINS, **digest):
        e = Env(digest=dict({"to": to, "allowed_domains": domains}, **digest))
        self.addCleanup(e.cleanup)
        return e

    def sent_to(self, e):
        """Run the digest. Returns (proc, the To: line of the mail sent, or None)."""
        proc = e.workq("digest", check=False)
        if not e.mail.exists():
            return proc, None
        line = next(x for x in e.mail.read_text().splitlines() if x.startswith("To:"))
        return proc, line[3:].strip()

    def assert_refused(self, to, domains=DOMAINS, why="not in an allowed domain"):
        e = self.make(to, domains)
        proc, sent = self.sent_to(e)
        self.assertIsNone(sent, f"mail went to {sent}")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(why, proc.stderr)
        self.assertTrue(any(why in p["problem"] for p in e.problems()))   # shows in the next digest
        self.assertIn("FOR REVIEW", proc.stdout)                           # the body is still printed
        return e

    def test_address_in_an_allowed_domain_is_sent(self):
        proc, sent = self.sent_to(self.make("david@company.com.au"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sent, "david@company.com.au")

    def test_several_domains_and_several_addresses(self):
        e = self.make(["David <David@COMPANY.com.au>", "ops@anothercompany.com"])
        proc, sent = self.sent_to(e)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sent, "David@company.com.au, ops@anothercompany.com")

    def test_domains_as_a_comma_string_with_at_signs_and_capitals(self):
        proc, sent = self.sent_to(self.make("a@anothercompany.com", "@Company.com.au, AnotherCompany.com"))
        self.assertEqual(sent, "a@anothercompany.com")

    def test_address_outside_the_list_is_refused(self):
        self.assert_refused("me@gmail.com")

    def test_one_bad_address_stops_the_whole_mail(self):
        self.assert_refused("david@company.com.au, me@gmail.com")

    def test_lookalike_domains_are_refused(self):
        for to in ("d@company.com.au.evil.com", "d@evilcompany.com.au", "d@sub.company.com.au",
                   "d@company.com", "d@company.com.au."[:-1] + "x"):
            with self.subTest(to=to):
                self.assert_refused(to)

    def test_tricks_in_the_address_are_refused(self):
        for to in ("d@evil.com@company.com.au", '"d@company.com.au" <d@evil.com>',
                   "d@company.com.au\nBcc: e@evil.com", "notanaddress", "<>"):
            with self.subTest(to=to):
                e = self.make(to)
                proc, sent = self.sent_to(e)
                self.assertIsNone(sent, f"{to!r} went to {sent}")
                self.assertEqual(proc.returncode, 1)
                self.assertIn("mail not sent", proc.stderr)

    def test_empty_list_sends_nothing(self):
        for domains in ([], None):
            with self.subTest(domains=domains):
                self.assert_refused("david@company.com.au", domains, why="allowed_domains is empty")

    def test_bad_list_entry_stops_with_a_clear_message(self):
        for entry in ("*.company.com.au", "company", "company.com.au/x", "me@"):
            with self.subTest(entry=entry):
                e = self.make("david@company.com.au")
                e.set_config(digest={"allowed_domains": [entry]})
                proc = e.workq("status", check=False)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("digest.allowed_domains", proc.stderr)

    def test_smtp_is_given_only_the_checked_addresses(self):
        server = TinySMTP()
        server.start()
        e = self.make("David <david@company.com.au>",
                      smtp={"host": "127.0.0.1", "port": server.port, "starttls": False})
        e.workq("digest")
        server.join(timeout=10)
        rcpt = [x for x in server.lines if x.upper().startswith("RCPT")]
        self.assertEqual([x.lower() for x in rcpt], ["rcpt to:<david@company.com.au>"])

    def test_smtp_is_not_contacted_for_a_refused_address(self):
        server = TinySMTP()
        server.start()
        e = self.make("me@gmail.com", smtp={"host": "127.0.0.1", "port": server.port, "starttls": False})
        proc = e.workq("digest", check=False)
        self.assertEqual(proc.returncode, 1)
        server.sock.close()
        self.assertEqual(server.commands, [])

    def test_doctor_reports_the_check(self):
        e = self.make("me@gmail.com")
        out = e.workq("doctor", check=False).stdout
        self.assertIn("me@gmail.com is not in an allowed domain", out)
        e2 = self.make("david@company.com.au", [])
        self.assertIn("allowed_domains is empty", e2.workq("doctor", check=False).stdout)
        e3 = self.make("david@company.com.au")
        self.assertIn("digest goes to david@company.com.au", e3.workq("doctor", check=False).stdout)

    def test_no_email_needs_no_list(self):
        e = self.make(None, [], send_command="/nonexistent/sendmail -t")
        proc = e.workq("digest", check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((e.clone() / "DIGEST.md").is_file())


if __name__ == "__main__":
    unittest.main()
