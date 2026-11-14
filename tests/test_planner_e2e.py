"""End-to-end: the real workq program, the fake Claude, and the stand-in Graph server."""

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fake_graph import FakeGraph  # noqa: E402
from fakes import make_docx, make_xlsx  # noqa: E402
from test_workq import PKG, WORKQ, Env, bwrap_ok, yaml  # noqa: E402,F401  (yaml: system or bundled)

ME = "11111111-2222-3333-4444-555555555555"
DAVID = "6463a5ce-2119-4198-9f2a-628761df4a62"
OTHER = "99999999-8888-7777-6666-555555555555"
SP = "https://corp.sharepoint.com/sites/fin/Shared%20Documents"
REPO = "https://dev.azure.test/org/proj/_git/workq-queue"


class PlannerE2E(unittest.TestCase):
    def make(self, sandbox="off", **overrides):
        self.graph = FakeGraph()
        self.addCleanup(self.graph.close)
        self.graph.add_plan("plan-1")
        planner = {
            "enabled": True,
            "plans": [{"id": "plan-1", "name": "Finance systems"}],
            "accept_from": [DAVID],
            "mention": DAVID,
            "links": {"file": REPO + "?path=/{path}&version=GC{commit}",
                      "folder": REPO + "?path=/{path}&version=GC{commit}",
                      "current": REPO + "?path=/{path}&version=GB{branch}"},
        }
        planner.update(overrides.pop("planner", {}))
        self.env = Env(graph={"tenant": "corp.test", "client_id": "client-1",
                              "authority": self.graph.base, "endpoint": self.graph.base},
                       planner=planner, **overrides)
        self.addCleanup(self.env.cleanup)
        self.env.set_config(claude={"sandbox": sandbox})
        return self.env

    def login(self):
        proc = subprocess.Popen([sys.executable, str(WORKQ), "graph", "login"], env=self.env.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        url = None
        while url is None:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError("graph login printed no address: " + proc.stderr.read())
            if line.strip().startswith("http"):
                url = line.strip()
        landing = self.graph.landing_address(url)
        out, err = proc.communicate(landing + "\n", timeout=60)
        self.assertEqual(proc.returncode, 0, out + err)
        return out

    def setup_board(self):
        self.login()
        out = self.env.workq("planner", "setup", "--yes").stdout
        self.assertIn("created workq: done", out)

    def task(self, title, description, creator=DAVID, assigned=(ME,), refs=(), checklist=()):
        return self.graph.add_task("plan-1", "For workq", title, creator, assigned, description,
                                   checklist, refs)

    def comments_text(self, tid):
        return [re.sub(r"<[^>]+>", "", b["content"]).strip() for b in self.graph.comments(tid)]

    def git(self, *args):
        return subprocess.run(["git", "--git-dir", str(self.env.remote), *args], capture_output=True,
                              text=True, check=True).stdout

    # ------------------------------------------------------------------ tests

    def test_a_task_goes_round_the_whole_loop(self):
        self.make()
        self.setup_board()
        self.assertEqual(sorted(self.graph.plans["plan-1"]["buckets"].values()),
                         sorted(["For workq", "workq: planning", "workq: working", "workq: for review",
                                 "workq: blocked", "workq: done"]))
        self.graph.add_file(SP + "/notes.docx", "notes.docx", make_docx(["Option C has 12 covered bays"]))
        self.graph.add_file(SP + "/costs.xlsx", "costs.xlsx", make_xlsx({"Costs": [["Option", "Monthly"], ["C", 130]]}))
        good = self.task("Pick a parking option", "fake-tier: haiku\nfake-exec: refs\nfake-check: pass",
                         refs=[(SP + "/notes.docx", "notes.docx"), (SP + "/costs.xlsx", "costs.xlsx"),
                               ("https://intranet.test/parking", "Parking policy")],
                         checklist=["Names one option"])
        unassigned = self.task("Not for workq", "fake-tier: haiku", assigned=())
        stranger = self.task("From someone else", "fake-tier: haiku", creator=OTHER)

        self.env.night()

        folder, path, meta = self.env.item("pick-a-parking-option")
        self.assertEqual(folder, "review")
        self.assertEqual(meta["planner"], {"plan_id": "plan-1", "task_id": good, "created_by": DAVID})
        self.assertEqual(self.graph.bucket_name(good), "workq: for review")
        said = self.comments_text(good)
        self.assertEqual([s.split(".")[0].split(" (")[0] for s in said],
                         ["Taken by workq as inbox/" + path.name, "Planned", "Executing", "Checking the result",
                          "Ready for review"])
        review = self.graph.comments(good)[-1]
        self.assertEqual(review["mentions"][0]["mentioned"], DAVID)
        # every pinned link names a commit that exists, and the act link is the branch
        commits = set(self.git("rev-list", "--all").split())
        pinned = re.findall(r"version=GC([0-9a-f]{40})", " ".join(b["content"] for b in self.graph.comments(good)))
        self.assertTrue(pinned and set(pinned) <= commits)
        self.assertIn(f"path=/review/{path.name}&amp;version=GBmain", review["content"])
        self.assertIn(f"path=/review/{path.name}/result.md&amp;version=GC", review["content"])
        # the brief, and what the executor saw in refs/
        brief = (path / "brief.md").read_text()
        self.assertIn("- Names one option", brief)
        self.assertIn("Parking policy: https://intranet.test/parking (a web page; not fetched)", brief)
        seen = (path / "out" / "refs-seen.txt").read_text()
        self.assertIn("notes.docx,notes.docx.md", seen)
        self.assertIn("costs.xlsx-1-Costs.csv", seen)
        self.assertIn("Option C has 12 covered bays", seen)
        if shutil.which("soffice") or shutil.which("libreoffice"):
            self.assertIn("notes.docx.pdf", seen)
        # refs/ never reaches git; refs.yaml does
        self.assertFalse((path / "refs").exists())
        refs_yaml = yaml.safe_load((path / "refs.yaml").read_text())
        self.assertEqual([r["status"] for r in refs_yaml], ["ok", "ok", "web"])
        history = self.git("log", "--all", "--name-only", "--format=")
        self.assertNotIn("/refs/", history)
        self.assertNotIn("planted.txt", history)
        blobs = self.git("log", "--all", "-p", "--format=")
        self.assertNotIn("PK\x03\x04", blobs)
        # the other two tasks
        self.assertEqual(self.comments_text(stranger), ["Not taken: only listed people can send work to workq."])
        self.assertEqual(self.graph.bucket_name(stranger), "For workq")
        self.assertEqual(self.graph.comments(unassigned), [])
        # a second night says nothing more to the stranger, and takes nothing twice
        self.env.night()
        self.assertEqual(len(self.graph.comments(stranger)), 1)
        self.assertEqual(len([1 for b in self.env.where()[0].values()]), 1)
        # the digest shows the Planner line and the task link
        digest = self.env.workq("digest", "--stdout").stdout
        self.assertIn("PLANNER", digest)
        self.assertIn("1 task taken since the last digest; 0 updates waiting", digest)
        self.assertIn(f"Planner task: https://tasks.office.com/corp.test/Home/Task/{good}", digest)

        # approve: archived, moved to done, never marked complete
        self.env.push_file(f"review/{path.name}/review.md", "verdict: approve\n\nGood.\n")
        self.env.night()
        self.assertEqual(self.env.item("pick-a-parking-option")[0].split("/")[0], "archive")
        self.assertEqual(self.graph.bucket_name(good), "workq: done")
        self.assertEqual(self.graph.tasks[good]["percentComplete"], 0)
        self.assertIn("Approved and archived.", self.comments_text(good)[-1])
        self.assertTrue((self.env.state / "attachments" / path.name / "archived").is_file())

    def test_questions_then_retry_fetches_attachments_again(self):
        self.make()
        self.setup_board()
        self.graph.add_file(SP + "/a.pdf", "a.pdf", b"%PDF-version-one")
        tid = self.task("Needs answers", "fake-shape: questions", refs=[(SP + "/a.pdf", "a.pdf")])
        self.env.night()
        folder, path, _meta = self.env.item("needs-answers")
        self.assertEqual(folder, "blocked")
        self.assertEqual(self.graph.bucket_name(tid), "workq: blocked")
        blocked = self.graph.comments(tid)[-1]
        self.assertRegex(re.sub(r"<[^>]+>", "", blocked["content"]), r"Blocked: \d+ questions?\.")
        self.assertEqual(blocked["mentions"][0]["mentioned"], DAVID)
        first_sha = yaml.safe_load((path / "refs.yaml").read_text())[0]["sha256"]
        self.graph.files[SP + "/a.pdf"]["content"] = b"%PDF-version-two"
        self.env.push_file(f"blocked/{path.name}/review.md", "verdict: retry\n\n1. Use 2026 figures.\n")
        self.env.workq("run", "file")
        self.env.workq("planner", "sync")
        _f, path2, _m = self.env.item("needs-answers")
        self.assertNotEqual(yaml.safe_load((path2 / "refs.yaml").read_text())[0]["sha256"], first_sha)
        self.assertIn("Back in the queue with your notes.", self.comments_text(tid)[-1])
        self.assertEqual(self.graph.bucket_name(tid), "workq: planning")

    def test_expired_sign_in_never_stops_the_queue(self):
        self.make()
        self.setup_board()
        self.task("Waiting in Planner", "fake-tier: haiku")
        self.graph.revoked = True
        self.graph.access_tokens.clear()
        self.env.add("Plain brief", "fake-tier: haiku", "fake-check: pass")
        proc = self.env.night()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.env.item("plain-brief")[0], "review")
        problems = " ".join(p["problem"] for p in self.env.problems())
        self.assertIn("workq graph login", problems)
        doctor = self.env.workq("doctor", check=False).stdout
        self.assertIn("Graph sign-in", doctor)
        # signing in again picks the task up
        self.graph.revoked = False
        self.login()
        self.env.night()
        self.assertEqual(self.env.item("waiting-in-planner")[0], "review")

    def test_comments_api_down_writes_to_the_description(self):
        self.make()
        self.setup_board()
        tid = self.task("Comments down", "fake-tier: haiku\nfake-check: pass")
        self.graph.message_status = 500
        self.env.night()
        desc = self.graph.details[tid]["description"]
        self.assertTrue(desc.startswith("fake-tier: haiku"))
        self.assertIn("--- workq ---", desc)
        self.assertIn("Ready for review.", desc)
        self.assertEqual(self.graph.bucket_name(tid), "workq: for review")

    def test_quiet_and_summaries_settings(self):
        self.make(planner={"comments": {"quiet": True, "summaries": True}})
        self.setup_board()
        tid = self.task("Quiet one", "fake-tier: haiku\nfake-check: pass")
        self.env.night()
        said = self.comments_text(tid)
        self.assertFalse(any(s.startswith(("Executing", "Checking")) for s in said))
        self.assertEqual(len(said), 3)

    def test_graph_mail_and_the_repo_fallback(self):
        self.make(digest={"to": "david@corp.test", "allowed_domains": ["corp.test"], "route": "graph"})
        self.login()
        self.env.workq("digest")
        self.assertEqual(self.graph.mail[-1]["message"]["toRecipients"],
                         [{"emailAddress": {"address": "david@corp.test"}}])
        self.graph.fail_next("POST", "/v1.0/me/sendMail", 500)
        proc = self.env.workq("digest", check=False)
        self.assertEqual(proc.returncode, 1)
        text = (self.env.clone() / "DIGEST.md").read_text()
        self.assertIn("Email failed", text)
        self.assertIn("## For review", text)

    def test_doctor_reports_the_graph_setup(self):
        self.make()
        out = self.env.workq("doctor", check=False).stdout
        self.assertIn("not signed in to Graph: run `workq graph login`", out)
        self.setup_board()
        out = self.env.workq("doctor", check=False).stdout
        self.assertIn("Graph is reachable", out)
        self.assertIn("signed in to Graph as svc-workq <svc-workq@corp.test>", out)
        self.assertIn("plan Finance systems is readable and has all six buckets", out)
        self.assertIn("Tasks.ReadWrite, Files.SelectedOperations.Selected", out)

    def test_config_is_checked(self):
        self.make()
        bad = [
            ({"planner": {"accept_from": []}}, "accept_from must list at least one person"),
            ({"planner": {"accept_from": ["david@corp.test"]}}, "Entra object IDs"),
            ({"planner": {"mention": "david"}}, "planner.mention"),
            ({"planner": {"links": {"file": REPO + "?path=/{path}", "folder": "x", "current": "y"}}},
             "planner.links.file must contain {commit}"),
            ({"planner": {"plans": []}}, "planner.plans must list"),
            ({"planner": {"plans": [{"id": "p", "buckets": {"done": "For workq"}}]}}, "must all be different"),
            ({"graph": {"tenant": None, "client_id": None}}, "planner.enabled needs a graph section"),
            ({"graph": {"endpoint": "http://graph.example.com"}}, "graph.endpoint must be an https"),
            ({"digest": {"route": "pigeon"}}, "digest.route must be"),
        ]
        good = yaml.safe_load(self.env.config.read_text())
        for change, message in bad:
            cfg = yaml.safe_load(self.env.config.read_text())
            for key, value in change.items():
                section = dict(cfg.get(key) or {})
                for k, v in value.items():
                    if isinstance(v, dict) and isinstance(section.get(k), dict):
                        section[k] = dict(section[k], **v)
                    else:
                        section[k] = v
                cfg[key] = section
            self.env.config.write_text(yaml.safe_dump(cfg))
            proc = self.env.workq("status", check=False)
            self.assertEqual(proc.returncode, 1, change)
            self.assertIn(message, proc.stderr, change)
            self.env.config.write_text(yaml.safe_dump(good))

    def test_tokens_never_reach_claude(self):
        if not bwrap_ok():
            self.skipTest("bubblewrap not usable here")
        self.make(sandbox="bwrap")
        self.setup_board()
        self.task("Sandboxed", "fake-tier: haiku\nfake-exec: refs\nfake-check: pass")
        self.env.night()
        _f, path, _m = self.env.item("sandboxed")
        self.assertIn("graph_token=hidden", (path / "out" / "refs-seen.txt").read_text())
        token = (self.env.state / "graph" / "token.json").read_text()
        refresh = re.search(r'"refresh_token": "([^"]+)"', token).group(1)
        for call in self.env.claude_calls():
            self.assertNotIn(refresh, str(call))
            self.assertFalse(any("graph" in k.lower() for k in call["env"]))

    def test_logout(self):
        self.make()
        self.login()
        self.assertTrue((self.env.state / "graph" / "token.json").is_file())
        self.env.workq("graph", "logout")
        self.assertFalse((self.env.state / "graph" / "token.json").exists())


if __name__ == "__main__":
    unittest.main()
