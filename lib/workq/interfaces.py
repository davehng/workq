"""The small interfaces the queue logic talks to, and the plain types that cross them.

The core never sees Graph, SMTP or file-format details: it holds a TaskBoard, an
AttachmentSource, DocumentConverters, a Mailer, a TokenSource and a LinkBuilder, and
build_services() decides which real classes stand behind them. Tests pass in fakes.

Each interface is a typing.Protocol: a class meets it by having the methods, without
inheriting from it.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Protocol


# ---------------------------------------------------------------- errors

class ServiceError(Exception):
    """A failure in an outside service, sorted into a kind the core can act on:

    signin     the sign-in has expired or was revoked: stop, ask for `workq graph login`
    throttled  the service asked us to slow down, and kept asking: try at the next sync
    network    could not reach the service (or its TLS certificate was not trusted)
    conflict   the thing changed since it was read, even after one re-read
    gone       not found (deleted, or never there)
    denied     the account may not do this
    invalid    the service refused the request as malformed
    other      anything else
    """

    KINDS = ("signin", "throttled", "network", "conflict", "gone", "denied", "invalid", "other")

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind if kind in self.KINDS else "other"
        self.status = status


class BoardError(ServiceError):
    """Raised by every TaskBoard."""


class MailError(ServiceError):
    """Raised by every Mailer."""


class AttachmentError(ServiceError):
    """Raised by every AttachmentSource."""


class ConvertError(Exception):
    """A document could not be converted."""


# ---------------------------------------------------------------- types

@dataclass
class Bucket:
    id: str
    name: str


@dataclass
class Task:
    id: str
    plan_id: str
    title: str
    bucket_id: str
    created_by: str
    assignees: List[str] = field(default_factory=list)
    percent_complete: int = 0
    due: Optional[str] = None
    etag: str = ""


@dataclass
class Reference:
    url: str
    alias: str = ""
    kind: str = ""


@dataclass
class TaskDetails:
    description: str = ""
    checklist: List[str] = field(default_factory=list)
    references: List[Reference] = field(default_factory=list)
    etag: str = ""


@dataclass
class Attachment:
    name: str
    url: str
    version: str
    size: int
    content: bytes


# ---------------------------------------------------------------- interfaces

class TaskBoard(Protocol):
    def buckets(self, plan_id: str) -> List[Bucket]: ...

    def create_bucket(self, plan_id: str, name: str) -> Bucket: ...

    def intake_tasks(self, plan_id: str, bucket_id: str) -> List[Task]: ...

    def details(self, task_id: str) -> TaskDetails: ...

    def move(self, task_id: str, bucket_id: str) -> None: ...

    def comment(self, task_id: str, html: str, mention: Optional[str] = None) -> None: ...

    def set_status_note(self, task_id: str, text: str) -> None: ...


class AttachmentSource(Protocol):
    def fetch(self, url: str, max_bytes: int) -> Attachment: ...


class DocumentConverter(Protocol):
    def accepts(self, name: str) -> bool: ...

    def convert(self, path: Path, out_dir: Path) -> List[Path]: ...


class Mailer(Protocol):
    def send(self, recipients, subject: str, body: str) -> None: ...


class TokenSource(Protocol):
    def access_token(self) -> str: ...

    def invalidate(self) -> None: ...


class LinkBuilder(Protocol):
    def file_link(self, path: str, commit: str) -> str: ...

    def folder_link(self, path: str, commit: str) -> str: ...

    def current_link(self, path: str) -> str: ...
