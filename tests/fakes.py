"""In-memory fakes of workq's interfaces, and small Office files built for tests.

The fakes keep what they were given, so a test can check the exact moves, comments and
mail. The contract tests run the same checks against these and the Graph classes, so a
test that passes with a fake means something for Graph too."""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from workq.interfaces import Attachment, AttachmentError, Bucket, BoardError, MailError, TaskDetails
from workq.planner import with_status_note


class FakeTaskBoard:
    def __init__(self):
        self.plans = {}            # plan id -> {bucket id: name}
        self.tasks = {}            # task id -> Task
        self.task_details = {}     # task id -> TaskDetails
        self.moves = []            # (task id, bucket name)
        self.comments = []         # (task id, html, mention)
        self.raise_next = {}       # method name -> [BoardError, ...]
        self.refuse_mentions = False
        self.refuse_comments = None   # an error kind: every comment fails with it

    def _maybe_raise(self, method):
        queued = self.raise_next.get(method) or []
        if queued:
            raise queued.pop(0)

    def add_plan(self, plan_id, names):
        self.plans[plan_id] = {f"b{i}": n for i, n in enumerate(names)}

    def bucket_id(self, plan_id, name):
        return next(b for b, n in self.plans[plan_id].items() if n == name)

    def bucket_of(self, task_id):
        t = self.tasks[task_id]
        return self.plans[t.plan_id][t.bucket_id]

    # TaskBoard

    def buckets(self, plan_id):
        self._maybe_raise("buckets")
        if plan_id not in self.plans:
            raise BoardError("gone", f"no plan {plan_id}")
        return [Bucket(b, n) for b, n in self.plans[plan_id].items()]

    def create_bucket(self, plan_id, name):
        bid = f"b{len(self.plans[plan_id])}"
        self.plans[plan_id][bid] = name
        return Bucket(bid, name)

    def intake_tasks(self, plan_id, bucket_id):
        self._maybe_raise("intake_tasks")
        return [t for t in self.tasks.values() if t.plan_id == plan_id and t.bucket_id == bucket_id]

    def details(self, task_id):
        self._maybe_raise("details")
        if task_id not in self.tasks:
            raise BoardError("gone", "no such task")
        return self.task_details.get(task_id) or TaskDetails()

    def move(self, task_id, bucket_id):
        self._maybe_raise("move")
        if task_id not in self.tasks:
            raise BoardError("gone", "no such task")
        task = self.tasks[task_id]
        if task.bucket_id != bucket_id:
            task.bucket_id = bucket_id
            self.moves.append((task_id, self.plans[task.plan_id][bucket_id]))

    def comment(self, task_id, html, mention=None):
        self._maybe_raise("comment")
        if task_id not in self.tasks:
            raise BoardError("gone", "no such task")
        if self.refuse_comments:
            raise BoardError(self.refuse_comments, "comments refused")
        if mention and self.refuse_mentions:
            mention = None
        self.comments.append((task_id, html, mention))

    def set_status_note(self, task_id, text):
        self._maybe_raise("set_status_note")
        d = self.task_details.setdefault(task_id, TaskDetails())
        d.description = with_status_note(d.description, text)


class FakeQueue:
    """What Intake needs from the queue repo."""

    def __init__(self, known=()):
        self.known = set(known)
        self.added = []            # (name, brief, planner info, entries)

    def known_task_ids(self):
        return set(self.known) | {a[2]["task_id"] for a in self.added}

    def new_name(self, title):
        return f"2026-11-14-{title.lower().replace(' ', '-')[:30]}-{len(self.added) + 1}"

    def add_item(self, name, brief, planner_info, entries):
        self.added.append((name, brief, planner_info, entries))
        return f"inbox/{name}", f"commit{len(self.added)}"


class FakeAttachmentSource:
    def __init__(self):
        self.files = {}            # url -> (name, bytes) or a ServiceError to raise
        self.fetched = []

    def fetch(self, url, max_bytes):
        self.fetched.append(url)
        got = self.files.get(url)
        if got is None:
            raise AttachmentError("denied", "outside workq's access, or deleted")
        if isinstance(got, Exception):
            raise got
        name, content = got
        if len(content) > max_bytes:
            raise AttachmentError("invalid", f"over {max_bytes // (1024 * 1024)} MB")
        return Attachment(name=name, url=url, version="v1", size=len(content), content=content)


