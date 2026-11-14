"""Tests for workq 1.0: Graph sign-in and client, Planner, attachments, converters and mail.

Unit tests use the fakes in fakes.py. Contract tests run one set of checks against both
the fake board and the Graph board (on the stand-in server in fake_graph.py). The
end-to-end tests run the real workq program against the stand-in server and the fake
Claude."""

import datetime as dt
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from fakes import (FakeAttachmentSource, FakeLinks, FakeMailer, FakeQueue,  # noqa: E402
                   FakeTaskBoard, make_docx, make_pptx, make_xlsx)
from fake_graph import FakeGraph  # noqa: E402
from workq.attachments import AttachmentKeeper, remove_refs, share_id  # noqa: E402
from workq.convert import (ConvertError, ExcelToCsv, LibreOfficePdf, PowerPointToText,  # noqa: E402
                           WordToMarkdown)
from workq.graph import (GraphClient, GraphError, Http, RefreshTokenSource, TokenStore,  # noqa: E402
                         check_base_url, scopes_for)
from workq.interfaces import BoardError, MailError, Reference, Task, TaskDetails  # noqa: E402
from workq.mail import AllowedDomainsMailer, GraphMailer  # noqa: E402
from workq.planner import (DEFAULT_BUCKETS, CommentBook, GraphTaskBoard, Intake, Outbox,  # noqa: E402
                           PlannerSync, RefusedLog, TemplateLinkBuilder, plain_reason, render_brief,
                           with_status_note)

ME = "11111111-2222-3333-4444-555555555555"
DAVID = "6463a5ce-2119-4198-9f2a-628761df4a62"
OTHER = "99999999-8888-7777-6666-555555555555"
SP = "https://corp.sharepoint.com/sites/fin/Shared%20Documents"
NAMES = list(DEFAULT_BUCKETS.values())
PLAN = {"id": "plan-1", "name": "Finance systems", "buckets": dict(DEFAULT_BUCKETS)}


def tmpdir(test):
    path = Path(tempfile.mkdtemp(prefix="workq-planner-"))
    test.addCleanup(shutil.rmtree, path, True)
    return path


# ================================================================ comments


