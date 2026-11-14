"""Files attached to a Planner task: fetched once, kept in the runner's state folder,
never in git, and copied into the item's refs/ folder only while a stage runs."""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import urllib.parse
from pathlib import Path

from .graph import GraphError
from .interfaces import Attachment, AttachmentError, ConvertError, ServiceError

FETCHED = (".docx", ".pptx", ".xlsx", ".xlsm", ".pdf", ".csv", ".txt", ".md", ".png", ".jpg", ".jpeg")
OFFICE = (".docx", ".pptx", ".xlsx", ".xlsm")


def is_sharepoint(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host.endswith(".sharepoint.com")


def share_id(url):
    """Graph's id for a sharing link or file address: u! + unpadded base64url."""
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


class GraphAttachmentSource:
    """AttachmentSource on Graph's shares API. Needs Files.SelectedOperations.Selected,
    and a grant on the library the file is in."""

    def __init__(self, client):
        self.client = client

    def fetch(self, url, max_bytes):
        sid = share_id(url)
        try:
            _s, _h, item = self.client.call("GET", f"/shares/{sid}/driveItem")
            if "file" not in item:
                raise AttachmentError("invalid", "not a file (a folder or a page)")
            size = int(item.get("size") or 0)
            if size > max_bytes:
                raise AttachmentError("invalid", f"over {max_bytes // (1024 * 1024)} MB")
            link = item.get("@microsoft.graph.downloadUrl")
            if not link:
                status, headers, _b = self.client.call("GET", f"/shares/{sid}/driveItem/content")
                link = next((v for k, v in headers.items() if k.lower() == "location"), None)
                if status not in (301, 302, 303, 307, 308) or not link:
                    raise AttachmentError("other", "no download address came back")
            content = self.client.download(link, max_bytes)
        except GraphError as exc:
            if exc.kind in ("denied", "gone"):
                raise AttachmentError(exc.kind, "outside workq's access, or deleted") from None
            raise AttachmentError(exc.kind, str(exc)) from None
        return Attachment(name=item.get("name") or "file", url=url, version=str(item.get("eTag") or ""),
                          size=len(content), content=content)


def safe_name(name, taken):
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", os.path.basename(str(name))).strip(" .-") or "file"
    stem = stem[:120]
    out, n = stem, 2
    while out.lower() in taken:
        base, dot, ext = stem.rpartition(".")
        out = f"{base}-{n}.{ext}" if dot else f"{stem}-{n}"
        n += 1
    taken.add(out.lower())
    return out


class AttachmentKeeper:
    """Fetch, convert and keep a task's attachments in state/attachments/<item>/."""

    def __init__(self, source, converters, root, max_file_mb=20, max_item_mb=50, keep_days=30):
        self.source = source
        self.converters = converters
        self.root = Path(root)
        self.max_file = int(float(max_file_mb) * 1024 * 1024)
        self.max_item = int(float(max_item_mb) * 1024 * 1024)
        self.keep_days = keep_days

    def folder(self, item_id):
        return self.root / item_id / "files"

    def _prepare_root(self):
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def collect(self, item_id, references):
        """Fetch every reference. Returns the entries for refs.yaml."""
        self._prepare_root()
        dest = self.folder(item_id)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        taken, entries, total = set(), [], 0
        for ref in references:
            label = ref.alias or os.path.basename(urllib.parse.urlsplit(ref.url).path) or ref.url
            entry = {"name": label, "source": ref.url}
            ext = os.path.splitext(urllib.parse.urlsplit(ref.url).path)[1].lower() or \
                os.path.splitext(label)[1].lower()
            if not is_sharepoint(ref.url):
                entry.update(status="web")
            elif ext and ext not in FETCHED:
                entry.update(status="skipped", reason=f"{ext} files are not fetched")
            elif total >= self.max_item:
                entry.update(status="skipped", reason=f"the item's files are over "
                                                      f"{self.max_item // (1024 * 1024)} MB")
            else:
                try:
                    got = self.source.fetch(ref.url, min(self.max_file, self.max_item - total))
                    entry.update(self._keep(got, dest, taken))
                    total += got.size
                except ServiceError as exc:
                    entry.update(status="failed", reason=str(exc)[:200])
            entries.append(entry)
        self._write_index(item_id, entries)
        return entries

    def _keep(self, got, dest, taken):
        ext = os.path.splitext(got.name)[1].lower()
        if ext not in FETCHED:
            return {"status": "skipped", "reason": f"{ext or 'this'} files are not fetched"}
        name = safe_name(got.name, taken)
        path = dest / name
        path.write_bytes(got.content)
        os.chmod(path, 0o600)
        entry = {"name": name, "status": "ok", "version": got.version, "size": got.size,
                 "sha256": hashlib.sha256(got.content).hexdigest(), "copies": [name]}
        if ext in OFFICE:
            notes = []
            for conv in self.converters:
                if not conv.accepts(name):
                    continue
                try:
                    for made in conv.convert(path, dest):
                        made_name = safe_name(made.name, taken) if made.name.lower() in taken else made.name
                        if made_name != made.name:
                            made = made.rename(dest / made_name)
                        taken.add(made.name.lower())
                        os.chmod(made, 0o600)
                        entry["copies"].append(made.name)
                except ConvertError as exc:
                    notes.append(str(exc)[:150])
            if len(entry["copies"]) == 1 and notes:
                path.unlink()
                return {"status": "failed", "reason": "could not open (encrypted or damaged): " + notes[0]}
            if notes:
                entry["notes"] = notes
        return entry

    def _write_index(self, item_id, entries):
        path = self.root / item_id / "entries.json"
        path.write_text(json.dumps(entries, indent=1))

    def place(self, item_id, item_dir):
        """Copy the kept files into <item>/refs/ for one run. Returns True if any were copied."""
        src = self.folder(item_id)
        if not src.is_dir() or not any(src.iterdir()):
            return False
        dest = Path(item_dir) / "refs"
        if dest.exists() or dest.is_symlink():
            remove_refs(item_dir)
        dest.mkdir()
        for path in sorted(src.iterdir()):
            if path.is_file() and not path.is_symlink():
                shutil.copyfile(path, dest / path.name)
        return True

    def mark_archived(self, item_id):
        folder = self.root / item_id
        if folder.is_dir():
            (folder / "archived").write_text(dt.date.today().isoformat())

    def cleanup(self, today=None):
        """Delete the files of items archived more than keep_days ago."""
        today = today or dt.date.today()
        removed = []
        if not self.root.is_dir():
            return removed
        for folder in self.root.iterdir():
            mark = folder / "archived"
            if not mark.is_file():
                continue
            try:
                when = dt.date.fromisoformat(mark.read_text().strip())
            except ValueError:
                continue
            if (today - when).days >= self.keep_days:
                shutil.rmtree(folder, ignore_errors=True)
                removed.append(folder.name)
        return removed


def remove_refs(item_dir):
    """Delete <item>/refs/ after a run, whatever the agent did to it."""
    dest = Path(item_dir) / "refs"
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        for root, _dirs, _files in os.walk(dest):
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
        shutil.rmtree(dest, ignore_errors=True)


def refs_yaml_entries(entries):
    """What goes into refs.yaml in the repo: no file contents, just what was used."""
    out = []
    for e in entries:
        row = {"name": e.get("name"), "source": e.get("source"), "status": e.get("status")}
        for key in ("version", "size", "sha256", "copies", "reason", "notes"):
            if e.get(key):
                row[key] = e[key]
        out.append(row)
    return out
