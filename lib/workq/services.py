"""build_services(cfg): the one place that creates Graph, SMTP and file classes.

The queue logic asks this object for a board, a mailer, an attachment keeper and so on,
and gets back something that meets the interface. Tests build the core classes with
fakes instead."""

from pathlib import Path

from .attachments import AttachmentKeeper, GraphAttachmentSource
from .convert import default_converters
from .graph import CallLog, GraphClient, Http, RefreshTokenSource, TokenStore, scopes_for
from .interfaces import MailError
from .mail import AllowedDomainsMailer, CommandMailer, GraphMailer, SmtpMailer
from .planner import CommentBook, GraphTaskBoard, Outbox, RefusedLog, TemplateLinkBuilder


class Services:
    def __init__(self, cfg, now=None):
        self.cfg = cfg
        self.state_dir = Path(cfg["state_dir"])
        self.now = now
        self._made = {}

    def _once(self, name, make):
        if name not in self._made:
            self._made[name] = make()
        return self._made[name]

    def _private_dir(self, name):
        path = self.state_dir / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    # -- Graph

    @property
    def graph_ready(self):
        return bool(self.cfg["graph"].get("configured"))

    def scopes(self):
        p = self.cfg["planner"]
        return scopes_for(planner=p["enabled"], attachments=p["attachments"]["enabled"],
                          mail=self.cfg["digest"].get("route") == "graph")

    @property
    def http(self):
        return self._once("http", lambda: Http(ca_file=self.cfg["graph"].get("ca_file")))

    @property
    def tokens(self):
        g = self.cfg["graph"]
        return self._once("tokens", lambda: RefreshTokenSource(
            TokenStore(g["token_file"]), g["tenant"], g["client_id"], self.scopes(), self.http,
            authority=g["authority"]))

    @property
    def client(self):
        g = self.cfg["graph"]
        return self._once("client", lambda: GraphClient(
            self.tokens, self.http, endpoint=g["endpoint"],
            log=CallLog(self._private_dir("graph") / "calls.log")))

    # -- Planner

    @property
    def plans(self):
        return self.cfg["planner"]["plans"]

    @property
    def board(self):
        return self._once("board", lambda: GraphTaskBoard(self.client))

    @property
    def links(self):
        return self._once("links", lambda: TemplateLinkBuilder(self.cfg["planner"]["links"], self.cfg["branch"]))

    @property
    def book(self):
        p = self.cfg["planner"]
        return self._once("book", lambda: CommentBook(
            self.links, summaries=p["comments"]["summaries"], quiet=p["comments"]["quiet"],
            mention=p.get("mention"), enabled=p["comments"]["enabled"]))

    @property
    def outbox(self):
        return self._once("outbox", lambda: Outbox(self._private_dir("planner") / "outbox.jsonl"))

    @property
    def refused(self):
        return self._once("refused", lambda: RefusedLog(self._private_dir("planner") / "refused.txt"))

    @property
    def keeper(self):
        a = self.cfg["planner"]["attachments"]
        if not (self.cfg["planner"]["enabled"] and a["enabled"]):
            return None
        return self._once("keeper", lambda: AttachmentKeeper(
            GraphAttachmentSource(self.client), default_converters(work_dir=str(self._private_dir("tmp"))),
            self._private_dir("attachments"), max_file_mb=a["max_file_mb"], max_item_mb=a["max_item_mb"],
            keep_days=a["keep_days"]))

    # -- mail

    def route(self):
        d = self.cfg["digest"]
        route = d.get("route") or "auto"
        if route == "auto":
            route = "smtp" if (d.get("smtp") or {}).get("host") else "command"
        return route

    def mailer(self):
        d = self.cfg["digest"]
        route = self.route()
        if route == "graph":
            if not self.graph_ready:
                raise MailError("invalid", "digest.route is graph but the graph section is not set up")
            inner = GraphMailer(self.client)
        elif route == "smtp":
            inner = SmtpMailer(d["smtp"], d["from"], self.now)
        else:
            inner = CommandMailer(d["send_command"], d["from"], self.now)
        return AllowedDomainsMailer(inner, d.get("allowed_domains"))


def build_services(cfg, now=None):
    return Services(cfg, now=now)
