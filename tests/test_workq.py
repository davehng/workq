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
import json
import os
import shutil
import socket
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
    sys.path.insert(0, str(PKG / "lib"))
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
                       "send_command": str(sendmail)},
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

    def night(self, minutes=180):
        until = (dt.datetime.now() + dt.timedelta(minutes=minutes)).strftime("%H:%M")
        return self.workq("night", "--until", until)

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
                meta = yaml.safe_load(meta_file.read_text()) if meta_file.exists() else {}
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
            e.night()
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
        e.night()
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
        e.night()
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
        e.night()
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


if __name__ == "__main__":
    unittest.main()
