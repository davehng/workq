#!/usr/bin/env python3
"""End-to-end tests for workq, using a fake `claude` and a local bare git repo.

Run from the package folder:  python3 -m unittest discover -s tests -v
Needs: git, python3, PyYAML. Takes about a minute. Calls no model.
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
WORKQ = HERE.parent / "bin" / "workq"
FAKE = HERE / "fake_claude.py"


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
            "claude": {"bin": str(FAKE), "agent_user": None, "restricted": True},
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
        self.env = dict(os.environ, WORKQ_CONFIG=str(self.config), FAKE_CLAUDE_LOG=str(self.calls),
                        FAKE_CLAUDE_STATE=str(fake_state), FAKE_REMOTE=str(self.remote))
        self.workq("init")

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


if __name__ == "__main__":
    unittest.main()
