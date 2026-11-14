"""Planner: take tasks in as briefs, and keep each task in step with its item.

The queue logic sees only the TaskBoard interface. GraphTaskBoard is the real one;
the tests use a fake. Nothing is sent to Planner while a stage runs: each change
goes into an outbox file after its push, and PlannerSync sends the outbox later, so
a Planner outage never holds up the queue.
"""

import contextlib
import datetime as dt
import fcntl
import html
import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path

from .graph import GraphError
from .interfaces import Bucket, BoardError, Reference, ServiceError, Task, TaskDetails

BUCKET_KEYS = ("intake", "planning", "working", "review", "blocked", "done")
DEFAULT_BUCKETS = {
    "intake": "For workq",
    "planning": "workq: planning",
    "working": "workq: working",
    "review": "workq: for review",
    "blocked": "workq: blocked",
    "done": "workq: done",
}
MENTION_SPAN = '<span itemid="0" itemtype="https://schema.skype.com/Mention/Person"></span>'
NOTE_MARK = "--- workq ---"
MAX_COMMENT = 4000
TRANSIENT = ("signin", "throttled", "network")


# ---------------------------------------------------------------- the board

def _board_error(exc):
    return BoardError(exc.kind, str(exc), getattr(exc, "status", None))


class GraphTaskBoard:
    """TaskBoard on Microsoft Graph. Graph's JSON and version tags stay in here."""

    def __init__(self, client):
        self.client = client

    def _call(self, *args, **kwargs):
        try:
            return self.client.call(*args, **kwargs)
        except GraphError as exc:
            raise _board_error(exc) from None

    def _all(self, path, version="v1.0"):
        try:
            return self.client.get_all(path, version=version)
        except GraphError as exc:
            raise _board_error(exc) from None

    def buckets(self, plan_id):
        return [Bucket(b["id"], b.get("name", "")) for b in self._all(f"/planner/plans/{_q(plan_id)}/buckets")]

    def create_bucket(self, plan_id, name):
        _s, _h, data = self._call("POST", "/planner/buckets", {"name": name, "planId": plan_id, "orderHint": " !"})
        return Bucket(data["id"], data.get("name", name))

    def intake_tasks(self, plan_id, bucket_id):
        return [self._task(t) for t in self._all(f"/planner/buckets/{_q(bucket_id)}/tasks")
                if t.get("planId", plan_id) == plan_id]

    def get_task(self, task_id):
        _s, _h, data = self._call("GET", f"/planner/tasks/{_q(task_id)}")
        return self._task(data)

    @staticmethod
    def _task(d):
        creator = ((d.get("createdBy") or {}).get("user") or {}).get("id", "")
        return Task(
            id=d.get("id", ""), plan_id=d.get("planId", ""), title=d.get("title") or "",
            bucket_id=d.get("bucketId", ""), created_by=creator,
            assignees=sorted((d.get("assignments") or {}).keys()),
            percent_complete=int(d.get("percentComplete") or 0),
            due=d.get("dueDateTime"), etag=d.get("@odata.etag", ""),
        )

    def details(self, task_id):
        _s, _h, d = self._call("GET", f"/planner/tasks/{_q(task_id)}/details")
        checklist = sorted((d.get("checklist") or {}).values(), key=lambda c: str(c.get("orderHint", "")))
        refs = []
        for key, value in (d.get("references") or {}).items():
            value = value or {}
            refs.append(Reference(url=urllib.parse.unquote(key), alias=value.get("alias") or "",
                                  kind=value.get("type") or ""))
        return TaskDetails(description=d.get("description") or "",
                           checklist=[str(c.get("title") or "") for c in checklist if c.get("title")],
                           references=refs, etag=d.get("@odata.etag", ""))

    def move(self, task_id, bucket_id):
        for attempt in (1, 2):
            task = self.get_task(task_id)
            if task.bucket_id == bucket_id:
                return
            try:
                self._call("PATCH", f"/planner/tasks/{_q(task_id)}", {"bucketId": bucket_id},
                           headers={"If-Match": task.etag})
                return
            except BoardError as exc:
                if exc.kind != "conflict" or attempt == 2:
                    raise

    def comment(self, task_id, html_text, mention=None):
        body = {"content": html_text}
        if mention:
            body = {"content": f"<div>{MENTION_SPAN} {html_text}</div>",
                    "mentions": [{"mentioned": mention, "position": 0, "mentionType": "user"}]}
        try:
            self._call("POST", f"/planner/tasks/{_q(task_id)}/messages", body, version="beta")
        except BoardError as exc:
            if mention and exc.kind in ("invalid", "denied"):
                # The mentioned person may not be in the plan: send it plain.
                self._call("POST", f"/planner/tasks/{_q(task_id)}/messages", {"content": html_text},
                           version="beta")
                return
            raise

    def set_status_note(self, task_id, text):
        for attempt in (1, 2):
            _s, _h, d = self._call("GET", f"/planner/tasks/{_q(task_id)}/details")
            description = with_status_note(d.get("description") or "", text)
            try:
                self._call("PATCH", f"/planner/tasks/{_q(task_id)}/details", {"description": description},
                           headers={"If-Match": d.get("@odata.etag", "")})
                return
            except BoardError as exc:
                if exc.kind != "conflict" or attempt == 2:
                    raise