class FakeMailer:
    def __init__(self, fail_kind=None):
        self.sent = []
        self.fail_kind = fail_kind

    def send(self, recipients, subject, body):
        if self.fail_kind:
            raise MailError(self.fail_kind, "mail failed")
        self.sent.append((list(recipients), subject, body))


class FakeLinks:
    def file_link(self, path, commit):
        return f"https://repo.test/file/{path}@{commit}"

    def folder_link(self, path, commit):
        return f"https://repo.test/folder/{path}@{commit}"

    def current_link(self, path):
        return f"https://repo.test/now/{path}"


# ---------------------------------------------------------------- Office files

CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>{over}</Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '{rels}</Relationships>')
OFFDOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"


def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return buf.getvalue()


def make_docx(paragraphs=(), table=None, heading=None):
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = ""
    if heading:
        body += f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{heading}</w:t></w:r></w:p>'
    for p in paragraphs:
        body += f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>"
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
                       + "</w:tr>" for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    return _zip({
        "[Content_Types].xml": CT.format(over='<Override PartName="/word/document.xml" ContentType='
                                              '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'),
        "_rels/.rels": RELS.format(rels=f'<Relationship Id="rId1" Type="{OFFDOC}" Target="word/document.xml"/>'),
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body></w:document>',
    })


def make_xlsx(sheets):
    """sheets: {name: [[cell, ...], ...]}; strings go in sharedStrings, numbers inline."""
    S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    Rn = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    shared, parts, sheet_tags, rel_tags = [], {}, "", ""
    for n, (name, rows) in enumerate(sheets.items(), 1):
        xml_rows = ""
        for r, row in enumerate(rows, 1):
            cells = ""
            for c, value in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if isinstance(value, str):
                    shared.append(value)
                    cells += f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>'
                else:
                    cells += f'<c r="{ref}"><f>1+1</f><v>{value}</v></c>'
            xml_rows += f'<row r="{r}">{cells}</row>'
        parts[f"xl/worksheets/sheet{n}.xml"] = f'<?xml version="1.0"?><worksheet {S}><sheetData>{xml_rows}</sheetData></worksheet>'
        sheet_tags += f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>'
        rel_tags += (f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                     f'relationships/worksheet" Target="worksheets/sheet{n}.xml"/>')
    sst = "".join(f"<si><t>{s}</t></si>" for s in shared)
    parts.update({
        "[Content_Types].xml": CT.format(over=""),
        "_rels/.rels": RELS.format(rels=f'<Relationship Id="rId1" Type="{OFFDOC}" Target="xl/workbook.xml"/>'),
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook {S} {Rn}><sheets>{sheet_tags}</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": RELS.format(rels=rel_tags),
        "xl/sharedStrings.xml": f'<?xml version="1.0"?><sst {S}>{sst}</sst>',
    })
    return _zip(parts)


def make_pptx(slides):
    """slides: [[text, ...], ...] in order (stored out of order in the zip on purpose)."""
    P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    Rn = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    parts, ids, rels = {}, "", ""
    for n, texts in enumerate(slides, 1):
        paras = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in texts)
        # file numbers run backwards, so order must come from presentation.xml
        num = len(slides) - n + 1
        parts[f"ppt/slides/slide{num}.xml"] = (f'<?xml version="1.0"?><p:sld {P} {A}><p:cSld><p:spTree><p:sp><p:txBody>'
                                               f'{paras}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
        ids += f'<p:sldId id="{255 + n}" r:id="rId{n}"/>'
        rels += (f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                 f'relationships/slide" Target="slides/slide{num}.xml"/>')
    parts.update({
        "[Content_Types].xml": CT.format(over=""),
        "_rels/.rels": RELS.format(rels=f'<Relationship Id="rId1" Type="{OFFDOC}" Target="ppt/presentation.xml"/>'),
        "ppt/presentation.xml": f'<?xml version="1.0"?><p:presentation {P} {Rn}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": RELS.format(rels=rels),
    })
    return _zip(parts)