class CommentBookTests(unittest.TestCase):
    def book(self, **kw):
        return CommentBook(FakeLinks(), mention=DAVID, **kw)

    def test_review_links_are_pinned_and_the_act_link_is_current(self):
        bucket, page, text, mention = self.book().build(
            "review", "review/2026-11-14-x", "abc123", {"summary": "SECRET FIGURES", "has_out": True})
        self.assertEqual(bucket, "review")
        self.assertIn('href="https://repo.test/file/review/2026-11-14-x/result.md@abc123"', page)
        self.assertIn('href="https://repo.test/file/review/2026-11-14-x/check.md@abc123"', page)
        self.assertIn('href="https://repo.test/folder/review/2026-11-14-x/out/@abc123"', page)
        self.assertIn('href="https://repo.test/now/review/2026-11-14-x"', page)
        self.assertEqual(mention, DAVID)
        self.assertNotIn("SECRET", page + text)          # summaries are off by default

    def test_summaries_on_add_the_first_line(self):
        _b, page, _t, _m = self.book(summaries=True).build(
            "review", "review/x", "c1", {"summary": "Option C at $18,720 a year"})
        self.assertIn("Option C at $18,720 a year", page)

    def test_agent_text_is_escaped_and_cannot_add_links(self):
        hostile = '<a href="https://evil.test">click</a> <script>x</script>'
        _b, page, _t, _m = self.book(summaries=True).build("review", "review/x", "c1", {"summary": hostile})
        self.assertNotIn('href="https://evil.test"', page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_blocked_reason_without_summaries_drops_agent_text(self):
        reason = "shape failed 3 times; last: no plan.md (the agent said: SECRET)"
        _b, page, text, mention = self.book().build("blocked", "blocked/x", "c1", {"reason": reason})
        self.assertIn("Blocked: shape failed 3 times.", text)
        self.assertNotIn("SECRET", page)
        self.assertEqual(mention, DAVID)
        self.assertEqual(plain_reason("check failed at opus"), "check failed at opus")

    def test_questions_count_and_link(self):
        bucket, page, text, mention = self.book().build(
            "questions", "blocked/x", "c1", {"questions": 3, "question_lines": ["Which year?"]})
        self.assertEqual(bucket, "blocked")
        self.assertIn("Blocked: 3 questions.", text)
        self.assertNotIn("Which year?", page)
        self.assertIn("verdict: retry", text)
        self.assertIn("questions.md@c1", page)

    def test_executing_and_checking_post_by_default_and_quiet_skips_them(self):
        built = self.book().build("executing", "ready/sonnet/x", "c1", {"tier": "sonnet", "attempt": 1,
                                                                        "max_attempts": 3})
        self.assertEqual(built[0], None)
        self.assertIn("Executing (sonnet, run 1 of 3).", built[2])
        self.assertIsNone(self.book(quiet=True).build("executing", "ready/sonnet/x", "c1", {}))
        self.assertIsNone(self.book(quiet=True).build("checking", "check/x", "c1", {}))
        # quiet never stops a move
        self.assertEqual(self.book(quiet=True).build("planned", "ready/sonnet/x", "c1", {"tier": "sonnet"})[0],
                         "working")

    def test_each_event_moves_to_the_right_bucket(self):
        want = {"taken": "planning", "planned": "working", "check_failed": "working", "review": "review",
                "replan": "planning", "sent_back": "planning", "archived": "done", "blocked": "blocked",
                "questions": "blocked", "refused": None, "checking": None}
        for event, bucket in want.items():
            built = self.book().build(event, "x/y", "c", {"verdict": "approve"})
            self.assertEqual(built[0], bucket, event)
        self.assertIn("Approved and archived.", self.book().build("archived", "archive/2026-11/y", "c",
                                                                 {"verdict": "approve"})[2])
        self.assertIn("Rejected and archived.", self.book().build("archived", "archive/2026-11/y", "c",
                                                                 {"verdict": "reject"})[2])

    def test_comments_off_still_moves(self):
        bucket, page, _t, _m = self.book(enabled=False).build("review", "review/x", "c1", {})
        self.assertEqual((bucket, page), ("review", ""))

    def test_template_links(self):
        links = TemplateLinkBuilder({
            "file": "https://dev.azure.com/o/p/_git/q?path=/{path}&version=GC{commit}",
            "folder": "https://dev.azure.com/o/p/_git/q?path=/{path}&version=GC{commit}",
            "current": "https://dev.azure.com/o/p/_git/q?path=/{path}&version=GB{branch}"}, "main")
        self.assertEqual(links.file_link("review/a b/result.md", "abc"),
                         "https://dev.azure.com/o/p/_git/q?path=/review/a%20b/result.md&version=GCabc")
        self.assertEqual(links.current_link("review/x"), "https://dev.azure.com/o/p/_git/q?path=/review/x&version=GBmain")

    def test_status_note_keeps_the_description_and_the_last_ten_lines(self):
        text = "The ask.\n\nMore detail."
        for n in range(12):
            text = with_status_note(text, f"note {n}")
        own, _, notes = text.partition("--- workq ---")
        self.assertEqual(own.strip(), "The ask.\n\nMore detail.")
        self.assertEqual(notes.strip().splitlines(), [f"note {n}" for n in range(2, 12)])


# ================================================================ outbox and sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.board = FakeTaskBoard()
        self.board.add_plan("plan-1", NAMES)
        self.board.tasks["t1"] = Task("t1", "plan-1", "Task one", "b0", DAVID, [ME])
        self.outbox = Outbox(self.dir / "outbox.jsonl")
        self.problems = []

    def sync(self, **kw):
        return PlannerSync(self.board, self.outbox, [PLAN], self.problems.append, **kw).run()

    def add(self, kind, bucket=None, html="<p>x</p>", task="t1", **kw):
        return self.outbox.add(kind=kind, task=task, plan="plan-1", item="inbox/x", bucket=bucket,
                               html=html, text="x", mention=None, **kw)

    def test_sends_every_comment_and_only_the_newest_move(self):
        self.add("taken", "planning", "<p>1</p>")
        self.add("planned", "working", "<p>2</p>")
        self.add("executing", None, "<p>3</p>")
        result = self.sync()
        self.assertEqual(result["sent"], 3)
        self.assertEqual(self.board.moves, [("t1", "workq: working")])
        self.assertEqual([c[1] for c in self.board.comments], ["<p>1</p>", "<p>2</p>", "<p>3</p>"])
        self.assertEqual(self.outbox.read(), [])

    def test_sign_in_failure_stops_and_keeps_everything(self):
        self.add("taken", "planning")
        self.add("planned", "working")
        self.board.raise_next["comment"] = [BoardError("signin", "expired")]
        result = self.sync()
        self.assertEqual(result["stopped"], "signin")
        self.assertEqual(len(self.outbox.read()), 2)
        self.assertTrue(any("stopped" in p for p in self.problems))
        self.assertEqual(self.sync()["sent"], 2)        # and next time it goes

    def test_throttling_and_network_keep_the_line(self):
        for kind in ("throttled", "network"):
            self.add("taken", "planning")
            self.board.raise_next["comment"] = [BoardError(kind, kind)]
            self.assertEqual(self.sync()["stopped"], kind)
            self.assertEqual(len(self.outbox.read()), 1)
            self.sync()
            self.assertEqual(self.outbox.read(), [])

    def test_gone_task_drops_its_line_and_carries_on(self):
        self.add("taken", "planning", task="missing")
        self.add("taken", "planning")
        result = self.sync()
        self.assertEqual((result["sent"], result["dropped"]), (1, 1))
        self.assertTrue(any("dropped" in p for p in self.problems))

    def test_conflict_is_kept_for_next_time(self):
        self.add("taken", "planning")
        self.board.raise_next["move"] = [BoardError("conflict", "etag")]
        self.assertEqual(self.sync()["kept"], 1)
        self.assertEqual(self.sync()["sent"], 1)

    def test_comment_api_failure_falls_back_to_the_description(self):
        self.board.task_details["t1"] = TaskDetails(description="Original ask")
        self.board.refuse_comments = "other"
        self.outbox.add(kind="review", task="t1", plan="plan-1", item="review/x", bucket="review",
                        html="<p>Ready</p>", text="Ready for review. result.md (https://repo.test/r)", mention=None)
        self.assertEqual(self.sync()["sent"], 1)
        desc = self.board.task_details["t1"].description
        self.assertTrue(desc.startswith("Original ask"))
        self.assertIn("Ready for review. result.md (https://repo.test/r)", desc)
        self.assertTrue(any("description instead" in p for p in self.problems))

    def test_old_lines_are_dropped(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat(timespec="seconds")
        self.add("taken", "planning", at=old)
        self.assertEqual(self.sync()["dropped"], 1)
        self.assertEqual(self.board.comments, [])

    def test_missing_bucket_still_sends_the_comment(self):
        self.board.plans["plan-1"] = {"b0": "For workq"}
        self.add("taken", "planning")
        self.assertEqual(self.sync()["sent"], 1)
        self.assertEqual(len(self.board.comments), 1)
        self.assertTrue(any("planner setup" in p for p in self.problems))

    def test_outbox_survives_bad_lines(self):
        self.add("taken", "planning")
        with open(self.outbox.path, "a") as fh:
            fh.write("not json\n")
        self.assertEqual(len(self.outbox.read()), 1)
        self.assertEqual(stat.S_IMODE(os.stat(self.outbox.path).st_mode), 0o600)


# ================================================================ intake


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.board = FakeTaskBoard()
        self.board.add_plan("plan-1", NAMES)
        self.queue = FakeQueue()
        self.outbox = Outbox(self.dir / "outbox.jsonl")
        self.refused = RefusedLog(self.dir / "refused.txt")
        self.problems = []

    def task(self, tid, creator=DAVID, assigned=(ME,), bucket="For workq", percent=0, title=None):
        self.board.tasks[tid] = Task(tid, "plan-1", title or f"Task {tid}", self.board.bucket_id("plan-1", bucket),
                                     creator, list(assigned), percent)
        self.board.task_details[tid] = TaskDetails(description=f"Do {tid}.", checklist=["One", "Two"])

    def intake(self, attachments=None):
        return Intake(self.board, self.queue, [PLAN], [DAVID], ME, self.outbox,
                      CommentBook(FakeLinks()), self.refused, self.problems.append, log=lambda *_: None,
                      attachments=attachments)

    def test_takes_only_assigned_tasks_in_the_intake_bucket_from_listed_people(self):
        self.task("good")
        self.task("unassigned", assigned=())
        self.task("other-bucket", bucket="workq: working")
        self.task("done", percent=100)
        self.task("stranger", creator=OTHER)
        self.assertEqual(self.intake().run(), 1)
        self.assertEqual([a[2]["task_id"] for a in self.queue.added], ["good"])
        kinds = [(e["kind"], e["task"]) for e in self.outbox.read()]
        self.assertEqual(kinds, [("taken", "good"), ("refused", "stranger")])
        # nothing said to the unassigned task
        self.assertNotIn("unassigned", [e["task"] for e in self.outbox.read()])

    def test_refusal_is_posted_once(self):
        self.task("stranger", creator=OTHER)
        self.intake().run()
        self.intake().run()
        self.assertEqual([e["kind"] for e in self.outbox.read()], ["refused"])

    def test_never_takes_a_task_twice(self):
        self.task("good")
        self.intake().run()
        self.assertEqual(self.intake().run(), 0)
        self.queue = FakeQueue(known={"good"})
        self.assertEqual(self.intake().run(), 0)

    def test_creator_and_assignee_ids_match_regardless_of_case(self):
        self.task("good", creator=DAVID.upper(), assigned=(ME.upper(),))
        self.assertEqual(self.intake().run(), 1)

    def test_missing_intake_bucket_is_a_problem_not_a_crash(self):
        self.board.plans["plan-1"] = {"b9": "Something else"}
        self.assertEqual(self.intake().run(), 0)
        self.assertTrue(any("planner setup" in p for p in self.problems))

    def test_no_account_id_stops(self):
        with self.assertRaises(BoardError) as ctx:
            Intake(self.board, self.queue, [PLAN], [DAVID], "", self.outbox, CommentBook(FakeLinks()),
                   self.refused, self.problems.append).run()
        self.assertEqual(ctx.exception.kind, "signin")

    def test_brief_has_the_ask_checklist_due_date_and_sources(self):
        task = Task("t", "plan-1", "Pick a parking option", "b0", DAVID, [ME], due="2026-11-30T00:00:00Z")
        details = TaskDetails(description="Use the notes only.", checklist=["Names one option", "Under 200 words"])
        entries = [{"name": "notes.docx", "source": SP + "/notes.docx", "status": "ok",
                    "copies": ["notes.docx", "notes.docx.md", "notes.docx.pdf"]},
                   {"name": "Intranet", "source": "https://intranet.test/x", "status": "web"},
                   {"name": "secret.xlsx", "source": SP + "/secret.xlsx", "status": "failed",
                    "reason": "outside workq's access"}]
        brief = render_brief(task, details, "Finance systems", entries)
        for want in ("# Pick a parking option", "## Ask", "Use the notes only.", "- Names one option",
                     "## Wanted by", "2026-11-30", "refs/notes.docx; also refs/notes.docx.md, refs/notes.docx.pdf",
                     "a web page; not fetched", "could not open (outside workq's access)", "Finance systems"):
            self.assertIn(want, brief)

    def test_taken_comment_lists_files_that_could_not_be_opened(self):
        self.task("good")
        self.board.task_details["good"].references = [Reference(SP + "/a.docx", "a.docx")]
        source = FakeAttachmentSource()
        keeper = AttachmentKeeper(source, [], self.dir / "attachments")
        self.intake(attachments=keeper).run()
        taken = self.outbox.read()[0]
        self.assertIn("Could not open: a.docx.", taken["text"])


# ================================================================ attachments and converters


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_word_to_markdown(self):
        path = self.write("a.docx", make_docx(["First para", "Second | para"], table=[["Year", "Cost"], ["2026", "95"]],
                                              heading="Background"))
        out = WordToMarkdown().convert(path, self.dir)[0].read_text()
        self.assertIn("## Background", out)
        self.assertIn("First para", out)
        self.assertIn("| Year | Cost |", out)
        self.assertIn("| 2026 | 95 |", out)

    def test_excel_to_csv_one_file_per_sheet_values_not_formulas(self):
        path = self.write("b.xlsx", make_xlsx({"Costs": [["Option", "Monthly"], ["A", 95], ["B", 180]],
                                               "Notes": [["x"]]}))
        made = ExcelToCsv().convert(path, self.dir)
        self.assertEqual([m.name for m in made], ["b.xlsx-1-Costs.csv", "b.xlsx-2-Notes.csv"])
        self.assertEqual(made[0].read_text().splitlines(), ["Option,Monthly", "A,95", "B,180"])

    def test_powerpoint_text_in_slide_order(self):
        path = self.write("c.pptx", make_pptx([["Title slide"], ["Second", "point"], ["Third"]]))
        out = PowerPointToText().convert(path, self.dir)[0].read_text()
        self.assertLess(out.index("Title slide"), out.index("Second"))
        self.assertLess(out.index("Second"), out.index("Third"))
        self.assertIn("## Slide 3", out)

    def test_not_a_zip_is_refused(self):
        path = self.write("enc.docx", b"\xd0\xcf\x11\xe0 encrypted container")
        with self.assertRaises(ConvertError):
            WordToMarkdown().convert(path, self.dir)

    def test_dtd_is_refused(self):
        bomb = make_docx(["x"])
        with zipfile.ZipFile(self.dir / "bomb.docx", "w") as z:
            with zipfile.ZipFile(__import__("io").BytesIO(bomb)) as src:
                for name in src.namelist():
                    data = src.read(name)
                    if name == "word/document.xml":
                        data = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + data.split(b"?>", 1)[1]
                    z.writestr(name, data)
        with self.assertRaises(ConvertError):
            WordToMarkdown().convert(self.dir / "bomb.docx", self.dir)

    @unittest.skipUnless(shutil.which("soffice") or shutil.which("libreoffice"), "LibreOffice not installed")
    def test_libreoffice_makes_a_pdf(self):
        path = self.write("d.docx", make_docx(["Hello from a test"]))
        made = LibreOfficePdf().convert(path, self.dir)
        self.assertTrue(made[0].read_bytes().startswith(b"%PDF"))

    def test_libreoffice_missing_is_reported(self):
        conv = LibreOfficePdf(soffice="")
        conv.soffice = None
        self.assertFalse(conv.accepts("x.docx"))
        with self.assertRaises(ConvertError):
            conv.convert(self.dir / "x.docx", self.dir)


class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.source = FakeAttachmentSource()
        self.keeper = AttachmentKeeper(self.source, [WordToMarkdown(), ExcelToCsv()], self.dir / "att",
                                       max_file_mb=1, max_item_mb=1.5)

    def test_fetches_converts_and_lists_what_it_could_not_use(self):
        self.source.files[SP + "/notes.docx"] = ("notes.docx", make_docx(["Parking notes"]))
        self.source.files[SP + "/big.pdf"] = ("big.pdf", b"%PDF" + b"0" * (2 * 1024 * 1024))
        refs = [Reference(SP + "/notes.docx", "notes.docx"), Reference("https://intranet.test/page", "Intranet"),
                Reference(SP + "/run.exe", "run.exe"), Reference(SP + "/big.pdf", "big.pdf"),
                Reference(SP + "/secret.xlsx", "secret.xlsx")]
        entries = {e["name"]: e for e in self.keeper.collect("2026-11-14-x", refs)}
        self.assertEqual(entries["notes.docx"]["status"], "ok")
        self.assertEqual(entries["notes.docx"]["copies"], ["notes.docx", "notes.docx.md"])
        self.assertEqual(entries["Intranet"]["status"], "web")
        self.assertEqual(entries["run.exe"]["status"], "skipped")
        self.assertEqual(entries["big.pdf"]["status"], "failed")
        self.assertIn("MB", entries["big.pdf"]["reason"])
        self.assertEqual(entries["secret.xlsx"]["status"], "failed")
        self.assertNotIn(SP + "/run.exe", self.source.fetched)     # never fetched
        folder = self.dir / "att" / "2026-11-14-x" / "files"
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["notes.docx", "notes.docx.md"])
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "att").st_mode), 0o700)

    def test_encrypted_office_file_is_reported_not_kept(self):
        self.source.files[SP + "/enc.docx"] = ("enc.docx", b"\xd0\xcf\x11\xe0 not a zip")
        entry = self.keeper.collect("i", [Reference(SP + "/enc.docx", "enc.docx")])[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("encrypted or damaged", entry["reason"])
        self.assertEqual(list((self.dir / "att" / "i" / "files").iterdir()), [])

    def test_item_total_cap(self):
        for n in range(3):
            self.source.files[f"{SP}/f{n}.pdf"] = (f"f{n}.pdf", b"%PDF" + b"0" * (700 * 1024))
        entries = self.keeper.collect("i", [Reference(f"{SP}/f{n}.pdf", f"f{n}.pdf") for n in range(3)])
        self.assertEqual([e["status"] for e in entries], ["ok", "ok", "failed"])

    def test_place_and_remove_refs(self):
        self.source.files[SP + "/a.pdf"] = ("a.pdf", b"%PDF-1")
        self.keeper.collect("i", [Reference(SP + "/a.pdf", "a.pdf")])
        item = self.dir / "item"
        item.mkdir()
        self.assertTrue(self.keeper.place("i", item))
        self.assertEqual((item / "refs" / "a.pdf").read_bytes(), b"%PDF-1")
        (item / "refs" / "planted").write_text("x")
        os.chmod(item / "refs", 0o500)
        remove_refs(item)
        self.assertFalse((item / "refs").exists())
        self.assertFalse(self.keeper.place("nothing-here", item))

    def test_cleanup_after_keep_days(self):
        self.source.files[SP + "/a.pdf"] = ("a.pdf", b"%PDF-1")
        self.keeper.collect("old", [Reference(SP + "/a.pdf", "a.pdf")])
        self.keeper.collect("live", [Reference(SP + "/a.pdf", "a.pdf")])
        self.keeper.mark_archived("old")
        self.assertEqual(self.keeper.cleanup(dt.date.today() + dt.timedelta(days=29)), [])
        self.assertEqual(self.keeper.cleanup(dt.date.today() + dt.timedelta(days=30)), ["old"])
        self.assertTrue((self.dir / "att" / "live").is_dir())

    def test_share_id(self):
        self.assertEqual(share_id("https://onedrive.live.com/redir?resid=1231244193912!12&authKey=1201919!12921!1"),
                         "u!aHR0cHM6Ly9vbmVkcml2ZS5saXZlLmNvbS9yZWRpcj9yZXNpZD0xMjMxMjQ0MTkzOTEyITEyJmF1dGhLZXk9MTIwMTkxOSExMjkyMSEx")


# ================================================================ mail


class MailTests(unittest.TestCase):
    def test_domain_check_wraps_every_route(self):
        inner = FakeMailer()
        mailer = AllowedDomainsMailer(inner, ["corp.test"])
        mailer.send("David <David@CORP.test>, ops@corp.test", "s", "b")
        self.assertEqual(inner.sent[0][0], ["David@corp.test", "ops@corp.test"])
        with self.assertRaises(MailError) as ctx:
            mailer.send("me@gmail.com", "s", "b")
        self.assertIn("not in an allowed domain", str(ctx.exception))
        self.assertEqual(len(inner.sent), 1)


# ================================================================ Graph: sign-in and client


class GraphTestCase(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()
        self.addCleanup(self.graph.close)
        self.dir = tmpdir(self)
        self.store = TokenStore(self.dir / "graph" / "token.json")
        self.http = Http()
        self.http.opener = __import__("urllib.request").request.build_opener(
            __import__("urllib.request").request.ProxyHandler({}), __import__("workq.graph").graph._NoRedirect())
        self.tokens = RefreshTokenSource(self.store, "corp.test", "client-1",
                                         scopes_for(planner=True, attachments=True, mail=True),
                                         self.http, authority=self.graph.base)
        self.waits = []
        self.client = GraphClient(self.tokens, self.http, endpoint=self.graph.base, sleep=self.waits.append)

    def sign_in(self):
        return self.graph.sign_in(self.tokens)


class SignInTests(GraphTestCase):
    def test_browser_sign_in_saves_a_private_token_and_the_account(self):
        account = self.sign_in()
        self.assertEqual(account["oid"], ME)
        self.assertEqual(account["upn"], "svc-workq@corp.test")
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path.parent).st_mode), 0o700)
        saved = self.store.load()
        self.assertTrue(saved["refresh_token"].startswith("rt-"))
        self.assertIn("https://graph.microsoft.com/Mail.Send", saved["scopes"])

    def test_wrong_state_or_error_in_the_pasted_address(self):
        pending = self.tokens.start_login()
        with self.assertRaises(GraphError):
            self.tokens.finish_login("http://localhost/?code=abc&state=wrong", pending)
        with self.assertRaises(GraphError) as ctx:
            self.tokens.finish_login("http://localhost/?error=access_denied&error_description=User+said+no", pending)
        self.assertEqual(ctx.exception.kind, "signin")
        with self.assertRaises(GraphError):
            self.tokens.finish_login("not an address", pending)

    def test_wrong_verifier_is_refused(self):
        pending = self.tokens.start_login()
        landing = self.graph.landing_address(pending["url"])
        pending["verifier"] = "something else"
        with self.assertRaises(GraphError):
            self.tokens.finish_login(landing, pending)

    def test_refresh_rotates_the_saved_refresh_token(self):
        self.sign_in()
        first = self.store.load()["refresh_token"]
        self.tokens.invalidate()
        self.tokens.access_token()
        self.assertNotEqual(self.store.load()["refresh_token"], first)

    def test_revoked_refresh_token_is_a_sign_in_error(self):
        self.sign_in()
        self.graph.revoked = True
        self.tokens.invalidate()
        with self.assertRaises(GraphError) as ctx:
            self.tokens.access_token()
        self.assertEqual(ctx.exception.kind, "signin")
        self.assertIn("workq graph login", str(ctx.exception))

    def test_not_signed_in(self):
        with self.assertRaises(GraphError) as ctx:
            self.tokens.access_token()
        self.assertEqual(ctx.exception.kind, "signin")

    def test_base_urls_must_be_https_or_local(self):
        self.assertEqual(check_base_url("https://graph.microsoft.com/", "x"), "https://graph.microsoft.com")
        self.assertEqual(check_base_url("http://127.0.0.1:9", "x"), "http://127.0.0.1:9")
        with self.assertRaises(ValueError):
            check_base_url("http://graph.example.com", "x")

    def test_scopes_follow_what_is_switched_on(self):
        self.assertEqual(scopes_for(), ["offline_access", "openid", "profile"])
        self.assertNotIn("https://graph.microsoft.com/Files.SelectedOperations.Selected",
                         scopes_for(planner=True, attachments=False))