def _q(value):
    return urllib.parse.quote(str(value), safe="")


def with_status_note(description, text, keep=10):
    """The task description with workq's own section at the end, holding the last few notes."""
    own, mark, notes = description.partition(NOTE_MARK)
    lines = [line for line in notes.splitlines() if line.strip()] if mark else []
    lines = (lines + [line for line in text.splitlines() if line.strip()])[-keep:]
    return own.rstrip() + "\n\n" + NOTE_MARK + "\n" + "\n".join(lines)


# ---------------------------------------------------------------- links

class TemplateLinkBuilder:
    """Links into the queue repo from planner.links: file and folder pinned to a commit,
    current = the folder on the branch as it is now."""

    def __init__(self, links, branch):
        self.links = links
        self.branch = branch

    def _fill(self, template, path, commit=""):
        return template.format(path=urllib.parse.quote(path.strip("/"), safe="/-_.~"),
                               commit=commit, branch=urllib.parse.quote(self.branch, safe=""))

    def file_link(self, path, commit):
        return self._fill(self.links["file"], path, commit)

    def folder_link(self, path, commit):
        return self._fill(self.links["folder"], path, commit)

    def current_link(self, path):
        return self._fill(self.links["current"], path)


# ---------------------------------------------------------------- comments

class _Text:
    """One comment, built as HTML for Planner and as plain text for the fallback."""

    def __init__(self):
        self.html = []
        self.text = []

    def line(self, *parts):
        h, t = [], []
        for part in parts:
            if isinstance(part, tuple):
                label, url = part
                h.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
                t.append(f"{label} ({url})")
            else:
                h.append(html.escape(str(part)))
                t.append(str(part))
        self.html.append("<p>" + "".join(h) + "</p>")
        self.text.append("".join(t))

    def links(self, pairs):
        pairs = [p for p in pairs if p]
        if not pairs:
            return
        parts = []
        for i, pair in enumerate(pairs):
            if i:
                parts.append(" · ")
            parts.append(pair)
        self.line(*parts)

    def done(self):
        page = "".join(self.html)
        if len(page) > MAX_COMMENT:
            page = page[:MAX_COMMENT - 20].rsplit("<p>", 1)[0] + "<p>(cut)</p>"
        return page, "\n".join(self.text)


def short(text, limit=300):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def plain_reason(reason):
    """A blocked reason without anything an agent wrote: the part before ';' or '('."""
    return re.split(r"[;(]", str(reason or "blocked"))[0].strip() or "blocked"


