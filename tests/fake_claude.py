#!/usr/bin/env python3
"""A stand-in for `claude -p` used by the tests.

It reads the stage and item folder from the prompt on stdin, then behaves as
the item's brief.md tells it, through lines such as:

    fake-tier: haiku            tier the shaper picks (default sonnet)
    fake-shape: plan | questions | nothing | escape | touch-meta | stray | badtier
    fake-exec: result | replan-once | nothing | timeout | limit-once | bigout | escape
               | race | probe | probe-read | githook | gitswap
    fake-check: pass | fail,pass | fail,fail,fail ...   (one per check attempt)

Every call's arguments are appended to $FAKE_CLAUDE_LOG as one JSON line.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    if os.environ.get("WORKQ_BUNDLED_YAML"):
        raise ImportError
    import yaml
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import yaml

argv = sys.argv[1:]
if "--version" in argv:
    print("9.9.9 (fake Claude Code)")
    sys.exit(0)
if "--help" in argv:
    print("Options: --tools --allowedTools --permission-mode --max-budget-usd "
          "--no-session-persistence --append-system-prompt --settings "
          + os.environ.get("FAKE_HELP_FLAGS", "--restricted --strict-mcp-config"))
    sys.exit(0)
prompt = sys.stdin.read()
if "Reply with the single word OK" in prompt:
    print(json.dumps({"type": "result", "is_error": False, "result": "OK", "total_cost_usd": 0.001}))
    sys.exit(0)
stage = re.search(r"^Stage: (\w+)", prompt, re.M).group(1)
item_rel = re.search(r"^Work item folder: (.+)$", prompt, re.M).group(1).strip()
item = Path.cwd() / item_rel
brief = (item / "brief.md").read_text()
directives = dict(re.findall(r"^fake-([\w-]+):\s*(.+)$", brief, re.M))
meta = yaml.safe_load((item / "meta.yaml").read_text()) or {}

log_path = os.environ.get("FAKE_CLAUDE_LOG")
if log_path:
    try:
        with open(log_path, "a") as fh:
            fh.write(json.dumps({"stage": stage, "item": item.name, "argv": argv,
                                 "cwd": str(Path.cwd()), "prompt": prompt,
                                 "env": dict(os.environ)}) + "\n")
    except OSError:
        pass  # inside a sandbox the log's folder is hidden

state_dir = Path(os.environ.get("FAKE_CLAUDE_STATE", "/tmp"))


def once(key):
    marker = state_dir / f"{item.name}-{key}"
    if marker.exists():
        return False
    marker.write_text("x")
    return True


def ok(text="done", cost=0.01):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": text, "total_cost_usd": cost, "num_turns": 3}))
    sys.exit(0)


def limit():
    reset = int(time.time()) + 2
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": f"Claude AI usage limit reached|{reset}",
                      "total_cost_usd": 0, "num_turns": 1}))
    sys.exit(1)


if stage == "shape":
    mode = directives.get("shape", "plan")
    tier = directives.get("tier", "sonnet")
    if mode == "questions":
        (item / "questions.md").write_text("1. Which year should I use?\n2. In AUD or USD?\n")
        ok("asked questions")
    if mode == "nothing":
        ok("I could not decide")
    if mode == "escape":
        (Path.cwd() / "CLAUDE.md").write_text("hacked\n")
    if mode == "touch-meta":
        (item / "meta.yaml").write_text("id: hacked\n")
    if mode == "stray":
        (item / "scratch.md").write_text("thinking\n")
    if mode == "badtier":
        tier = "gpt"
    (item / "plan.md").write_text(
        f"---\ntier: {tier}\nreason: test\nmax_turns: 999\nbudget_usd: 99\n"
        "tools: [Read, Write, WebSearch, Bash]\n---\n## Goal\nTest.\n"
        "## Acceptance criteria\n1. result.md exists\n"
    )
    ok("planned")

if stage == "execute":
    mode = directives.get("exec", "result")
    if mode == "nothing":
        ok("gave up")
    if mode == "timeout":
        time.sleep(30)
        ok("too late")
    if mode == "limit-once" and once("limit"):
        limit()
    if mode == "replan-once" and once("replan"):
        (item / "result.md").write_text("---\nreplan: true\n---\nThe plan assumed data we do not have.\n")
        ok("replan")
    if mode == "bigout":
        (item / "out").mkdir(exist_ok=True)
        (item / "out" / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    if mode == "escape":
        (Path.cwd() / "inbox" / "evil.md").write_text("new brief planted by an agent\n")
    if mode in ("probe", "probe-read"):
        # Report what this process can see and change, for the sandbox tests.
        root = Path.cwd().parents[2]          # <root>/state/clones/<stage>
        lines = []
        for label, path in (("ssh_key", Path.home() / ".ssh" / "id_test"),
                            ("config", root / "config.yaml"),
                            ("ledger", root / "state" / "ledger.jsonl"),
                            ("tmp_secret", Path("/tmp") / root.name / "tmp-secret.txt")):
            lines.append(f"{label}={'visible' if path.exists() else 'hidden'}")
        writes = (("git_write", Path.cwd() / ".git" / "hooks" / "evil"),
                  ("etc_write", Path("/etc/workq-probe")),
                  ("home_write", Path.home() / "planted.txt"))
        for label, path in (writes if mode == "probe" else ()):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
                lines.append(f"{label}=allowed")
            except OSError:
                lines.append(f"{label}=blocked")
        lines.append("token=" + os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "none"))
        (item / "out").mkdir(exist_ok=True)
        (item / "out" / "probe.txt").write_text("\n".join(lines) + "\n")
    if mode == "githook":
        hook = Path.cwd() / ".git" / "hooks" / "post-commit"
        try:
            hook.parent.mkdir(exist_ok=True)
            hook.write_text(f"#!/bin/sh\ntouch {Path.cwd().parents[2]}/hook-ran\n")
            hook.chmod(0o755)
        except OSError:
            pass
    if mode == "gitswap":
        try:
            os.rename(".git", ".git-real")
            os.makedirs(".git")
            Path(".git/config").write_text("[core]\n\tfsmonitor = touch /tmp/pwned\n")
        except OSError:
            pass
    if mode == "race":
        # The owner pushes an edit while this run is going, so the runner's push is rejected.
        import subprocess
        import tempfile
        work = tempfile.mkdtemp()
        remote = os.environ["FAKE_REMOTE"]
        run = lambda *a: subprocess.run(a, cwd=work, check=True, capture_output=True)
        run("git", "clone", "-q", remote, "c")
        work = os.path.join(work, "c")
        Path(work, "inbox", "owner-added.md").write_text("# Added while the shift ran\n")
        run("git", "add", "-A")
        run("git", "-c", "user.name=owner", "-c", "user.email=o@x", "commit", "-qm", "owner edit")
        run("git", "push", "-q", "origin", "HEAD")
    n = meta.get("execute_attempts")
    (item / "out").mkdir(exist_ok=True)
    (item / "out" / "report.md").write_text(f"# Report\n\nAttempt {n} at {meta.get('tier')}.\n")
    (item / "result.md").write_text(
        f"---\nreplan: false\n---\nSupplier B is cheapest (attempt {n}).\n\n## What I did\nStuff.\n"
    )
    ok("executed", cost=0.05)

if stage == "check":
    seq = [s.strip() for s in directives.get("check", "pass").split(",")]
    n = int(meta.get("check_attempts") or 1)
    verdict = seq[min(n, len(seq)) - 1]
    if verdict == "nothing":
        ok("forgot to write")
    if verdict == "bad":
        (item / "check.md").write_text("---\nverdict: maybe\n---\n")
        ok("checked")
    (item / "check.md").write_text(
        f"---\nverdict: {verdict}\n---\n- [{verdict}] 1. result.md exists\n"
    )
    ok("checked")

print(json.dumps({"is_error": True, "result": f"unknown stage {stage}"}))
sys.exit(1)
