"""Turn Office files into forms Claude Code can read: Markdown, CSV and PDF.

Word, Excel and PowerPoint files are zipped XML, so the Markdown and CSV copies need
only Python's standard library. The PDF copies need LibreOffice (`soffice`), which
workq never installs. Every converter is a DocumentConverter: accepts() and convert().
"""

import csv
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .interfaces import ConvertError

MAX_UNZIPPED = 200 * 1024 * 1024     # all members read from one file
MAX_RATIO = 200                      # compressed vs. unzipped size, per member
MAX_OUTPUT = 20 * 1024 * 1024

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class SafeZip:
    """Read members of an Office file with limits, and refuse XML with a DTD (Office
    files never have one; a DTD is how entity bombs get in)."""

    def __init__(self, path):
        try:
            self.zip = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ConvertError(f"not a readable Office file (encrypted or damaged): {exc}")
        self.read_total = 0

    def names(self):
        return set(self.zip.namelist())

    def xml(self, name):
        try:
            info = self.zip.getinfo(name)
        except KeyError:
            raise ConvertError(f"missing part {name}")
        if info.file_size > MAX_UNZIPPED or (info.compress_size and info.file_size / info.compress_size > MAX_RATIO
                                             and info.file_size > 10 * 1024 * 1024):
            raise ConvertError(f"part {name} is too large")
        self.read_total += info.file_size
        if self.read_total > MAX_UNZIPPED:
            raise ConvertError("file unzips to too much data")
        data = self.zip.read(name)
        head = data[:4096].upper()
        if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
            raise ConvertError(f"part {name} has a DTD; refused")
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise ConvertError(f"part {name} is not valid XML: {exc}")

    def rels(self, name):
        """Relationship id -> target path, for a part."""
        folder, _, base = name.rpartition("/")
        rels_name = f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"
        if rels_name not in self.names():
            return {}
        out = {}
        for rel in self.xml(rels_name).iter(PR + "Relationship"):
            target = rel.get("Target", "")
            if rel.get("TargetMode") == "External":
                continue
            target = target.lstrip("/") if target.startswith("/") else os.path.normpath(
                os.path.join(folder, target)).replace(os.sep, "/")
            out[rel.get("Id")] = target
        return out

    def close(self):
        self.zip.close()


def _write(path, text):
    data = text.encode("utf-8")
    if len(data) > MAX_OUTPUT:
        data = data[:MAX_OUTPUT] + "\n\n(cut: the converted text was over 20 MB)\n".encode()
    path.write_bytes(data)
    return path


def safe_part(text, limit=60):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:limit] or "part"


# ---------------------------------------------------------------- Word

class WordToMarkdown:
    """Headings, paragraphs, lists and tables from a .docx, as Markdown."""

    def accepts(self, name):
        return name.lower().endswith(".docx")

    def convert(self, path, out_dir):
        z = SafeZip(path)
        try:
            body = z.xml("word/document.xml").find(W + "body")
            lines = []
            for el in list(body if body is not None else []):
                if el.tag == W + "p":
                    lines.append(self._para(el))
                elif el.tag == W + "tbl":
                    lines.extend(self._table(el))
                    lines.append("")
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
        finally:
            z.close()
        return [_write(Path(out_dir) / (Path(path).name + ".md"), text)]

    @staticmethod
    def _text(el):
        parts = []
        for node in el.iter():
            if node.tag == W + "t" and node.text:
                parts.append(node.text)
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                parts.append("\n")
        return "".join(parts)

    def _para(self, p):
        text = self._text(p).strip()
        props = p.find(W + "pPr")
        style = ""
        listed = False
        if props is not None:
            st = props.find(W + "pStyle")
            style = (st.get(W + "val") or "") if st is not None else ""
            listed = props.find(W + "numPr") is not None
        if not text:
            return ""
        m = re.match(r"(?i)heading\s*(\d)", style)
        if m:
            return "#" * min(6, int(m.group(1)) + 1) + " " + text + "\n"
        if style.lower() == "title":
            return "# " + text + "\n"
        if listed or style.lower().startswith("list"):
            return "- " + text
        return text + "\n"

    def _table(self, tbl):
        rows = []
        for tr in tbl.iter(W + "tr"):
            cells = [" ".join(self._text(tc).split()).replace("|", "\\|") for tc in tr.findall(W + "tc")]
            rows.append(cells)
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return out


# ---------------------------------------------------------------- Excel

def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in (letters.group(0) if letters else "A"):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