class CommentBook:
    """What workq says on a task at each step, with links to the files it mentions."""

    QUIET_EVENTS = ("executing", "checking")

    def __init__(self, links, summaries=False, quiet=False, mention=None, enabled=True):
        self.links = links
        self.summaries = summaries
        self.quiet = quiet
        self.mention = mention
        self.enabled = enabled

    def build(self, event, item_rel, commit, info):
        """(bucket key or None, html, text, mention) for an event, or None to say nothing.

        info holds what the runner knows: tier, attempt, max_attempts, steps, reason,
        summary (agent-written; used only with summaries on), questions (count),
        has_out, newest_log, could_not_open (list of names), verdict."""
        bucket = {
            "taken": "planning", "planned": "working", "questions": "blocked", "blocked": "blocked",
            "check_failed": "working", "review": "review", "replan": "planning",
            "sent_back": "planning", "archived": "done",
        }.get(event)
        if not self.enabled or (self.quiet and event in self.QUIET_EVENTS):
            return (bucket, "", "", None) if bucket else None
        f = lambda name: (name, self.links.file_link(f"{item_rel}/{name}", commit))  # noqa: E731
        folder = lambda name: (name, self.links.folder_link(f"{item_rel}/{name}", commit))  # noqa: E731
        here = ("the item folder", self.links.current_link(item_rel))
        c = _Text()
        mention = None
        if event == "taken":
            c.line(f"Taken by workq as {item_rel}.")
            if info.get("could_not_open"):
                c.line("Could not open: " + ", ".join(info["could_not_open"]) + ".")
            c.links([f("brief.md")])
        elif event == "refused":
            c.line("Not taken: only listed people can send work to workq.")
        elif event == "planned":
            steps = f" Steps: {info['steps']}." if info.get("steps") else ""
            c.line(f"Planned. Tier: {info.get('tier') or '-'}.{steps}")
            c.links([f("plan.md")])
        elif event == "executing":
            c.line(f"Executing ({info.get('tier') or '-'}, run {info.get('attempt', 1)} of "
                   f"{info.get('max_attempts', 3)}).")
            c.links([f("plan.md")])
        elif event == "checking":
            c.line("Checking the result.")
            c.links([f("result.md"), folder("out/") if info.get("has_out") else None])
        elif event == "questions":
            n = info.get("questions") or 0
            c.line(f"Blocked: {n} question{'s' if n != 1 else ''}.")
            if self.summaries and info.get("question_lines"):
                for q in info["question_lines"][:10]:
                    c.line(short(q, 300))
            c.line("To answer, add review.md with verdict: retry as its first line.")
            c.links([f("questions.md"), here])
            mention = self.mention
        elif event == "blocked":
            reason = short(info.get("reason"), 300) if self.summaries else plain_reason(info.get("reason"))
            c.line(f"Blocked: {reason}.")
            c.line("To retry, add review.md with verdict: retry as its first line.")
            c.links([here, f(info["newest_log"]) if info.get("newest_log") else None])
            mention = self.mention
        elif event == "check_failed":
            c.line(f"Check failed (attempt {info.get('attempt', 1)} of {info.get('max_attempts', 3)}).")
            if self.summaries and info.get("summary"):
                c.line(short(info["summary"]))
            c.links([f("check.md")])
        elif event == "review":
            c.line("Ready for review.")
            if self.summaries and info.get("summary"):
                c.line(short(info["summary"]))
            c.links([f("result.md"), f("check.md"), folder("out/") if info.get("has_out") else None])
            c.line("To answer, add review.md to ", here,
                   ", first line verdict: approve, rework or reject.")
            mention = self.mention
        elif event == "replan":
            c.line("Back to planning: the executor asked for a new plan.")
            c.links([f("result.md")])
        elif event == "sent_back":
            c.line("Back in the queue with your notes.")
            c.links([f("notes.md")])
        elif event == "archived":
            word = "Approved" if info.get("verdict") == "approve" else "Rejected"
            c.line(f"{word} and archived.")
            c.links([("the archive folder", self.links.folder_link(item_rel, commit))])
        else:
            return (bucket, "", "", None) if bucket else None
        page, text = c.done()
        return bucket, page, text, mention


# ---------------------------------------------------------------- outbox