class ClientTests(GraphTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.graph.add_plan("plan-1", NAMES)

    def test_throttling_waits_for_retry_after_then_succeeds(self):
        self.graph.fail_next("GET", "/v1.0/planner/plans", 429, {"Retry-After": "7"})
        self.graph.fail_next("GET", "/v1.0/planner/plans", 503, {"Retry-After": "500"})
        self.client.call("GET", "/planner/plans/plan-1/buckets")
        self.assertEqual(self.waits, [7, 120])                     # never more than 2 minutes

    def test_three_throttles_in_a_row_give_up(self):
        for _ in range(3):
            self.graph.fail_next("GET", "/v1.0/planner", 429, {"Retry-After": "0"})
        with self.assertRaises(GraphError) as ctx:
            self.client.call("GET", "/planner/plans/plan-1/buckets")
        self.assertEqual(ctx.exception.kind, "throttled")

    def test_401_gets_one_fresh_token(self):
        self.graph.fail_next("GET", "/v1.0/planner", 401)
        self.client.call("GET", "/planner/plans/plan-1/buckets")
        self.graph.access_tokens.clear()
        self.graph.revoked = True
        with self.assertRaises(GraphError) as ctx:
            self.client.call("GET", "/planner/plans/plan-1/buckets")
        self.assertEqual(ctx.exception.kind, "signin")

    def test_status_kinds(self):
        for status, kind in ((403, "denied"), (404, "gone"), (412, "conflict"), (400, "invalid"), (500, "other")):
            self.graph.fail_next("GET", "/v1.0/planner", status)
            with self.assertRaises(GraphError) as ctx:
                self.client.call("GET", "/planner/plans/plan-1/buckets")
            self.assertEqual(ctx.exception.kind, kind, status)

    def test_network_error(self):
        client = GraphClient(self.tokens, self.http, endpoint="http://127.0.0.1:9")
        with self.assertRaises(GraphError) as ctx:
            client.call("GET", "/planner/plans/plan-1/buckets")
        self.assertEqual(ctx.exception.kind, "network")

    def test_token_is_never_sent_off_graph(self):
        with self.assertRaises(GraphError):
            self.client.call("GET", "https://evil.test/v1.0/steal")
        before = len(self.graph.downloads)
        self.graph.add_file(SP + "/a.pdf", "a.pdf", b"%PDF")
        self.client.download(f"{self.graph.base}/download/{__import__('urllib.parse').parse.quote(SP + '/a.pdf', safe='')}", 100)
        self.assertEqual(self.graph.downloads[before:], [(SP + "/a.pdf", False)])

    def test_call_log(self):
        from workq.graph import CallLog
        log = CallLog(self.dir / "calls.log")
        client = GraphClient(self.tokens, self.http, endpoint=self.graph.base, log=log)
        client.call("GET", "/planner/plans/plan-1/buckets")
        line = (self.dir / "calls.log").read_text().strip()
        self.assertTrue(line.endswith("GET /v1.0/planner/plans/plan-1/buckets 200"), line)

    def test_graph_mailer(self):
        GraphMailer(self.client).send(["david@corp.test"], "workq digest", "body text")
        sent = self.graph.mail[0]
        self.assertEqual(sent["message"]["toRecipients"], [{"emailAddress": {"address": "david@corp.test"}}])
        self.assertTrue(sent["saveToSentItems"])
        self.graph.fail_next("POST", "/v1.0/me/sendMail", 403)
        with self.assertRaises(MailError) as ctx:
            GraphMailer(self.client).send(["david@corp.test"], "s", "b")
        self.assertEqual(ctx.exception.kind, "denied")


# ================================================================ contract: fake board and Graph board


class BoardContract:
    """The same checks for every TaskBoard. Subclasses provide self.board and the setup helpers."""

    def test_buckets_and_create(self):
        names = {b.name for b in self.board.buckets("plan-1")}
        self.assertEqual(names, set(NAMES))
        made = self.board.create_bucket("plan-1", "Extra")
        self.assertIn(("Extra", made.id), {(b.name, b.id) for b in self.board.buckets("plan-1")})

    def test_intake_tasks_and_details(self):
        a = self.make_task("Alpha", "For workq", DAVID, [ME], "Do alpha.", ["One", "Two"],
                           [(SP + "/notes.docx", "notes.docx")])
        self.make_task("Beta", "workq: working", DAVID, [ME])
        self.make_task("Gamma", "For workq", OTHER, [])
        intake = self.bucket("For workq")
        found = {t.title: t for t in self.board.intake_tasks("plan-1", intake)}
        self.assertEqual(set(found), {"Alpha", "Gamma"})
        self.assertEqual(found["Alpha"].created_by, DAVID)
        self.assertEqual(found["Alpha"].assignees, [ME])
        self.assertEqual(found["Gamma"].assignees, [])
        d = self.board.details(a)
        self.assertEqual(d.description, "Do alpha.")
        self.assertEqual(d.checklist, ["One", "Two"])
        self.assertEqual([(r.url, r.alias) for r in d.references], [(SP + "/notes.docx", "notes.docx")])

    def test_move(self):
        t = self.make_task("Alpha", "For workq", DAVID, [ME])
        self.board.move(t, self.bucket("workq: planning"))
        self.board.move(t, self.bucket("workq: planning"))        # already there: fine
        self.assertEqual(self.where(t), "workq: planning")
        with self.assertRaises(BoardError) as ctx:
            self.board.move("t-missing", self.bucket("workq: done"))
        self.assertEqual(ctx.exception.kind, "gone")

    def test_comment_and_mention(self):
        t = self.make_task("Alpha", "For workq", DAVID, [ME])
        self.board.comment(t, "<p>Hello</p>")
        self.board.comment(t, "<p>Ready</p>", mention=DAVID)
        got = self.comments(t)
        self.assertEqual(got[0], ("<p>Hello</p>", None))
        self.assertIn("<p>Ready</p>", got[1][0])
        self.assertEqual(got[1][1], DAVID)

    def test_mention_refused_still_posts(self):
        t = self.make_task("Alpha", "For workq", DAVID, [ME])
        self.refuse_mentions()
        self.board.comment(t, "<p>Ready</p>", mention=DAVID)
        got = self.comments(t)
        self.assertEqual(len(got), 1)
        self.assertIn("<p>Ready</p>", got[0][0])
        self.assertIsNone(got[0][1])

    def test_status_note_keeps_the_description(self):
        t = self.make_task("Alpha", "For workq", DAVID, [ME], "The ask.")
        self.board.set_status_note(t, "first")
        self.board.set_status_note(t, "second")
        desc = self.board.details(t).description
        self.assertTrue(desc.startswith("The ask."))
        self.assertTrue(desc.endswith("--- workq ---\nfirst\nsecond"))

    def test_comment_on_missing_task_is_gone(self):
        with self.assertRaises(BoardError) as ctx:
            self.board.comment("t-missing", "<p>x</p>")
        self.assertEqual(ctx.exception.kind, "gone")


class FakeBoardContractTests(BoardContract, unittest.TestCase):
    def setUp(self):
        self.board = FakeTaskBoard()
        self.board.add_plan("plan-1", NAMES)
        self.n = 0

    def make_task(self, title, bucket, creator, assigned, description="", checklist=(), refs=()):
        self.n += 1
        tid = f"t{self.n}"
        self.board.tasks[tid] = Task(tid, "plan-1", title, self.board.bucket_id("plan-1", bucket), creator,
                                     list(assigned))
        self.board.task_details[tid] = TaskDetails(description, list(checklist),
                                                   [Reference(u, a) for u, a in refs])
        return tid

    def bucket(self, name):
        return self.board.bucket_id("plan-1", name)

    def where(self, tid):
        return self.board.bucket_of(tid)

    def comments(self, tid):
        return [(h, m) for t, h, m in self.board.comments if t == tid]

    def refuse_mentions(self):
        self.board.refuse_mentions = True


class GraphBoardContractTests(BoardContract, GraphTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.graph.add_plan("plan-1", NAMES)
        self.board = GraphTaskBoard(self.client)

    def make_task(self, title, bucket, creator, assigned, description="", checklist=(), refs=()):
        return self.graph.add_task("plan-1", bucket, title, creator, assigned, description, checklist, refs)

    def bucket(self, name):
        return self.graph.bucket_id("plan-1", name)

    def where(self, tid):
        return self.graph.bucket_name(tid)

    def comments(self, tid):
        out = []
        for body in self.graph.comments(tid):
            mention = body["mentions"][0]["mentioned"] if body.get("mentions") else None
            out.append((body["content"], mention))
        return out

    def refuse_mentions(self):
        self.graph.mention_status = 400

    def test_move_retries_once_after_a_conflict(self):
        t = self.make_task("Alpha", "For workq", DAVID, [ME])
        self.graph.fail_next("PATCH", f"/v1.0/planner/tasks/{t}", 412)
        self.board.move(t, self.bucket("workq: done"))
        self.assertEqual(self.where(t), "workq: done")

    def test_intake_follows_next_pages(self):
        for n in range(3):
            self.make_task(f"T{n}", "For workq", DAVID, [ME])
        self.assertEqual(len(self.board.intake_tasks("plan-1", self.bucket("For workq"))), 3)

    def test_graph_attachment_source(self):
        from workq.attachments import GraphAttachmentSource
        self.graph.add_file(SP + "/notes.docx", "notes.docx", b"PK-fake")
        got = GraphAttachmentSource(self.client).fetch(SP + "/notes.docx", 1024)
        self.assertEqual((got.name, got.content, got.version), ("notes.docx", b"PK-fake", '"{v1,1}"'))
        self.assertEqual(self.graph.downloads[-1], (SP + "/notes.docx", False))    # no token on the download
        from workq.interfaces import AttachmentError
        with self.assertRaises(AttachmentError) as ctx:
            GraphAttachmentSource(self.client).fetch(SP + "/not-granted.docx", 1024)
        self.assertEqual(ctx.exception.kind, "denied")
        with self.assertRaises(AttachmentError):
            GraphAttachmentSource(self.client).fetch(SP + "/notes.docx", 3)


if __name__ == "__main__":
    unittest.main()