class ExcelToCsv:
    """One CSV per sheet of an .xlsx: the values as last saved, not the formulas."""

    def accepts(self, name):
        return name.lower().endswith((".xlsx", ".xlsm"))

    def convert(self, path, out_dir):
        z = SafeZip(path)
        written = []
        try:
            shared = []
            if "xl/sharedStrings.xml" in z.names():
                for si in z.xml("xl/sharedStrings.xml").iter(S + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(S + "t")))
            book = z.xml("xl/workbook.xml")
            rels = z.rels("xl/workbook.xml")
            sheets = book.find(S + "sheets")
            for n, sheet in enumerate(list(sheets if sheets is not None else []), 1):
                target = rels.get(sheet.get(R + "id"))
                if not target or target not in z.names():
                    continue
                rows = self._rows(z.xml(target), shared)
                buf = io.StringIO()
                csv.writer(buf).writerows(rows)
                name = f"{Path(path).name}-{n}-{safe_part(sheet.get('name') or 'sheet')}.csv"
                written.append(_write(Path(out_dir) / name, buf.getvalue()))
        finally:
            z.close()
        if not written:
            raise ConvertError("no sheets found")
        return written

    @staticmethod
    def _rows(root, shared):
        rows = []
        for row in root.iter(S + "row"):
            values = {}
            for c in row.findall(S + "c"):
                kind = c.get("t")
                v = c.find(S + "v")
                if kind == "s" and v is not None:
                    try:
                        value = shared[int(v.text)]
                    except (ValueError, IndexError, TypeError):
                        value = ""
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(S + "t"))
                elif kind == "b" and v is not None:
                    value = "TRUE" if v.text == "1" else "FALSE"
                else:
                    value = v.text if v is not None and v.text is not None else ""
                values[_col_index(c.get("r"))] = value
            if values:
                width = max(values) + 1
                rows.append([values.get(i, "") for i in range(width)])
            else:
                rows.append([])
        return rows


# ---------------------------------------------------------------- PowerPoint

class PowerPointToText:
    """The text of each slide of a .pptx, in order, as Markdown."""

    def accepts(self, name):
        return name.lower().endswith(".pptx")

    def convert(self, path, out_dir):
        z = SafeZip(path)
        try:
            pres = z.xml("ppt/presentation.xml")
            rels = z.rels("ppt/presentation.xml")
            order = [rels.get(s.get(R + "id")) for s in pres.iter(P + "sldId")]
            out = []
            for n, target in enumerate([t for t in order if t and t in z.names()], 1):
                out.append(f"## Slide {n}\n")
                for para in z.xml(target).iter(A + "p"):
                    text = "".join(t.text or "" for t in para.iter(A + "t")).strip()
                    if text:
                        out.append(text)
                out.append("")
        finally:
            z.close()
        return [_write(Path(out_dir) / (Path(path).name + ".md"), "\n".join(out).strip() + "\n")]


# ---------------------------------------------------------------- LibreOffice

class LibreOfficePdf:
    """A PDF copy of a Word or PowerPoint file, made by LibreOffice in its own profile."""

    def __init__(self, soffice=None, timeout=180, work_dir=None):
        self.soffice = soffice or shutil.which("soffice") or shutil.which("libreoffice")
        self.timeout = timeout
        self.work_dir = work_dir

    def available(self):
        return bool(self.soffice)

    def accepts(self, name):
        return self.available() and name.lower().endswith((".docx", ".pptx", ".doc", ".ppt"))

    def convert(self, path, out_dir):
        if not self.soffice:
            raise ConvertError("LibreOffice (soffice) is not installed")
        with tempfile.TemporaryDirectory(prefix="workq-soffice-", dir=self.work_dir) as tmp:
            profile = Path(tmp) / "profile"
            outdir = Path(tmp) / "out"
            outdir.mkdir()
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "LANG": "C.UTF-8"}
            cmd = [self.soffice, "--headless", "--norestore", "--nolockcheck",
                   f"-env:UserInstallation=file://{profile}", "--convert-to", "pdf",
                   "--outdir", str(outdir), str(path)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env)
            except subprocess.TimeoutExpired:
                raise ConvertError("LibreOffice took too long")
            except OSError as exc:
                raise ConvertError(f"cannot start LibreOffice: {exc}")
            made = sorted(outdir.glob("*.pdf"))
            if proc.returncode != 0 or not made:
                raise ConvertError("LibreOffice could not convert it: "
                                   + (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:200])
            dest = Path(out_dir) / (Path(path).name + ".pdf")
            shutil.move(str(made[0]), dest)
            return [dest]


def default_converters(work_dir=None):
    return [WordToMarkdown(), ExcelToCsv(), PowerPointToText(), LibreOfficePdf(work_dir=work_dir)]