class Outbox:
    """Changes waiting to be sent to Planner, one JSON line each, oldest first."""

    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _read(self):
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def add(self, **event):
        event.setdefault("id", uuid.uuid4().hex)
        event.setdefault("at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        with self._locked():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(json.dumps(event) + "\n")
        return event

    def read(self):
        with self._locked():
            return self._read()

    def remove(self, ids):
        if not ids:
            return
        with self._locked():
            kept = [e for e in self._read() if e.get("id") not in ids]
            tmp = self.path.with_name(self.path.name + ".new")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("".join(json.dumps(e) + "\n" for e in kept))
            os.replace(tmp, self.path)


class PlannerSync:
    """Send the outbox: the newest bucket move per task, and every comment, in order.

    Sign-in, throttling and network trouble stop the sync and keep the rest for next
    time. A task that is gone or out of reach has its line dropped. A comment the
    (beta) comments API won't take goes into the task's description instead."""

    def __init__(self, board, outbox, plans, problem, now=None, max_age_days=7):
        self.board = board
        self.outbox = outbox
        self.plans = {p["id"]: p for p in plans}
        self.problem = problem
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.max_age = dt.timedelta(days=max_age_days)
        self._bucket_ids = {}

    def bucket_id(self, plan_id, key):
        plan = self.plans.get(plan_id)
        if not plan:
            raise BoardError("denied", f"plan {plan_id} is not in planner.plans")
        if plan_id not in self._bucket_ids:
            self._bucket_ids[plan_id] = {b.name: b.id for b in self.board.buckets(plan_id)}
        name = plan["buckets"][key]
        found = self._bucket_ids[plan_id].get(name)
        if not found:
            raise BoardError("gone", f"bucket \"{name}\" is missing from plan {plan.get('name') or plan_id}: "
                                     "run `workq planner setup`")
        return found

    def run(self):
        events = self.outbox.read()
        result = {"sent": 0, "dropped": 0, "kept": 0, "stopped": None}
        if not events:
            return result
        last_move = {}
        for i, ev in enumerate(events):
            if ev.get("bucket"):
                last_move[ev.get("task")] = i
        done = set()
        for i, ev in enumerate(events):
            if self._too_old(ev):
                done.add(ev["id"])
                result["dropped"] += 1
                self.problem(f"dropped a Planner update older than {self.max_age.days} days "
                             f"({ev.get('kind')} for {ev.get('item')})")
                continue
            try:
                self._send(ev, move=ev.get("bucket") and last_move.get(ev.get("task")) == i)
                done.add(ev["id"])
                result["sent"] += 1
            except ServiceError as exc:
                if exc.kind in TRANSIENT:
                    result["stopped"] = exc.kind
                    self.problem(f"Planner sync stopped: {exc}")
                    break
                if exc.kind == "conflict":
                    continue          # the task keeps changing under us: try at the next sync
                done.add(ev["id"])
                result["dropped"] += 1
                self.problem(f"Planner update dropped ({ev.get('kind')} for {ev.get('item')}): {exc}")
        self.outbox.remove(done)
        result["kept"] = len(events) - len(done)
        return result

    def _too_old(self, ev):
        try:
            at = dt.datetime.fromisoformat(ev.get("at"))
        except (TypeError, ValueError):
            return False
        return self.now() - at > self.max_age

    def _send(self, ev, move):
        task = ev["task"]
        if move:
            try:
                self.board.move(task, self.bucket_id(ev.get("plan"), ev["bucket"]))
            except BoardError as exc:
                if exc.kind in TRANSIENT or exc.kind == "conflict":
                    raise
                if not ev.get("html"):
                    raise
                self.problem(f"could not move the Planner task for {ev.get('item')}: {exc}")
        if not ev.get("html"):
            return
        try:
            self.board.comment(task, ev["html"], ev.get("mention"))
        except BoardError as exc:
            if exc.kind in TRANSIENT or exc.kind == "gone":
                raise
            self.board.set_status_note(task, ev.get("text") or "")
            self.problem(f"Planner comments failed ({exc}); wrote to the task's description instead")


# ---------------------------------------------------------------- intake

class RefusedLog:
    """Tasks already told they can't be taken, so the comment is posted once."""

    def __init__(self, path):
        self.path = Path(path)

    def ids(self):
        try:
            return {line.strip() for line in self.path.read_text().splitlines() if line.strip()}
        except OSError:
            return set()

    def add(self, task_id):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(task_id + "\n")


def md_line(text):
    return " ".join(str(text or "").split()).replace("<", "&lt;")


def render_brief(task, details, plan_name, entries):
    """brief.md for a Planner task."""
    title = md_line(task.title) or "Untitled task"
    out = [f"# {title}", "", "## Ask", "", details.description.strip() or "(No description on the Planner task.)", ""]
    if details.checklist:
        out += ["## Good looks like", ""] + [f"- {md_line(c)}" for c in details.checklist] + [""]
    if task.due:
        out += ["## Wanted by", "", str(task.due)[:10], ""]
    if entries:
        out += ["## Inputs and sources", ""]
        for e in entries:
            name = md_line(e.get("name") or e.get("source"))
            if e["status"] == "ok":
                copies = [c for c in e.get("copies", []) if c != e["name"]]
                also = f"; also {', '.join('refs/' + c for c in copies)}" if copies else ""
                out.append(f"- {name}: refs/{e['name']}{also}")
            elif e["status"] == "web":
                out.append(f"- {name}: {e['source']} (a web page; not fetched)")
            else:
                out.append(f"- {name}: could not open ({e.get('reason')})")
        out += ["", "Files attached to the Planner task are in `refs/` in this folder while workq "
                    "works on the item. They are not kept in the repo; `refs.yaml` lists them.", ""]
    out += ["---", "", f"From the Planner task \"{title}\" on {md_line(plan_name)}."]
    return "\n".join(out) + "\n"


class Intake:
    """Turn Planner tasks into inbox items.

    A task is taken when it is in the plan's intake bucket, assigned to the signed-in
    account, not complete, and created by someone on accept_from. The queue adapter
    does the git work; the board and the attachment keeper do the rest."""

    def __init__(self, board, queue, plans, accept_from, me, outbox, book, refused, problem,
                 log=print, attachments=None):
        self.board = board
        self.queue = queue
        self.plans = plans
        self.accept_from = {str(x).lower() for x in accept_from}
        self.me = str(me or "").lower()
        self.outbox = outbox
        self.book = book
        self.refused = refused
        self.problem = problem
        self.log = log
        self.attachments = attachments

    def run(self):
        """Returns the number of tasks taken. Stops early if Graph can't be used."""
        if not self.me:
            raise BoardError("signin", "the signed-in account has no ID: run `workq graph login` again")
        taken = 0
        for plan in self.plans:
            buckets = {b.name: b.id for b in self.board.buckets(plan["id"])}
            intake_id = buckets.get(plan["buckets"]["intake"])
            if not intake_id:
                self.problem(f"plan {plan.get('name') or plan['id']} has no bucket "
                             f"\"{plan['buckets']['intake']}\": run `workq planner setup`")
                continue
            known = self.queue.known_task_ids()
            refused = self.refused.ids()
            for task in self.board.intake_tasks(plan["id"], intake_id):
                if task.id in known or task.percent_complete >= 100:
                    continue
                if self.me not in {a.lower() for a in task.assignees}:
                    continue
                if task.created_by.lower() not in self.accept_from:
                    if task.id not in refused:
                        self._say("refused", task, plan, item_rel="", commit="", info={})
                        self.refused.add(task.id)
                        self.log(f"[intake] task {task.id} not taken: creator is not on accept_from")
                    continue
                try:
                    self._take(task, plan)
                    taken += 1
                except BoardError:
                    raise
                except ServiceError as exc:
                    if exc.kind in TRANSIENT:
                        raise BoardError(exc.kind, str(exc))
                    self.problem(f"could not take Planner task \"{short(task.title, 80)}\": {exc}")
        return taken

    def _take(self, task, plan):
        details = self.board.details(task.id)
        name = self.queue.new_name(task.title)
        entries = self.attachments.collect(name, details.references) if (self.attachments and details.references) else []
        if details.references and not self.attachments:
            entries = [{"name": r.alias or r.url, "source": r.url, "status": "skipped",
                        "reason": "attachments are switched off"} for r in details.references]
        brief = render_brief(task, details, plan.get("name") or plan["id"], entries)
        planner_meta = {"plan_id": plan["id"], "task_id": task.id, "created_by": task.created_by}
        item_rel, commit = self.queue.add_item(name, brief, planner_meta, entries)
        could_not = [e.get("name") or e.get("source") for e in entries if e["status"] in ("failed", "skipped")]
        self._say("taken", task, plan, item_rel=item_rel, commit=commit, info={"could_not_open": could_not})
        self.log(f"[intake] took Planner task \"{short(task.title, 80)}\" as {item_rel}")

    def _say(self, event, task, plan, item_rel, commit, info):
        built = self.book.build(event, item_rel, commit, info)
        if not built:
            return
        bucket, page, text, mention = built
        self.outbox.add(kind=event, task=task.id, plan=plan["id"], item=item_rel or task.title[:60],
                        bucket=bucket, html=page, text=text, mention=mention)
