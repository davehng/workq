"""workq: a git-backed work queue run by scheduled Claude Code agents.

Commands
  workq init                 put the queue skeleton into an empty remote repo
  workq add TITLE [-f FILE]  add a brief to inbox/
  workq run STAGE            run one stage once (file, shape, exec-haiku,
                             exec-sonnet, exec-opus, check)
  workq night [--until HH:MM]  run the overnight shift
  workq digest [--stdout]    build the digest: email it, write it to the
                             queue repo, or both
  workq status               show what is in each folder
  workq pause | resume       add or remove the PAUSE file
  workq doctor [--live]      check this machine is set up to run workq
  workq schedule show|on|off set up the timers without root (systemd user
                             timers, or cron when those can't be used)
  workq graph login|logout   sign in to Microsoft Graph as the service account
  workq planner setup|status|sync|intake
                             the Planner link: make buckets, show state, send
                             waiting updates now, take tasks in now

The agents only read and write files. This script does every git operation
and every move between folders.
"""

import argparse
import copy
import datetime as dt
import email.utils
import fcntl
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import smtplib
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from email.message import EmailMessage
from pathlib import Path

from . import VERSION

PKG_DIR = Path(__file__).resolve().parents[2]      # the folder holding bin/, lib/, template/
_VENDOR = str(PKG_DIR / "lib" / "vendor")
try:
    if os.environ.get("WORKQ_BUNDLED_YAML"):
        raise ImportError("bundled copy asked for")
    import yaml
    YAML_SOURCE = "system PyYAML"
except ImportError:  # no PyYAML on this machine: use the copy shipped in lib/vendor/
    sys.path.insert(0, _VENDOR)
    import yaml
    YAML_SOURCE = "bundled PyYAML"

from .attachments import refs_yaml_entries, remove_refs  # noqa: E402
from .graph import DEFAULT_AUTHORITY, DEFAULT_ENDPOINT, GraphError, check_base_url  # noqa: E402
from .interfaces import BoardError, ServiceError  # noqa: E402
from .mail import check_addresses  # noqa: E402
from .planner import BUCKET_KEYS, DEFAULT_BUCKETS, Intake, PlannerSync  # noqa: E402
from .services import build_services  # noqa: E402

STAGES = ["file", "shape", "exec-haiku", "exec-sonnet", "exec-opus", "check"]
TIERS = ["haiku", "sonnet", "opus"]
SOURCE = {
    "shape": "inbox",
    "exec-haiku": "ready/haiku",
    "exec-sonnet": "ready/sonnet",
    "exec-opus": "ready/opus",
    "check": "check",
}
# Files each kind of stage may write inside the item folder.
OWNS = {
    "shape": ["plan.md", "questions.md"],
    "execute": ["result.md", "out/"],
    "check": ["check.md"],
}
BASE_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit"]
WEB_TOOLS = ["WebSearch", "WebFetch"]
DATED = re.compile(r"^\d{4}-\d{2}-\d{2}-")
# Failures that are the setup's fault, not the item's: stop the shift, keep the attempt.
# A failed run with no JSON result means Claude never started (bad flag, missing
# binary, sudo refused). Sign-in errors come back as JSON, so match those by text.
BROKEN_TEXT = re.compile(
    r"invalid api key|authentication_error|not logged in|please run /login|oauth token (has )?expired",
    re.I,
)
LIMIT_TEXT = re.compile(
    r"usage limit|limit reached|hit your limit|rate.?limit|out of (extra )?usage|"
    r"too many requests|overloaded",
    re.I,
)

DEFAULTS = {
    "repo_url": None,
    "branch": "main",
    "state_dir": "/var/lib/workq",
    "web_url": "",
    "timezone": None,
    "window": {"start": "22:00", "end": "06:00"},
    "max_attempts": 3,
    "max_output_mb": 20,
    "max_text_mb": 2,
    "lock_wait_s": 30,
    "allow_web": True,
    "bash_allow": [],
    "limit_wait_min": 30,
    "env": {},
    "extra_shifts": [],
    "claude": {
        "bin": "claude",
        "agent_user": None,
        "wrapper": "/opt/workq/bin/agent-claude",
        "restricted": True,
        "strict_mcp": True,
        "extra_args": [],
        # The rest apply in single-user mode (agent_user is null).
        "env_file": None,
        "config_dir": None,
        "settings": None,
        "sandbox": "auto",
        "sandbox_binds": [],
        "pass_env": [],
    },
    "models": {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus"},
    "stages": {
        "file": {"timeout_min": 2},
        "shape": {"tier": "opus", "timeout_min": 15, "max_turns": 30, "budget_usd": 2.0},
        "exec-haiku": {"tier": "haiku", "timeout_min": 20, "max_turns": 30, "budget_usd": 0.5},
        "exec-sonnet": {"tier": "sonnet", "timeout_min": 45, "max_turns": 60, "budget_usd": 3.0},
        "exec-opus": {"tier": "opus", "timeout_min": 60, "max_turns": 80, "budget_usd": 8.0},
        "check": {"tier": "opus", "timeout_min": 20, "max_turns": 30, "budget_usd": 2.0},
    },
    "caps": {
        "haiku": {"runs": 40, "usd": 5.0},
        "sonnet": {"runs": 20, "usd": 20.0},
        "opus": {"runs": 15, "usd": 30.0},
    },
    "digest": {
        "to": None,                # empty = no email
        "allowed_domains": [],     # mail goes only to addresses in these domains; empty = no mail
        "repo_file": "auto",       # auto = DIGEST.md in the queue repo when there is no email
        "route": "auto",           # auto (smtp when smtp.host is set, else send_command), smtp, command, graph
        "from": "workq@localhost",
        "subject": "workq digest",
        "at": "06:30",
        "send_command": "msmtp -t",
        "smtp": {"host": None, "port": 587, "starttls": True, "ssl": False,
                 "user": None, "password_file": None},
    },
    "git": {"author_name": "workq", "author_email": "workq@localhost",
            "ssh_key": None, "credential_file": None},
    "graph": {"tenant": None, "client_id": None, "token_file": None, "ca_file": None,
              "authority": DEFAULT_AUTHORITY, "endpoint": DEFAULT_ENDPOINT},
    "planner": {
        "enabled": False,
        "plans": [],
        "accept_from": [],
        "mention": None,
        "comments": {"enabled": True, "quiet": False, "summaries": False},
        "links": {"file": None, "folder": None, "current": None},
        "task_link": "https://tasks.office.com/{tenant}/Home/Task/{task}",
        "attachments": {"enabled": True, "max_file_mb": 20, "max_item_mb": 50, "keep_days": 30},
    },
}

# Environment passed to Claude in single-user mode. Everything else is dropped,
# so an agent never inherits things like SSH_AUTH_SOCK or cloud credentials.
ENV_KEEP = {
    "HOME", "PATH", "LANG", "LANGUAGE", "TZ", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
}
ENV_KEEP_PREFIX = ("LC_", "ANTHROPIC_", "CLAUDE")
CREDENTIAL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                   "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


# ---------------------------------------------------------------- helpers


class WorkqError(Exception):
    pass


def redact(text):
    """Hide a password or token written inside a URL (https://user:token@host/...)."""
    return re.sub(r"(\w+://)[^/@\s]+@", r"\1***@", str(text))


TEXT_CAP = 1_000_000


def read_text(path, cap=TEXT_CAP):
    """Up to `cap` bytes of a text file from a clone. Never reads through a symlink."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return ""
    with open(path, "rb") as fh:
        return fh.read(cap).decode("utf-8", errors="replace")


def write_file(path, text, append=False):
    """Write a file in a clone, replacing a symlink rather than writing through it."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
    with open(path, "a" if append else "w") as fh:
        fh.write(text)


class NoAliasLoader(yaml.SafeLoader):
    """Safe YAML without aliases, which let a tiny file expand to gigabytes."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted")
        return super().compose_node(parent, index)


def load_yaml(text):
    return yaml.load(text, Loader=NoAliasLoader)


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deep_merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def default_config_path():
    """$WORKQ_CONFIG, else ~/.config/workq/config.yaml if present, else /etc/workq/config.yaml."""
    if os.environ.get("WORKQ_CONFIG"):
        return os.environ["WORKQ_CONFIG"]
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    user = Path(base) / "workq" / "config.yaml"
    return str(user) if user.exists() else "/etc/workq/config.yaml"


def expand(path):
    """Expand ~ and $VARS in a path from the config."""
    if not path:
        return path
    return os.path.expanduser(os.path.expandvars(str(path)))


DIGEST_FILE = "DIGEST.md"
_RESERVED_NAMES = {"claude.md", "readme.md", "pause", "inbox", "ready", "check", "review",
                   "blocked", "archive", "prompts"}


def digest_file_setting(value):
    """digest.repo_file as "auto", None (never), or a file name at the top of the queue repo."""
    if value is None or value is False or str(value).strip().lower() in ("", "off", "never", "none"):
        return None
    if value is True or str(value).strip().lower() in ("on", "always"):
        return DIGEST_FILE
    value = str(value).strip()
    if value.lower() == "auto":
        return "auto"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", value) or value.lower() in _RESERVED_NAMES:
        raise WorkqError(f"config: digest.repo_file must be auto, off, or a plain file name such as "
                         f"{DIGEST_FILE} (not a path, and not a name the queue uses): {value!r}")
    return value


_DOMAIN = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}")


def domain_list(value):
    """digest.allowed_domains as a list of lower-case domains. Accepts a list or a
    comma-separated string; a leading @ is dropped."""
    if value is None or value is False:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    domains = []
    for item in items:
        domain = str(item if item is not None else "").strip().lower().lstrip("@").rstrip(".")
        if not domain:
            continue
        if not _DOMAIN.fullmatch(domain):
            raise WorkqError(f"config: digest.allowed_domains: {item!r} is not a domain name "
                             "(write it like company.com.au, with no * or @)")
        if domain not in domains:
            domains.append(domain)
    return domains


def address_list(value):
    """digest.to as one string, for display: a list or a comma-separated string."""
    if value is None or value is False:
        return None
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    joined = ", ".join(str(x).strip() for x in items if x is not None and str(x).strip())
    return joined or None


def check_recipients(cfg):
    """The plain addresses in digest.to, each checked against digest.allowed_domains.
    Returns (addresses, None) or (None, the reason nothing may be sent). All or nothing:
    one address outside the list stops the whole mail."""
    return check_addresses(cfg["digest"].get("to") or "", cfg["digest"].get("allowed_domains") or [])


def digest_file(cfg):
    """The file the digest is written to in the queue repo, or None."""
    value = cfg["digest"]["repo_file"]
    if value == "auto":
        return None if cfg["digest"].get("to") else DIGEST_FILE
    return value


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise WorkqError(f"config not found: {path}")
    cfg = deep_merge(DEFAULTS, yaml.safe_load(path.read_text()) or {})
    if not cfg.get("repo_url"):
        raise WorkqError("config: repo_url is required")
    cfg["_config_path"] = str(path.resolve())
    cfg["repo_url"] = expand(cfg["repo_url"]) if str(cfg["repo_url"]).startswith(("~", "$")) else cfg["repo_url"]
    cfg["state_dir"] = Path(expand(cfg["state_dir"]))
    ccfg = cfg["claude"]
    for key in ("bin", "wrapper", "env_file", "config_dir", "settings"):
        ccfg[key] = expand(ccfg.get(key))
    ccfg["sandbox_binds"] = [expand(x) for x in ccfg.get("sandbox_binds") or []]
    for key in ("ssh_key", "credential_file"):
        cfg["git"][key] = expand(cfg["git"].get(key))
    cfg["digest"]["smtp"] = cfg["digest"].get("smtp") or {}
    cfg["digest"]["to"] = address_list(cfg["digest"].get("to"))
    cfg["digest"]["allowed_domains"] = domain_list(cfg["digest"].get("allowed_domains"))
    cfg["digest"]["repo_file"] = digest_file_setting(cfg["digest"].get("repo_file", "auto"))
    cfg["digest"]["smtp"]["password_file"] = expand(cfg["digest"]["smtp"].get("password_file"))
    # Clock times: YAML reads an unquoted 22:00 as the number 1320, so accept both.
    cfg["window"] = {k: clock(v) for k, v in cfg["window"].items()}
    cfg["digest"]["at"] = clock(cfg["digest"]["at"])
    cfg["extra_shifts"] = [
        {"start": clock(x["start"]), "until": clock(x["until"]), "digest": bool(x.get("digest", True))}
        for x in cfg.get("extra_shifts") or []
    ]
    normalize_graph(cfg)
    normalize_planner(cfg)
    route = str(cfg["digest"].get("route") or "auto").strip().lower()
    if route not in ("auto", "smtp", "command", "graph"):
        raise WorkqError(f"config: digest.route must be auto, smtp, command or graph, not {route!r}")
    if route == "graph" and not cfg["graph"]["configured"]:
        raise WorkqError("config: digest.route is graph, which needs a graph section with tenant and client_id")
    cfg["digest"]["route"] = route
    return cfg


GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_graph(cfg):
    g = cfg["graph"] = deep_merge(DEFAULTS["graph"], cfg.get("graph") or {})
    tenant = str(g.get("tenant") or "").strip()
    client = str(g.get("client_id") or "").strip()
    if bool(tenant) != bool(client):
        raise WorkqError("config: the graph section needs both tenant and client_id")
    g["tenant"], g["client_id"] = tenant or None, client or None
    g["configured"] = bool(tenant and client)
    g["token_file"] = (Path(expand(g["token_file"])) if g.get("token_file")
                       else cfg["state_dir"] / "graph" / "token.json")
    g["ca_file"] = expand(g.get("ca_file")) or None
    try:
        g["authority"] = check_base_url(g.get("authority") or DEFAULT_AUTHORITY, "graph.authority")
        g["endpoint"] = check_base_url(g.get("endpoint") or DEFAULT_ENDPOINT, "graph.endpoint")
    except ValueError as exc:
        raise WorkqError(f"config: {exc}")


def _check_template(label, value, needs):
    if not value:
        raise WorkqError(f"config: planner.links.{label} is required when planner.enabled is true")
    value = str(value)
    for key in needs:
        if "{" + key + "}" not in value:
            raise WorkqError(f"config: planner.links.{label} must contain {{{key}}}")
    try:
        sample = value.format(path="x", commit="0", branch="main")
        check_base_url(sample.split("?")[0], f"planner.links.{label}")
    except (KeyError, IndexError, ValueError) as exc:
        raise WorkqError(f"config: planner.links.{label}: {exc}")
    return value


def normalize_planner(cfg):
    p = cfg["planner"] = deep_merge(DEFAULTS["planner"], cfg.get("planner") or {})
    p["enabled"] = bool(p.get("enabled"))
    p["comments"] = deep_merge(DEFAULTS["planner"]["comments"], p.get("comments") or {})
    for key in ("enabled", "quiet", "summaries"):
        p["comments"][key] = bool(p["comments"].get(key))
    p["attachments"] = deep_merge(DEFAULTS["planner"]["attachments"], p.get("attachments") or {})
    p["attachments"]["enabled"] = bool(p["attachments"].get("enabled"))
    for key in ("max_file_mb", "max_item_mb", "keep_days"):
        try:
            p["attachments"][key] = float(p["attachments"][key])
        except (TypeError, ValueError):
            raise WorkqError(f"config: planner.attachments.{key} must be a number")
    p["links"] = p.get("links") or {}
    if not p["enabled"]:
        p["plans"] = []
        return
    if not cfg["graph"]["configured"]:
        raise WorkqError("config: planner.enabled needs a graph section with tenant and client_id")
    plans = []
    for raw in p.get("plans") or []:
        if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
            raise WorkqError("config: each entry in planner.plans needs an id")
        given = raw.get("buckets") or {}
        unknown = sorted(set(given) - set(BUCKET_KEYS))
        if unknown:
            raise WorkqError(f"config: planner.plans buckets: unknown key(s) {', '.join(unknown)}; "
                             f"the keys are {', '.join(BUCKET_KEYS)}")
        buckets = dict(DEFAULT_BUCKETS)
        buckets.update({k: str(v).strip() for k, v in given.items() if v and str(v).strip()})
        if len(set(buckets.values())) != len(BUCKET_KEYS):
            raise WorkqError("config: the six bucket names of a plan must all be different")
        plans.append({"id": str(raw["id"]).strip(), "name": str(raw.get("name") or raw["id"]).strip(),
                      "buckets": buckets})
    if not plans:
        raise WorkqError("config: planner.plans must list at least one plan id")
    p["plans"] = plans
    accept = p.get("accept_from") or []
    accept = accept if isinstance(accept, list) else [accept]
    accept = [str(x).strip().lower() for x in accept if str(x or "").strip()]
    if not accept:
        raise WorkqError("config: planner.accept_from must list at least one person: an empty list "
                         "would take work from anyone on the board, so it is refused")
    bad = [x for x in accept if not GUID.match(x)]
    if bad:
        raise WorkqError(f"config: planner.accept_from takes Entra object IDs (like "
                         f"6463a5ce-2119-4198-9f2a-628761df4a62), not {bad[0]!r}")
    p["accept_from"] = accept
    mention = str(p.get("mention") or "").strip().lower()
    if mention and not GUID.match(mention):
        raise WorkqError(f"config: planner.mention takes an Entra object ID, not {mention!r}")
    p["mention"] = mention or None
    p["links"] = {
        "file": _check_template("file", p["links"].get("file"), ("path", "commit")),
        "folder": _check_template("folder", p["links"].get("folder"), ("path", "commit")),
        "current": _check_template("current", p["links"].get("current"), ("path", "branch")),
    }


def single_user(cfg):
    """True when Claude runs as the same user as the runner (no agent_user)."""
    return not cfg["claude"].get("agent_user")


def apply_env(cfg):
    """Set the config's env: values for this process, and make ~/.local/bin findable."""
    for key, value in (cfg.get("env") or {}).items():
        if value is None or value == "":
            continue  # a blank line in the config sets nothing
        os.environ[str(key)] = expand(str(value))
    parts = [x for x in os.environ.get("PATH", "/usr/bin:/bin").split(":") if x]
    for extra in (str(Path.home() / ".local" / "bin"), "/usr/local/bin"):
        if extra not in parts:
            parts.append(extra)
    os.environ["PATH"] = ":".join(parts)


def read_env_file(path):
    """KEY=VALUE lines; blank values, comments and a leading `export` are ignored."""
    out = {}
    if not path or not Path(path).is_file():
        return out
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            out[key.strip()] = value
    return out


def now(cfg):
    if cfg.get("timezone"):
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(cfg["timezone"]))
    return dt.datetime.now().astimezone()


def clock(value):
    """Normalise a clock time to "HH:MM". YAML turns an unquoted 22:00 into 1320."""
    if isinstance(value, bool) or value is None:
        raise WorkqError(f"config: bad clock time {value!r}; write it as \"22:00\"")
    if isinstance(value, (int, float)):
        hour, minute = divmod(int(value), 60)
    else:
        try:
            hour, minute = (int(x) for x in str(value).strip().split(":")[:2])
        except ValueError:
            raise WorkqError(f"config: bad clock time {value!r}; write it as \"22:00\"")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise WorkqError(f"config: bad clock time {value!r}; write it as \"22:00\"")
    return f"{hour:02d}:{minute:02d}"


def hhmm(text):
    hour, minute = clock(text).split(":")
    return int(hour), int(minute)


def shift_id(cfg, at=None):
    """Date of the most recent window start: the shift a run belongs to."""
    at = at or now(cfg)
    hour, minute = hhmm(cfg["window"]["start"])
    start = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at < start:
        start -= dt.timedelta(days=1)
    return start.date().isoformat()


def shift_end(cfg, until=None):
    at = now(cfg)
    hour, minute = hhmm(until or cfg["window"]["end"])
    end = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if end <= at:
        end += dt.timedelta(days=1)
    return end


def slugify(text, limit=50):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].strip("-")) or "item"


def file_hash(path):
    """Content hash plus modification time, so a rewrite with the same text still counts."""
    path = Path(path)
    if path.is_file():
        return (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return None


def front_matter(text):
    """Return (dict, body) from a markdown file with optional --- front matter."""
    if text.startswith("---"):
        parts = text.split("\n")
        for i in range(1, min(len(parts), 200)):
            if parts[i].strip() == "---":
                try:
                    data = load_yaml("\n".join(parts[1:i])) or {}
                except yaml.YAMLError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                return data, "\n".join(parts[i + 1:])
    return {}, text


def first_line(text):
    _, body = front_matter(text)
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:200]
    return ""


def title_of(item_dir):
    line = first_line(read_text(item_dir / "brief.md", 20000))
    return line or item_dir.name


def dir_size(path):
    path = Path(path)
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def quiet_stdout():
    """The reader of our output has gone (`workq ... | head`): send the rest nowhere."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def log(msg):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"{stamp} {redact(msg)}", flush=True)
    except BrokenPipeError:
        quiet_stdout()   # keep working: a shift must not die because nobody is reading


# ---------------------------------------------------------------- state files


class State:
    """Runner bookkeeping kept on the VM, outside the repo."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg["state_dir"]
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "locks").mkdir(exist_ok=True)
        (self.dir / "clones").mkdir(exist_ok=True)
        self.secure()

    def secure(self):
        """Keep the runner's bookkeeping private. With a separate agent user, that user
        may pass through this folder to the clone it is given, and nothing more."""
        through = 0o700 if single_user(self.cfg) else 0o710
        wanted = [(self.dir, through), (self.dir / "clones", through), (self.dir / "locks", 0o700)]
        wanted += [(self.dir / name, 0o600)
                   for name in ("ledger.jsonl", "problems.jsonl", "limit_until", "last_digest")]
        wanted += [(path, 0o600) for path in (self.dir / "locks").iterdir()]
        # Sign-in token, Planner outbox and attachment copies: the runner's alone, in both modes.
        wanted += [(self.dir / name, 0o700) for name in ("graph", "planner", "attachments", "tmp")
                   if (self.dir / name).is_dir()]
        for path, mode in wanted:
            try:
                if not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) != mode:
                    os.chmod(path, mode)
            except OSError:
                pass

    def _append(self, name, record):
        with open(self.dir / name, "a") as fh:
            fh.write(json.dumps(record) + "\n")

    def _read(self, name):
        path = self.dir / name
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def ledger_add(self, **record):
        record.setdefault("at", now(self.cfg).isoformat())
        record.setdefault("shift", shift_id(self.cfg))
        self._append("ledger.jsonl", record)

    def ledger(self):
        return self._read("ledger.jsonl")

    def problem(self, stage, item, text):
        text = redact(text)
        log(f"PROBLEM [{stage}] {item or '-'}: {text}")
        self._append(
            "problems.jsonl",
            {"at": now(self.cfg).isoformat(), "stage": stage, "item": item, "problem": text},
        )

    def problems_since(self, since):
        return [p for p in self._read("problems.jsonl") if p["at"] > since]

    def spend(self, tier, shift=None):
        shift = shift or shift_id(self.cfg)
        rows = [r for r in self.ledger() if r.get("shift") == shift and r.get("tier") == tier]
        return len(rows), sum(float(r.get("cost") or 0) for r in rows)

    def limit_until(self):
        path = self.dir / "limit_until"
        if path.exists():
            try:
                return float(path.read_text().strip())
            except ValueError:
                return 0.0
        return 0.0

    def set_limit_until(self, epoch):
        (self.dir / "limit_until").write_text(str(epoch))

    def last_digest(self):
        path = self.dir / "last_digest"
        return path.read_text().strip() if path.exists() else ""

    def set_last_digest(self, stamp):
        (self.dir / "last_digest").write_text(stamp)


class Lock:
    """A file lock. `with Lock(...) as got:` gives False if someone else holds it,
    after waiting up to `wait` seconds."""

    def __init__(self, state, name, wait=0.0):
        self.path = state.dir / "locks" / f"{name}.lock"
        self.wait = float(wait)
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.time() >= deadline:
                    self.fh.close()
                    self.fh = None
                    return False
                time.sleep(0.2)

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


# ---------------------------------------------------------------- git


class Repo:
    def __init__(self, cfg, path):
        self.cfg = cfg
        self.path = Path(path)
        self.branch = cfg["branch"]
        self.removed_links = []
        self.env = dict(os.environ)
        self.env.update(
            GIT_AUTHOR_NAME=cfg["git"]["author_name"],
            GIT_AUTHOR_EMAIL=cfg["git"]["author_email"],
            GIT_COMMITTER_NAME=cfg["git"]["author_name"],
            GIT_COMMITTER_EMAIL=cfg["git"]["author_email"],
            GIT_TERMINAL_PROMPT="0",
        )
        # Never run hooks or helper programs named in a clone an agent has worked in.
        self.base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
        if cfg["git"].get("ssh_key"):
            self.env["GIT_SSH_COMMAND"] = (
                "ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
                % shlex.quote(cfg["git"]["ssh_key"])
            )
        if cfg["git"].get("credential_file"):
            # HTTPS with a token: one line, https://anything:TOKEN@host, in a file only you can read.
            self.base += ["-c", "credential.helper=", "-c",
                          "credential.helper=store --file=" + shlex.quote(cfg["git"]["credential_file"])]

    def git(self, *args, check=True, cwd=None):
        proc = subprocess.run(
            [*self.base, *args],
            cwd=cwd or self.path,
            env=self.env,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise WorkqError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc

    def sync(self):
        gitdir = self.path / ".git"
        if gitdir.is_dir() and not gitdir.is_symlink():
            # Left behind by a run that was killed: a clone still open to the agent
            # user, and a .git that is still read-only.
            self.close_for_agent()
            self.lock_git(False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() or self.path.is_symlink():
                force_rmtree(self.path)
            self.git("clone", "--quiet", "--branch", self.branch, self.cfg["repo_url"],
                     str(self.path), cwd=self.path.parent)
            self.git("config", "core.quotePath", "false")
        # 0.1 and 0.2.0 made .git group-writable in two-user mode. Nothing needs that.
        self.git("config", "--unset-all", "core.sharedRepository", check=False)
        self.git("fetch", "--quiet", "origin", self.branch)
        self.git("reset", "--quiet", "--hard", f"origin/{self.branch}")
        self.git("clean", "-qfdx")
        self.exclude_refs()
        # The queue has no use for symbolic links, and the runner must never read or
        # write through one, so any that arrive are deleted here.
        self.removed_links = self.strip_symlinks()

    REFS_EXCLUDE = ("/inbox/*/refs/", "/ready/*/*/refs/", "/check/*/refs/")

    def exclude_refs(self):
        """Attachment copies are put in <item>/refs/ for one run: git must never see them."""
        path = self.path / ".git" / "info" / "exclude"
        path.parent.mkdir(exist_ok=True)
        have = path.read_text() if path.is_file() else ""
        missing = [p for p in self.REFS_EXCLUDE if p not in have.splitlines()]
        if missing:
            text = have.rstrip("\n") + ("\n" if have.strip() else "")
            text += "# workq: attachment copies, there only while a stage runs\n" + "\n".join(missing) + "\n"
            path.write_text(text)

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def strip_symlinks(self):
        """Delete every symbolic link in the working files. Returns their paths."""
        found = []
        for root, dirs, files in os.walk(self.path):
            if root == str(self.path) and ".git" in dirs:
                dirs.remove(".git")
            for name in list(dirs) + files:
                full = os.path.join(root, name)
                if os.path.islink(full):
                    try:
                        os.unlink(full)
                        found.append(os.path.relpath(full, self.path))
                    except OSError:
                        pass
                    if name in dirs:
                        dirs.remove(name)
        return sorted(found)

    def open_for_agent(self):
        """Two-user mode: let the agent user's group read this clone and write its
        working files (never .git) for the length of one run."""
        for root, dirs, files in os.walk(self.path):
            in_git = root == str(self.path / ".git") or root.startswith(str(self.path / ".git") + os.sep)
            for path, is_dir in [(root, True)] + [(os.path.join(root, f), False) for f in files]:
                if os.path.islink(path):
                    continue
                try:
                    mode = stat.S_IMODE(os.lstat(path).st_mode)
                    if is_dir:
                        mode = (mode | 0o050) if in_git else 0o2770
                    else:
                        mode = (mode | 0o040) if in_git else (mode | 0o060)
                    os.chmod(path, mode)
                except OSError:
                    pass  # a file the agent made in an earlier run: already its own

    def close_for_agent(self):
        """Shut the clone to everyone but the runner."""
        try:
            os.chmod(self.path, 0o700)
        except OSError:
            pass

    def lock_git(self, locked):
        """Make .git read-only while an agent works in the clone, then writable again."""
        gitdir = self.path / ".git"
        paths = [str(gitdir)]
        for root, dirs, files in os.walk(gitdir):
            paths += [os.path.join(root, name) for name in dirs + files]
        for path in paths:
            if os.path.islink(path):
                continue
            try:
                mode = stat.S_IMODE(os.lstat(path).st_mode)
                os.chmod(path, (mode & ~0o222) if locked else (mode | 0o200))
            except OSError:
                pass

    def git_fingerprint(self):
        """Identity of .git and a hash of everything in it that decides what git runs
        or what gets pushed: config, HEAD, the index, every ref and every hook."""
        gitdir = self.path / ".git"
        try:
            st = os.lstat(gitdir)
        except OSError:
            return None
        files = [gitdir / name for name in
                 ("config", "HEAD", "index", "packed-refs", "info/exclude", "info/attributes")]
        for sub in ("refs", "hooks"):
            for root, _dirs, names in os.walk(gitdir / sub):
                files += [Path(root) / name for name in names]
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(str(path.relative_to(gitdir)).encode() + b"\0")
            if path.is_symlink():
                digest.update(b"link:" + os.readlink(path).encode())
            elif path.is_file():
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        return (st.st_ino, st.st_dev, stat.S_ISDIR(st.st_mode), digest.hexdigest())

    def changes(self):
        """Paths that differ from HEAD, including untracked files."""
        proc = self.git("status", "--porcelain=v1", "-z", "-uall")
        entries = proc.stdout.split("\0")
        out = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if not entry:
                continue
            code, path = entry[:2], entry[3:]
            out.append((code, path))
            if code[0] in "RC":  # rename/copy carries the old path next
                out.append((code, entries[i]))
                i += 1
        return out

    def revert(self, path):
        full = self.path / path
        if full.is_symlink():
            full.unlink()  # first, so nothing below follows it
        tracked = self.git("ls-files", "--error-unmatch", "--", path, check=False).returncode == 0
        if tracked:
            self.git("checkout", "HEAD", "--", path)
        elif full.is_dir():
            force_rmtree(full)
        elif full.exists():
            full.unlink()

    def commit(self, message):
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.git("commit", "--quiet", "-m", message)
        return True

    def push(self):
        for _ in range(4):
            if self.git("push", "--quiet", "origin", f"HEAD:{self.branch}", check=False).returncode == 0:
                return True
            pull = self.git("pull", "--quiet", "--rebase", "origin", self.branch, check=False)
            if pull.returncode != 0:
                self.git("rebase", "--abort", check=False)
                return False
        return False

    def move(self, src, dst):
        """git mv, adding a suffix if the destination already exists."""
        dst_path = self.path / dst
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        n = 2
        base = dst
        while dst_path.exists():
            dst = f"{base}-{n}"
            dst_path = self.path / dst
            n += 1
        self.git("mv", src, dst)
        return dst


# ---------------------------------------------------------------- items


COUNTERS = ("shape_attempts", "execute_attempts", "check_attempts", "check_fails", "runs", "unfinished")


def load_meta(item_dir):
    """Read meta.yaml. A missing, broken or hand-mangled file gives clean defaults."""
    try:
        data = load_yaml(read_text(item_dir / "meta.yaml")) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("id", item_dir.name)
    data.setdefault("title", title_of(item_dir))
    data.setdefault("tier", None)
    data.setdefault("status", "active")
    if not isinstance(data.get("history"), list):
        data["history"] = []
    for key in COUNTERS:
        data[key] = max(0, to_int(data.get(key)))
    try:
        data["cost_usd"] = float(data.get("cost_usd") or 0)
    except (TypeError, ValueError):
        data["cost_usd"] = 0.0
    return data


def save_meta(item_dir, meta):
    write_file(item_dir / "meta.yaml", yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))


def note_move(cfg, meta, src, dst, note):
    meta["history"].append(
        {"at": now(cfg).isoformat(timespec="seconds"), "from": src, "to": dst, "note": note}
    )


def list_items(repo, folder):
    base = repo.path / folder
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def tidy_inbox(cfg, repo, state):
    """Turn loose briefs into dated item folders with meta.yaml."""
    inbox = repo.path / "inbox"
    if not inbox.exists():
        return
    today = now(cfg).date().isoformat()
    changed = False
    for entry in sorted(inbox.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_file():
            if entry.suffix.lower() not in (".md", ".txt"):
                continue
            name = f"{today}-{slugify(entry.stem)}"
            folder, n = inbox / name, 2
            while folder.exists():
                folder, n = inbox / f"{name}-{n}", n + 1
            folder.mkdir()
            repo.git("mv", f"inbox/{entry.name}", f"inbox/{folder.name}/brief.md")
            entry = folder
            changed = True
        if not (entry / "brief.md").exists():
            state.problem("shape", entry.name, "inbox folder has no brief.md; skipped")
            continue
        if not DATED.match(entry.name):
            new = repo.move(f"inbox/{entry.name}", f"inbox/{today}-{slugify(entry.name)}")
            entry = repo.path / new
            changed = True
        if not (entry / "meta.yaml").exists():
            meta = load_meta(entry)
            note_move(cfg, meta, None, "inbox", "added")
            save_meta(entry, meta)
            changed = True
    if changed and repo.commit("workq: tidy inbox"):
        if not repo.push():
            raise WorkqError("push failed while tidying inbox")


def is_paused(repo):
    return (repo.path / "PAUSE").exists()


# ---------------------------------------------------------------- claude


def stage_kind(stage):
    return "execute" if stage.startswith("exec-") else stage


def stage_tools(cfg, kind, plan=None):
    """Tool names and allow-list entries for a stage, clamped to its ceiling."""
    web = WEB_TOOLS if cfg["allow_web"] else []
    bash = [f"Bash({p})" for p in cfg["bash_allow"]]
    if kind == "shape":
        ceiling = BASE_TOOLS
    elif kind == "check":
        ceiling = BASE_TOOLS + ([t for t in web if t == "WebFetch"])
    else:
        ceiling = BASE_TOOLS + web + bash
    if kind == "execute" and plan and isinstance(plan.get("tools"), list):
        asked = [str(t) for t in plan["tools"]]
        allowed = [t for t in asked if t in ceiling]
        for need in ("Read", "Write"):
            if need not in allowed:
                allowed.append(need)
    else:
        allowed = list(ceiling)
    names = []
    for entry in allowed:
        name = entry.split("(")[0]
        if name not in names:
            names.append(name)
    return names, allowed


def clamp_number(value, ceiling):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ceiling
    return max(0.0, min(value, float(ceiling)))


def parse_reset(text, cfg):
    """Best guess at when a usage limit resets, as an epoch."""
    match = re.search(r"\|(\d{10})\b", text)
    if match:
        return float(match.group(1))
    match = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if match:
        hour = int(match.group(1)) % 12 if match.group(3) else int(match.group(1))
        if match.group(3) and match.group(3).lower() == "pm":
            hour += 12
        minute = int(match.group(2) or 0)
        if hour <= 23 and minute <= 59:
            # Claude is started with TZ set to the config's zone, so its clock times
            # and now(cfg) agree.
            at = now(cfg)
            reset = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reset <= at:
                reset += dt.timedelta(days=1)
            return reset.timestamp()
    return time.time() + float(cfg["limit_wait_min"]) * 60


def claude_env(cfg):
    """Environment for the Claude process."""
    ccfg = cfg["claude"]
    if not single_user(cfg):
        env = dict(os.environ)  # sudo resets it; the wrapper loads the agent's own file
        if cfg.get("timezone"):
            env["TZ"] = cfg["timezone"]
        return env
    keep = set(ccfg.get("pass_env") or []) | {str(k) for k in (cfg.get("env") or {})}
    env = {
        k: v for k, v in os.environ.items()
        if k in keep or k.upper() in ENV_KEEP or k.startswith(ENV_KEEP_PREFIX)
    }
    if cfg.get("timezone"):
        env["TZ"] = cfg["timezone"]
    env.update(read_env_file(ccfg.get("env_file")))
    if ccfg.get("config_dir"):
        os.makedirs(ccfg["config_dir"], mode=0o700, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = ccfg["config_dir"]
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    return env


def settings_args(cfg):
    """--settings with the deny rules file, passed as text so the file can stay hidden."""
    path = cfg["claude"].get("settings")
    if not path:
        return []
    try:
        return ["--settings", json.dumps(json.loads(Path(path).read_text()))]
    except (OSError, ValueError) as exc:
        raise WorkqError(f"claude.settings: cannot use {path}: {exc}")


def force_rmtree(path):
    """Delete a folder tree even when parts of it were made read-only."""
    for root, _dirs, _files in os.walk(path):
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def claude_exe(cfg):
    """Full path of the Claude Code program in single-user mode, or None."""
    return shutil.which(cfg["claude"]["bin"])


_BWRAP_WORKS = None
# Its own user, process and IPC namespaces: from inside, no process outside the
# sandbox can be seen or reached, so /proc gives no way back to the hidden files.
BWRAP_ISOLATE = ["--unshare-user-try", "--unshare-pid", "--unshare-ipc"]


def bwrap_works():
    global _BWRAP_WORKS
    if _BWRAP_WORKS is None:
        exe = shutil.which("bwrap")
        _BWRAP_WORKS = False
        if exe:
            try:
                probe = subprocess.run(
                    [exe, *BWRAP_ISOLATE, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                    capture_output=True, timeout=20,
                )
                _BWRAP_WORKS = probe.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
    return _BWRAP_WORKS


def sandbox_mode(cfg):
    """"bwrap" or "off" for this machine and config."""
    want = cfg["claude"].get("sandbox", "auto")
    want = "auto" if want is True else "off" if want is False else str(want).lower()
    if not single_user(cfg) or want in ("off", "none"):
        return "off"
    if bwrap_works():
        return "bwrap"
    if want == "bwrap":
        raise WorkqError("claude.sandbox is bwrap, but bubblewrap is missing or not allowed to run here")
    return "off"


def sandbox_wrap(cfg, workdir, cmd):
    """Wrap a command in bubblewrap. Inside, the whole system is read-only, and the
    home folder, /tmp and the workq config and state are empty, apart from the
    clone being worked on (writable, but with .git read-only) and Claude's own files."""
    ccfg = cfg["claude"]
    hidden = [str(Path.home()), "/tmp", "/var/tmp", f"/run/user/{os.getuid()}"]
    for extra in (str(cfg["state_dir"]), str(Path(cfg["_config_path"]).parent)):
        if not any(extra == h or extra.startswith(h + "/") for h in hidden):
            hidden.append(extra)
    hidden = [h for h in hidden if os.path.isdir(h)]

    def is_hidden(path):
        return any(path == h or path.startswith(h + "/") for h in hidden)

    args = [shutil.which("bwrap"), *BWRAP_ISOLATE, "--die-with-parent", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc"]
    for path in hidden:
        args += ["--tmpfs", path]
    exe_dir = str(Path(cmd[0]).resolve().parent)
    for path in [exe_dir, *ccfg.get("sandbox_binds", [])]:
        if is_hidden(path) and os.path.exists(path):
            args += ["--ro-bind", path, path]
    if ccfg.get("config_dir"):
        args += ["--bind", ccfg["config_dir"], ccfg["config_dir"]]
    workdir = str(workdir)
    args += ["--bind", workdir, workdir, "--ro-bind", workdir + "/.git", workdir + "/.git",
             "--chdir", workdir, "--"]
    return args + cmd


_LIBC = None


def _stop_with_runner():
    """Runs in the child just before Claude starts: ask the kernel to stop it if the
    runner dies, so a killed runner doesn't leave an agent working unwatched."""
    try:
        _LIBC.prctl(1, int(signal.SIGTERM), 0, 0, 0)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def stop_child(proc):
    """End Claude and everything it started. Returns what it had printed."""
    for sig, patience in ((signal.SIGTERM, 15), (signal.SIGKILL, 30)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            return proc.communicate(timeout=patience)
        except subprocess.TimeoutExpired:
            continue
        except (OSError, ValueError):
            break
    return "", ""


def run_claude(cfg, repo, stage, item_rel, model, tools, max_turns, budget, timeout_s, note=""):
    """Run one headless Claude Code session. Returns a dict describing the run."""
    global _LIBC
    kind = stage_kind(stage)
    prompt_file = repo.path / "prompts" / f"{kind}.md"
    stage_prompt = read_text(prompt_file)
    if not stage_prompt.strip():
        raise WorkqError(f"missing or empty prompt file prompts/{kind}.md in the queue repo")
    if _LIBC is None:
        try:
            import ctypes
            _LIBC = ctypes.CDLL(None)
        except Exception:
            _LIBC = False
    names, allowed = tools
    ccfg = cfg["claude"]
    cmd = []
    if ccfg.get("agent_user"):
        # The wrapper starts Claude itself; set CLAUDE_BIN in agent.env to change which one.
        cmd += ["sudo", "-n", "-H", "-u", ccfg["agent_user"], ccfg["wrapper"]]
    else:
        exe = claude_exe(cfg)
        # Run the real file, so a sandbox only has to show Claude its own folder.
        cmd.append(str(Path(exe).resolve()) if exe else ccfg["bin"])
    cmd += [
        "-p",
        "--model", model,
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--max-turns", str(int(max_turns)),
        "--max-budget-usd", f"{budget:.2f}",
        "--append-system-prompt", stage_prompt,
    ]
    if ccfg.get("restricted"):
        cmd.append("--restricted")
    if ccfg.get("strict_mcp"):
        cmd.append("--strict-mcp-config")
    cmd += settings_args(cfg)
    cmd += [str(a) for a in ccfg.get("extra_args") or []]
    # Variadic flags go last; the prompt goes in on stdin so they can't swallow it.
    cmd += ["--tools", ",".join(names)]
    cmd += ["--allowedTools", *allowed]
    prompt = (
        f"Stage: {kind}\n"
        f"Work item folder: {item_rel}\n\n"
        f"Do the {kind} stage for this one work item, following the stage instructions "
        f"and CLAUDE.md. Write only the files this stage owns, inside that folder."
        + note
    )
    sandbox = sandbox_mode(cfg)
    if sandbox == "bwrap":
        cmd = sandbox_wrap(cfg, repo.path, cmd)
    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=repo.path,
            env=claude_env(cfg),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=_stop_with_runner if _LIBC else None,
        )
    except OSError as exc:
        # Counts as a broken setup: the attempt is given back and the shift stops.
        return {
            "cmd": cmd, "sandbox": sandbox, "broken": True, "returncode": 127, "timed_out": False,
            "seconds": 0.0, "stdout": "", "stderr": f"cannot start Claude Code ({cmd[0]}): {exc}",
            "result": {}, "cost": 0.0, "turns": None, "failed": True, "limited": False,
            "reset_at": None, "summary": "", "error_text": f"cannot start Claude Code ({cmd[0]}): {exc}",
        }
    timed_out = False
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout, stderr = stop_child(proc)
    except BaseException:
        stop_child(proc)  # the runner is being stopped: take the agent down with it
        raise
    result = {}
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not result and (stdout or "").strip().startswith("{"):
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            result = {}
    if not isinstance(result, dict):
        result = {}
    # Claude Code puts some failures (turn limit, budget) in an "errors" list.
    errors = result.get("errors")
    errors = "; ".join(str(e) for e in errors) if isinstance(errors, list) else str(errors or "")
    text = " ".join(
        str(x) for x in (result.get("result", ""), errors, result.get("api_error_status") or "",
                         stderr or "", stdout if not result else "")
    )
    failed = timed_out or proc.returncode != 0 or bool(result.get("is_error"))
    limited = failed and not timed_out and (
        bool(LIMIT_TEXT.search(text)) or result.get("api_error_status") == 429
    )
    broken = failed and not timed_out and not limited and (
        not result or bool(BROKEN_TEXT.search(text)) or result.get("api_error_status") in (401, 403)
    )
    return {
        "cmd": cmd,
        "sandbox": sandbox,
        "broken": broken,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 1),
        "stdout": stdout or "",
        "stderr": stderr or "",
        "result": result,
        "cost": float(result.get("total_cost_usd") or 0),
        "turns": result.get("num_turns"),
        "failed": failed,
        "limited": limited,
        "reset_at": parse_reset(text, cfg) if limited else None,
        "summary": (str(result.get("result") or "") or errors)[:300],
        "error_text": (errors or str(result.get("result") or "") or (stderr or stdout or "").strip())[-300:],
    }


# ---------------------------------------------------------------- stages


def run_stage(cfg, stage):
    """Run one stage once. Returns one of:
    done, idle, paused, busy, capped, limited, broken, error."""
    if stage not in STAGES:
        raise WorkqError(f"unknown stage {stage}; use one of {', '.join(STAGES)}")
    state = State(cfg)
    with Lock(state, stage) as got:
        if not got:
            log(f"[{stage}] already running; skipped")
            return "busy"
        repo = Repo(cfg, state.dir / "clones" / stage)
        try:
            repo.sync()
            if repo.removed_links:
                shown = ", ".join(repo.removed_links[:5])
                if repo.commit("workq: remove symbolic links (not allowed in the queue)") and repo.push():
                    state.problem(stage, None, f"removed symbolic links from the queue repo: {shown}")
            if is_paused(repo):
                log(f"[{stage}] PAUSE file present; skipped")
                return "paused"
            if stage == "file":
                return file_stage(cfg, repo, state)
            if state.limit_until() > time.time():
                return "limited"
            return work_stage(cfg, repo, state, stage)
        except WorkqError as exc:
            state.problem(stage, None, str(exc))
            return "error"
        except Exception as exc:  # a fault in the runner or the repo must not end the shift
            traceback.print_exc()
            state.problem(stage, None, f"unexpected error, stage skipped this pass: "
                                       f"{type(exc).__name__}: {exc}")
            return "error"


# Items that made the runner fail this session. They are passed over until the next
# shift, so one bad item can't hold up everything behind it.
_SET_ASIDE = set()


def block_item(cfg, repo, state, stage, item, meta, reason):
    """Move an item to blocked/ without running anything."""
    src = SOURCE[stage]
    meta.pop("running", None)
    meta["status"] = "blocked"
    meta["blocked_reason"] = reason
    note_move(cfg, meta, src, "blocked", reason)
    save_meta(item, meta)
    repo.move(f"{src}/{item.name}", f"blocked/{item.name}")
    repo.commit(f"workq: {item.name} blocked ({reason})"[:200])
    if not repo.push():
        state.problem(stage, item.name, "push clashed while blocking; will retry next pass")
        return
    log(f"[{stage}] {item.name}: blocked ({reason})")
    rel = f"blocked/{item.name}"
    planner_say(cfg, state, repo, meta, "blocked", rel,
                {"reason": reason, "newest_log": newest_log(repo.path / rel)})


def work_stage(cfg, repo, state, stage):
    kind = stage_kind(stage)
    sandbox_mode(cfg)   # fail here, before anything is claimed, if the setup is wrong
    settings_args(cfg)
    if kind == "shape":
        tidy_inbox(cfg, repo, state)
    items = [p for p in list_items(repo, SOURCE[stage])
             if (p / "brief.md").is_file() and p.name not in _SET_ASIDE]
    if not items:
        return "idle"
    item = items[0]
    try:
        return work_item(cfg, repo, state, stage, item)
    except WorkqError:
        raise
    except Exception as exc:
        traceback.print_exc()
        _SET_ASIDE.add(item.name)
        state.problem(stage, item.name, f"unexpected error, item set aside until the next shift: "
                                        f"{type(exc).__name__}: {exc}")
        return "skipped"


def work_item(cfg, repo, state, stage, item):
    kind = stage_kind(stage)
    item_rel = f"{SOURCE[stage]}/{item.name}"
    scfg = cfg["stages"][stage]
    tier = scfg["tier"]
    cap = cfg["caps"][tier]
    runs, usd = state.spend(tier)
    if runs >= cap["runs"] or usd >= cap["usd"]:
        log(f"[{stage}] {tier} cap reached ({runs} runs, ${usd:.2f}); skipped")
        return "capped"

    meta = load_meta(item)
    attempts_key = f"{kind}_attempts"
    if meta.get("running"):
        # The last run of this item never reported back: the runner was killed, the
        # machine went down, or its result could not be pushed.
        meta["unfinished"] += 1
        if meta["unfinished"] >= int(cfg["max_attempts"]):
            block_item(cfg, repo, state, stage, item, meta,
                       f"{meta['unfinished']} runs ended without finishing (killed, crashed or not pushed)")
            return "done"
    plan = {}
    if kind == "execute":
        plan, _ = front_matter(read_text(item / "plan.md"))
    max_turns = clamp_number(plan.get("max_turns", scfg["max_turns"]), scfg["max_turns"])
    budget = clamp_number(plan.get("budget_usd", scfg["budget_usd"]), scfg["budget_usd"])
    tools = stage_tools(cfg, kind, plan if kind == "execute" else None)

    # Keep the previous check so the history survives the next one.
    if kind == "check" and (item / "check.md").exists():
        (item / "log").mkdir(exist_ok=True)
        n = meta["check_attempts"]
        shutil.copyfile(item / "check.md", item / "log" / f"check-{n}.md")

    # Claim: count the attempt before the run, so a crash still counts.
    meta[attempts_key] += 1
    meta["running"] = {"stage": stage, "at": now(cfg).isoformat(timespec="seconds")}
    save_meta(item, meta)
    repo.commit(f"workq: {item.name} {stage} attempt {meta[attempts_key]}")
    if not repo.push():
        state.problem(stage, item.name, "could not push claim; will retry next pass")
        return "error"
    if kind in ("execute", "check"):
        planner_say(cfg, state, repo, meta, "executing" if kind == "execute" else "checking", item_rel,
                    {"tier": meta.get("tier") or tier, "attempt": meta[attempts_key],
                     "max_attempts": int(cfg["max_attempts"]), "has_out": (item / "out").is_dir()})
    note = place_refs(cfg, state, item, meta, item_rel)

    owned = OWNS[kind]
    before = {name: file_hash(item / name) for name in owned if not name.endswith("/")}
    log(f"[{stage}] {item.name}: running {cfg['models'][tier]} (attempt {meta[attempts_key]})")
    if not single_user(cfg):
        repo.open_for_agent()
    git_before = repo.git_fingerprint()
    repo.lock_git(True)
    try:
        run = run_claude(
            cfg, repo, stage, item_rel, cfg["models"][tier], tools, max_turns, budget,
            float(scfg["timeout_min"]) * 60, note=note,
        )
    finally:
        repo.close_for_agent()
        remove_refs(item)
        git_after = repo.git_fingerprint()
        if git_after == git_before:
            repo.lock_git(False)
    if git_after != git_before:
        # The agent swapped or edited .git. Run no git command in this clone again.
        force_rmtree(repo.path)
        state.ledger_add(stage=stage, tier=tier, item=item.name, cost=run["cost"],
                         turns=run["turns"], outcome="tampered", reason="clone's .git changed")
        state.problem(stage, item.name, "the agent changed the clone's .git folder; the clone was "
                      "thrown away, the run failed and the shift stopped. Read the item's brief "
                      "and files before running again")
        return "broken"

    # Save the raw output outside the repo first, then into the item's log/.
    stamp = now(cfg).strftime("%Y%m%dT%H%M%S")
    raw = {k: run[k] for k in ("returncode", "timed_out", "seconds", "result", "cmd", "sandbox")}
    raw["stderr"] = run["stderr"][-4000:]
    if not run["result"]:
        raw["stdout"] = run["stdout"][-4000:]

    def settle_meta():
        # meta.yaml on disk is the claim version (the guard restores it if touched).
        m = load_meta(item)
        m.pop("running", None)
        m["runs"] += 1
        m["cost_usd"] = round(float(m["cost_usd"]) + run["cost"], 4)
        return m

    if run["broken"]:
        # Claude didn't start or can't sign in: not the item's fault. Undo, give the
        # attempt back, and stop rather than burn every item into blocked/.
        for _, path in repo.changes():
            repo.revert(path)
        meta = settle_meta()
        meta[attempts_key] = max(0, meta[attempts_key] - 1)
        save_meta(item, meta)
        write_log(item, stamp, stage, raw)
        detail = (run["summary"] or run["error_text"] or run["stderr"] or run["stdout"]).strip()[-300:]
        state.ledger_add(stage=stage, tier=tier, item=item.name, cost=run["cost"],
                         turns=run["turns"], outcome="broken", reason=detail)
        state.problem(stage, item.name, f"Claude Code failed to run; shift stopped: {detail}")
        repo.commit(f"workq: {item.name} {stage} could not run claude")
        repo.push()
        return "broken"

    if run["limited"]:
        # Not the item's fault: undo everything and give the attempt back.
        for _, path in repo.changes():
            repo.revert(path)
        meta = settle_meta()
        meta[attempts_key] = max(0, meta[attempts_key] - 1)
        save_meta(item, meta)
        write_log(item, stamp, stage, raw)
        state.set_limit_until(run["reset_at"])
        state.ledger_add(stage=stage, tier=tier, item=item.name, cost=run["cost"],
                         turns=run["turns"], outcome="limited", reason=run["summary"])
        state.problem(stage, item.name, "usage limit hit; stage paused until "
                      + dt.datetime.fromtimestamp(run["reset_at"]).strftime("%H:%M"))
        repo.commit(f"workq: {item.name} {stage} stopped by usage limit")
        repo.push()
        return "limited"

    # Guard: anything outside the stage's own files is thrown away, and so is any
    # symbolic link, wherever it is.
    violations = []
    for _, path in repo.changes():
        if os.path.islink(repo.path / path):
            violations.append(path)
            continue
        if not path.startswith(item_rel + "/"):
            violations.append(path)
            continue
        rel = path[len(item_rel) + 1:]
        if not any(rel == o or (o.endswith("/") and rel.startswith(o)) for o in owned):
            violations.append(path)
    for path in sorted(set(violations)):
        repo.revert(path)
    # git doesn't report what a .gitignore hides, so look for links directly as well.
    violations += [f"{link} (symbolic link)" for link in repo.strip_symlinks()]
    meta = settle_meta()

    outcome, dst, reason = decide(cfg, repo, item, item_rel, kind, meta, before, run, violations)

    write_log(item, stamp, stage, raw)
    if dst and dst != item_rel:
        if dst.startswith("blocked/"):
            meta["status"] = "blocked"
            meta["blocked_reason"] = reason
        note_move(cfg, meta, SOURCE[stage], dst.rsplit("/", 1)[0], reason)
        save_meta(item, meta)
        new_rel = repo.move(item_rel, dst)
        log(f"[{stage}] {item.name}: {outcome} -> {new_rel} ({reason})")
    else:
        save_meta(item, meta)
        new_rel = item_rel
        log(f"[{stage}] {item.name}: {outcome}, stays ({reason})")
    repo.commit(f"workq: {item.name} {stage}: {outcome} ({reason})"[:200])
    if not repo.push():
        state.problem(stage, item.name, "push clashed; result dropped, item left for the next pass")
    else:
        planner_after_run(cfg, state, repo, meta, outcome, reason, new_rel)
    state.ledger_add(stage=stage, tier=tier, item=item.name, cost=run["cost"], turns=run["turns"],
                     outcome=outcome, reason=reason, seconds=run["seconds"])
    if outcome == "failed":  # blocked items have their own section in the digest
        state.problem(stage, item.name, f"failed: {reason}")
    return "done"


def write_log(item, stamp, stage, raw):
    logdir = item / "log"
    logdir.mkdir(exist_ok=True)
    path, n = logdir / f"{stamp}-{stage}.json", 2
    while path.exists() or path.is_symlink():   # two runs in one second: keep both
        path, n = logdir / f"{stamp}-{stage}-{n}.json", n + 1
    write_file(path, json.dumps(raw, indent=2, default=str))


def revert_owned(repo, item_rel):
    """Throw away what a failed run wrote, keeping meta.yaml and log/."""
    for _, path in repo.changes():
        if not path.startswith(item_rel + "/"):
            continue
        rel = path[len(item_rel) + 1:]
        if rel != "meta.yaml" and not rel.startswith("log/"):
            repo.revert(path)


def decide(cfg, repo, item, item_rel, kind, meta, before, run, violations):
    """Work out where the item goes. Returns (outcome, destination or None, reason)."""
    max_attempts = int(cfg["max_attempts"])
    attempts = meta[f"{kind}_attempts"]

    def fail(reason):
        revert_owned(repo, item_rel)
        if attempts >= max_attempts:
            return "blocked", f"blocked/{item.name}", f"{kind} failed {attempts} times; last: {reason}"
        return "failed", None, reason

    if run["timed_out"]:
        return fail("run hit its time limit")
    if violations:
        shown = ", ".join(sorted(set(violations))[:5])
        return fail(f"wrote files it does not own: {shown}")
    if run["failed"]:
        # Claude ended in an error (turn or budget limit, API fault). Whatever it
        # left behind may be half written, so none of it is used.
        return fail(f"claude ended with an error: {run['error_text'].strip() or 'exit %s' % run['returncode']}")
    limit = float(cfg["max_text_mb"]) * 1024 * 1024
    for name in OWNS[kind]:
        path = item / name
        if not name.endswith("/") and path.is_file() and path.stat().st_size > limit:
            return fail(f"{name} is over {cfg['max_text_mb']} MB")

    if kind == "shape":
        q_changed = file_hash(item / "questions.md") != before.get("questions.md")
        p_changed = file_hash(item / "plan.md") != before.get("plan.md")
        if q_changed and (item / "questions.md").exists():
            if p_changed:
                repo.revert(f"{item_rel}/plan.md")
            return "blocked", f"blocked/{item.name}", "shaper has questions"
        if p_changed and (item / "plan.md").exists():
            fm, _ = front_matter(read_text(item / "plan.md"))
            tier = str(fm.get("tier", "")).strip().lower()
            if tier in TIERS:
                meta["tier"] = tier
                return "shaped", f"ready/{tier}/{item.name}", f"plan written, tier {tier}"
            return fail(f"plan.md has no valid tier (got {tier or 'none'})")
        return fail(f"no plan.md or questions.md written ({run['summary'][:120]})")

    if kind == "execute":
        if file_hash(item / "result.md") == before.get("result.md") or not (item / "result.md").exists():
            return fail(f"no new result.md ({run['summary'][:120]})")
        limit = float(cfg["max_output_mb"]) * 1024 * 1024
        if dir_size(item / "out") > limit:
            return fail(f"out/ is over {cfg['max_output_mb']} MB")
        fm, _ = front_matter(read_text(item / "result.md"))
        if fm.get("replan") is True or str(fm.get("replan", "")).lower() == "true":
            if meta["shape_attempts"] >= max_attempts:
                return ("blocked", f"blocked/{item.name}",
                        f"executor asked for a new plan after {meta['shape_attempts']} plans")
            return "replan", f"inbox/{item.name}", "executor asked for a new plan"
        return "executed", f"check/{item.name}", f"result written at {meta.get('tier')}"

    if kind == "check":
        if file_hash(item / "check.md") == before.get("check.md") or not (item / "check.md").exists():
            return fail(f"no new check.md ({run['summary'][:120]})")
        fm, _ = front_matter(read_text(item / "check.md"))
        verdict = str(fm.get("verdict", "")).strip().lower()
        if verdict == "pass":
            meta["status"] = "review"
            return "passed", f"review/{item.name}", "check passed"
        if verdict != "fail":
            return fail(f"check.md verdict must be pass or fail (got {verdict or 'none'})")
        meta["check_fails"] += 1
        tier = meta.get("tier") or "sonnet"
        if meta["execute_attempts"] >= max_attempts:
            return "blocked", f"blocked/{item.name}", f"check failed; {meta['execute_attempts']} execute attempts used"
        if meta["check_fails"] == 1:
            return "check-failed", f"ready/{tier}/{item.name}", f"check failed; retry at {tier}"
        nxt = TIERS.index(tier) + 1 if tier in TIERS else len(TIERS)
        if nxt >= len(TIERS):
            return "blocked", f"blocked/{item.name}", "check failed at opus"
        meta["tier"] = TIERS[nxt]
        return "check-failed", f"ready/{TIERS[nxt]}/{item.name}", f"check failed again; step up to {TIERS[nxt]}"
    raise WorkqError(f"unknown kind {kind}")


VERDICTS = {"review": {"approve", "rework", "reject"}, "blocked": {"retry", "reject", "rework"}}


def read_verdict(path):
    text = read_text(path)
    fm, body = front_matter(text)
    verdict = str(fm.get("verdict", "")).strip().lower()
    if not verdict:
        match = re.search(r"^\s*verdict\s*:\s*(\w+)", text, re.I | re.M)
        if match:
            verdict = match.group(1).lower()
            body = re.sub(r"^\s*verdict\s*:.*$", "", body, count=1, flags=re.I | re.M)
    return verdict, body.strip()


def file_stage(cfg, repo, state):
    did = False
    for folder in ("review", "blocked"):
        for item in list_items(repo, folder):
            if not (item / "review.md").is_file() or item.name in _SET_ASIDE:
                continue
            try:
                outcome = file_item(cfg, repo, state, folder, item)
            except WorkqError:
                raise
            except Exception as exc:
                traceback.print_exc()
                _SET_ASIDE.add(item.name)
                state.problem("file", item.name, f"unexpected error, item set aside until the next "
                                                 f"shift: {type(exc).__name__}: {exc}")
                # Start the next item from a clean copy.
                repo.git("reset", "--quiet", "--hard", f"origin/{repo.branch}")
                repo.git("clean", "-qfdx")
                continue
            if outcome == "error":
                return "error"
            did = did or outcome == "done"
    return "done" if did else "idle"


def file_item(cfg, repo, state, folder, item):
    """Act on one review.md. Returns done, skipped or error."""
    review = item / "review.md"
    if not (item / "meta.yaml").is_file():
        state.problem("file", item.name, f"{folder}/{item.name} is not a work item "
                      "(no meta.yaml); review.md left alone")
        return "skipped"
    verdict, notes = read_verdict(review)
    if verdict not in VERDICTS[folder]:
        state.problem("file", item.name, f"review.md verdict '{verdict}' not valid in {folder}/; "
                      f"use one of {', '.join(sorted(VERDICTS[folder]))}")
        return "skipped"
    meta = load_meta(item)
    stamp = now(cfg).strftime("%Y-%m-%d %H:%M")
    write_file(item / "notes.md", f"\n## Your notes, {stamp} ({verdict})\n\n{notes or '(none)'}\n",
               append=True)
    repo.git("rm", "--quiet", "-f", f"{folder}/{item.name}/review.md")
    item_rel = f"{folder}/{item.name}"
    if verdict in ("rework", "retry"):
        for key in ("shape_attempts", "execute_attempts", "check_attempts", "check_fails", "unfinished"):
            meta[key] = 0
        meta.pop("running", None)
        meta["status"] = "active"
        meta.pop("blocked_reason", None)
        note_move(cfg, meta, folder, "inbox", f"you asked for {verdict}")
        save_meta(item, meta)
        refresh_attachments(cfg, state, item, meta)
        dst = repo.move(item_rel, f"inbox/{item.name}")
    else:
        meta["status"] = "approved" if verdict == "approve" else "rejected"
        month = now(cfg).strftime("%Y-%m")
        note_move(cfg, meta, folder, "archive", f"you chose {verdict}")
        meta["archived"] = now(cfg).date().isoformat()
        save_meta(item, meta)
        dst = repo.move(item_rel, f"archive/{month}/{item.name}")
        add_index_line(repo, meta, dst)
    log(f"[file] {item.name}: {verdict} -> {dst}")
    repo.commit(f"workq: {item.name} {verdict} -> {dst.split('/')[0]}")
    if not repo.push():
        state.problem("file", item.name, "push clashed; will retry next pass")
        return "error"
    if verdict in ("rework", "retry"):
        planner_say(cfg, state, repo, meta, "sent_back", dst, {})
    else:
        planner_say(cfg, state, repo, meta, "archived", dst, {"verdict": verdict})
        if planner_meta(meta) and services(cfg).keeper:
            services(cfg).keeper.mark_archived(meta.get("id") or item.name)
    return "done"


def add_index_line(repo, meta, dst):
    index = repo.path / "archive" / "INDEX.md"
    if not index.is_file():
        index.parent.mkdir(parents=True, exist_ok=True)
        write_file(
            index,
            "# Archive index\n\n"
            "| Archived | Item | Title | Tier | Runs | Est. cost (USD) | Outcome |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
        )
    title = str(meta.get("title", "")).replace("|", "/").replace("\n", " ")
    write_file(
        index,
        f"| {meta.get('archived')} | [{meta['id']}]({dst[len('archive/'):]}) | {title} | "
        f"{meta.get('tier') or '-'} | {meta.get('runs', 0)} | {float(meta.get('cost_usd') or 0):.2f} | "
        f"{meta.get('status')} |\n",
        append=True,
    )


# ---------------------------------------------------------------- Planner glue

_SERVICES = None


def services(cfg):
    """The Graph, Planner, attachment and mail objects for this config (made once)."""
    global _SERVICES
    if _SERVICES is None or _SERVICES.cfg is not cfg:
        _SERVICES = build_services(cfg, now=lambda: now(cfg))
    return _SERVICES


def planner_meta(meta):
    found = meta.get("planner")
    return found if isinstance(found, dict) and found.get("task_id") else None


def planner_say(cfg, state, repo, meta, event, item_rel, info=None):
    """Queue a Planner update for an item that came from a Planner task. Called only
    after the change is pushed, so every link points at something that exists. Never raises."""
    task = planner_meta(meta)
    if not (cfg["planner"]["enabled"] and task):
        return
    try:
        svc = services(cfg)
        built = svc.book.build(event, item_rel, repo.head(), info or {})
        if not built:
            return
        bucket, page, text, mention = built
        svc.outbox.add(kind=event, task=task["task_id"], plan=task.get("plan_id"), item=item_rel,
                       bucket=bucket, html=page, text=text, mention=mention)
    except Exception as exc:  # Planner must never stop the queue
        state.problem("planner", meta.get("id"), f"could not queue a Planner update: {type(exc).__name__}: {exc}")


def count_steps(text):
    _, body = front_matter(text)
    return len(re.findall(r"(?m)^(\d+)[.)]\s+\S", body))


def question_lines(text):
    _, body = front_matter(text)
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    listed = [re.sub(r"^(\d+[.)]|[-*+])\s+", "", l) for l in lines if re.match(r"^(\d+[.)]|[-*+])\s+", l)]
    return listed or [l for l in lines if l.endswith("?")] or lines[:1]


def newest_log(item):
    logs = sorted((Path(item) / "log").glob("*.json")) if (Path(item) / "log").is_dir() else []
    return f"log/{logs[-1].name}" if logs else None


def planner_after_run(cfg, state, repo, meta, outcome, reason, rel):
    if not planner_meta(meta):
        return
    item = repo.path / rel
    if outcome == "shaped":
        planner_say(cfg, state, repo, meta, "planned", rel,
                    {"tier": meta.get("tier"), "steps": count_steps(read_text(item / "plan.md"))})
    elif outcome == "blocked" and reason == "shaper has questions":
        lines = question_lines(read_text(item / "questions.md"))
        planner_say(cfg, state, repo, meta, "questions", rel, {"questions": len(lines), "question_lines": lines})
    elif outcome == "blocked":
        planner_say(cfg, state, repo, meta, "blocked", rel, {"reason": reason, "newest_log": newest_log(item)})
    elif outcome == "replan":
        planner_say(cfg, state, repo, meta, "replan", rel, {})
    elif outcome == "passed":
        planner_say(cfg, state, repo, meta, "review", rel,
                    {"summary": first_line(read_text(item / "result.md", 20000)), "has_out": (item / "out").is_dir()})
    elif outcome == "check-failed":
        planner_say(cfg, state, repo, meta, "check_failed", rel,
                    {"attempt": meta.get("execute_attempts"), "max_attempts": int(cfg["max_attempts"]),
                     "summary": first_line(read_text(item / "check.md", 20000))})


REFS_NOTE = (
    "\n\nFiles attached to this work item are in {rel}/refs/ for this run; refs.yaml in the item "
    "folder lists them. Read them before you start. For Word and PowerPoint files read the .pdf "
    "copy if there is one, else the .md copy; for Excel files read the .csv copies. Do not write "
    "into refs/: anything written there is thrown away when the run ends."
)


def place_refs(cfg, state, item, meta, item_rel):
    """Copy the item's attachments into <item>/refs/ for this run. Returns a line for the prompt."""
    if not (cfg["planner"]["enabled"] and planner_meta(meta) and services(cfg).keeper):
        return ""
    try:
        if services(cfg).keeper.place(meta.get("id") or item.name, item):
            return REFS_NOTE.format(rel=item_rel)
    except OSError as exc:
        state.problem("planner", item.name, f"could not copy the attachments for this run: {exc}")
    return ""


def write_refs_yaml(item, entries):
    if entries:
        write_file(item / "refs.yaml", yaml.safe_dump(refs_yaml_entries(entries), sort_keys=False,
                                                      allow_unicode=True))


def refresh_attachments(cfg, state, item, meta):
    """On retry or rework, fetch the task's attachments again; refs.yaml shows what changed."""
    task = planner_meta(meta)
    if not (task and cfg["planner"]["enabled"] and services(cfg).keeper):
        return
    try:
        details = services(cfg).board.details(task["task_id"])
        if details.references:
            write_refs_yaml(item, services(cfg).keeper.collect(meta.get("id") or item.name, details.references))
    except ServiceError as exc:
        state.problem("file", item.name, f"kept the old attachments; could not fetch them again: {exc}")


class QueueAdapter:
    """What intake needs from the queue repo: which tasks are already in it, and a way
    to add an item."""

    FOLDERS = ("inbox/*", "ready/*/*", "check/*", "review/*", "blocked/*", "archive/*/*")

    def __init__(self, cfg, repo):
        self.cfg = cfg
        self.repo = repo

    def known_task_ids(self):
        found = set()
        for pattern in self.FOLDERS:
            for meta_file in self.repo.path.glob(pattern + "/meta.yaml"):
                task = planner_meta(load_meta(meta_file.parent))
                if task:
                    found.add(str(task["task_id"]))
        return found

    def new_name(self, title):
        name = f"{now(self.cfg).date().isoformat()}-{slugify(title or 'planner task')}"
        out, n = name, 2
        while (self.repo.path / "inbox" / out).exists():
            out, n = f"{name}-{n}", n + 1
        return out

    def add_item(self, name, brief, planner_info, entries):
        folder = self.repo.path / "inbox" / name
        folder.mkdir(parents=True)
        write_file(folder / "brief.md", brief)
        meta = load_meta(folder)
        meta["planner"] = planner_info
        note_move(self.cfg, meta, None, "inbox", "taken from Planner")
        save_meta(folder, meta)
        write_refs_yaml(folder, entries)
        self.repo.commit(f"workq: take {name} from Planner")
        if not self.repo.push():
            raise WorkqError("push failed while taking a Planner task; it will be taken next pass")
        return f"inbox/{name}", self.repo.head()


def run_intake(cfg):
    """Take new Planner tasks in as inbox items. Returns done, idle, off, busy, paused or error."""
    if not cfg["planner"]["enabled"]:
        return "off"
    state = State(cfg)
    with Lock(state, "intake") as got:
        if not got:
            return "busy"
        repo = Repo(cfg, state.dir / "clones" / "intake")
        try:
            repo.sync()
            if is_paused(repo):
                return "paused"
            svc = services(cfg)
            if svc.keeper:
                for gone in svc.keeper.cleanup():
                    log(f"[intake] deleted the attachments of archived item {gone}")
            intake = Intake(svc.board, QueueAdapter(cfg, repo), svc.plans, cfg["planner"]["accept_from"],
                            svc.tokens.account().get("oid"), svc.outbox, svc.book, svc.refused,
                            problem=lambda text: state.problem("intake", None, text), log=log,
                            attachments=svc.keeper)
            taken = intake.run()
            if taken:
                state._append("planner.jsonl", {"at": now(cfg).isoformat(), "taken": taken})
            return "done" if taken else "idle"
        except ServiceError as exc:
            state.problem("intake", None, f"Planner intake stopped: {exc}")
            return "error"
        except WorkqError as exc:
            state.problem("intake", None, str(exc))
            return "error"
        except Exception as exc:
            traceback.print_exc()
            state.problem("intake", None, f"unexpected error in Planner intake: {type(exc).__name__}: {exc}")
            return "error"


def run_planner_sync(cfg, state=None):
    """Send waiting Planner updates. Returns the PlannerSync result, or None if skipped."""
    if not cfg["planner"]["enabled"]:
        return None
    state = state or State(cfg)
    with Lock(state, "planner-sync") as got:
        if not got:
            return None
        try:
            svc = services(cfg)
            result = PlannerSync(svc.board, svc.outbox, svc.plans,
                                 problem=lambda text: state.problem("planner", None, text)).run()
            if result["sent"] or result["dropped"]:
                log(f"[planner] sent {result['sent']}, dropped {result['dropped']}, waiting {result['kept']}")
            return result
        except ServiceError as exc:
            state.problem("planner", None, f"Planner sync stopped: {exc}")
        except Exception as exc:
            traceback.print_exc()
            state.problem("planner", None, f"unexpected error in Planner sync: {type(exc).__name__}: {exc}")
        return None


def planner_task_link(cfg, meta):
    task = planner_meta(meta)
    template = cfg["planner"].get("task_link")
    if not (task and template and cfg["planner"]["enabled"]):
        return None
    try:
        return str(template).format(tenant=cfg["graph"]["tenant"], task=task["task_id"],
                                    plan=task.get("plan_id", ""))
    except (KeyError, IndexError, ValueError):
        return None


# ---------------------------------------------------------------- night


def night(cfg, until=None, max_passes=100):
    state = State(cfg)
    with Lock(state, "night") as got:
        if not got:
            log("night shift already running")
            return 0
        end = shift_end(cfg, until)
        log(f"night shift until {end:%Y-%m-%d %H:%M}")
        broken = False
        for n in range(max_passes):
            progress = False
            stopped = False
            if cfg["planner"]["enabled"] and run_intake(cfg) == "done":
                progress = True
            for stage in STAGES:
                while True:
                    budget_s = float(cfg["stages"][stage]["timeout_min"]) * 60
                    if now(cfg) + dt.timedelta(seconds=budget_s) > end:
                        log(f"[{stage}] not enough time left before {end:%H:%M}")
                        break
                    status = run_stage(cfg, stage)
                    if status == "done":
                        progress = True
                        continue
                    if status == "skipped":
                        continue  # one item was set aside; carry on with the rest
                    if status == "limited":
                        reset = dt.datetime.fromtimestamp(state.limit_until()).astimezone()
                        if reset + dt.timedelta(seconds=budget_s) < end:
                            wait = max(1.0, state.limit_until() - time.time())
                            log(f"usage limit: sleeping until {reset:%H:%M}")
                            time.sleep(wait)
                            continue
                        log("usage limit runs past the end of the shift; stopping")
                        stopped = True
                    if status in ("paused", "broken"):
                        stopped = True
                        broken = broken or status == "broken"
                    break
                if stopped:
                    break
            run_planner_sync(cfg, state)
            if stopped or not progress:
                break
        log(f"night shift over after {n + 1} pass(es)"
            + (": stopped because Claude Code could not run" if broken else ""))
        return 1 if broken else 0


# ---------------------------------------------------------------- digest


def md_escape(text):
    """Make text from briefs and agents show as plain text in a Markdown file."""
    return re.sub(r"([\\`*_<>\[\]|#~])", r"\\\1", str(text))


def build_digest(cfg, repo, state, md=False):
    """The digest as plain text (for mail and the terminal) or as Markdown (for the repo file)."""
    web = cfg["web_url"].rstrip("/")

    def link(rel):
        url = f"{web}/{rel}" if web else rel
        return f"[{rel}]({url})" if md else url

    def t(text):
        return md_escape(text) if md else text

    def head(text):
        if md:
            lines.extend([f"## {text.capitalize()}", ""])
        else:
            lines.append(text)

    def nothing(word):
        lines.append(f"{word.capitalize()}." if md else f"  {word}")

    def sub(text):
        lines.append(f"  - {text}" if md else f"  {text}")

    since = state.last_digest() or "0000"
    sid = shift_id(cfg)
    lines = []
    review = list_items(repo, "review")
    blocked = list_items(repo, "blocked")
    counts = (f"{len(review)} for review, {len(blocked)} blocked, "
              f"{len(list_items(repo, 'inbox'))} waiting in inbox")
    if md:
        zone = f" ({cfg['timezone']})" if cfg.get("timezone") else ""
        lines.extend([f"# workq digest, {now(cfg):%A %d %B %Y}", "",
                      f"Written at {now(cfg):%H:%M}{zone}. {counts}."])
    else:
        lines.append(f"workq digest, {now(cfg):%A %d %B %Y}")
        lines.append(counts)
    if is_paused(repo):
        lines.extend(["", "**PAUSE file is set: nothing ran.**"] if md else ["PAUSE file is set: nothing ran."])
    lines.append("")

    head(f"FOR REVIEW ({len(review)})")
    if not review:
        nothing("nothing")
    for item in review:
        meta = load_meta(item)
        entered = next((h["at"] for h in reversed(meta["history"]) if h.get("to") == "review"), None)
        waited = ""
        if entered:
            days = (now(cfg) - dt.datetime.fromisoformat(entered)).days
            waited = f", waiting {days} day{'s' if days != 1 else ''}" if days else ", new"
        summary = first_line(read_text(item / "result.md", 20000))
        if md:
            lines.append(f"- **{t(meta['title'])}** ({meta.get('tier') or '-'}{waited})")
        else:
            lines.append(f"- {meta['title']} [{meta.get('tier') or '-'}{waited}]")
        if summary:
            sub(t(summary))
        sub(link('review/' + item.name))
        task_url = planner_task_link(cfg, meta)
        if task_url:
            sub(f"[Planner task]({task_url})" if md else f"Planner task: {task_url}")
    lines.append("")

    head(f"BLOCKED ({len(blocked)})")
    if not blocked:
        nothing("nothing")
    for item in blocked:
        meta = load_meta(item)
        reason = meta.get('blocked_reason', 'blocked')
        lines.append(f"- **{t(meta['title'])}**: {t(reason)}" if md else f"- {meta['title']}: {reason}")
        if (item / "questions.md").is_file():
            _, body = front_matter(read_text(item / "questions.md", 20000))
            for q in [l for l in body.splitlines() if l.strip()][:4]:
                q = q.strip()[:160]
                sub(t(re.sub(r"^(\d+[.)]|[-*+]|#+)\s+", "", q)) if md else q)
        sub(link('blocked/' + item.name))
        task_url = planner_task_link(cfg, meta)
        if task_url:
            sub(f"[Planner task]({task_url})" if md else f"Planner task: {task_url}")
    lines.append("")

    grouped = {}
    for p in state.problems_since(since):
        key = (p["stage"], p.get("item") or "", p["problem"])
        first, count = grouped.get(key, (p["at"], 0))
        grouped[key] = (first, count + 1)
    head(f"PROBLEMS ({len(grouped)})")
    if not grouped:
        nothing("none")
    for (stage, item_name, text), (first, count) in list(grouped.items())[-20:]:
        times = f" (x{count})" if count > 1 else ""
        lines.append("- " + t(f"{first[11:16]} [{stage}] {item_name} {text}{times}".replace("  ", " ")))
    lines.append("")

    if cfg["planner"]["enabled"]:
        taken = sum(int(r.get("taken") or 0) for r in state._read("planner.jsonl") if r.get("at", "") > since)
        waiting = services(cfg).outbox.read()
        head("PLANNER")
        oldest = f" (oldest {waiting[0].get('at', '')[:16].replace('T', ' ')} UTC)" if waiting else ""
        lines.append(f"- {taken} task{'s' if taken != 1 else ''} taken since the last digest; "
                     f"{len(waiting)} update{'s' if len(waiting) != 1 else ''} waiting to be sent{oldest}")
        lines.append("")

    rows = [r for r in state.ledger() if r.get("shift") == sid]
    head(f"LAST NIGHT (shift {sid})")
    if not rows:
        nothing("no runs")
    for tier in TIERS:
        tier_rows = [r for r in rows if r.get("tier") == tier]
        if not tier_rows:
            continue
        cost = sum(float(r.get("cost") or 0) for r in tier_rows)
        cap = cfg["caps"][tier]
        lines.append(f"- {tier}: {len(tier_rows)}/{cap['runs']} runs, est. ${cost:.2f}/${cap['usd']:.2f}")
    outcomes = {}
    for r in rows:
        outcomes[r.get("outcome")] = outcomes.get(r.get("outcome"), 0) + 1
    if outcomes:
        text = "outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items()))
        lines.append(f"- {text}" if md else f"  {text}")
    lines.append("")
    if md:
        lines.append("To answer, add `review.md` to an item folder: verdict approve, rework or reject "
                     "(retry or reject in `blocked/`).")
    else:
        lines.append("Reply by adding review.md to an item folder: verdict approve, rework or reject "
                     "(retry or reject in blocked/).")
    return "\n".join(lines) + "\n"


def send_mail(cfg, subject, body):
    """Send one plain-text email by the configured route. Returns None, or the error as text."""
    try:
        services(cfg).mailer().send(cfg["digest"].get("to") or "", subject, body)
    except ServiceError as exc:
        return str(exc)
    return None


def write_digest_file(cfg, repo, state, name, note=None):
    """Put the digest in the queue repo. Returns None, or the error as text."""
    try:
        if (repo.path / name).is_dir() and not (repo.path / name).is_symlink():
            return f"{name} is a folder in the queue repo"
        text = build_digest(cfg, repo, state, md=True)
        if note:
            first, _, rest = text.partition("\n")
            text = f"{first}\n\n**{md_escape(note)}**\n{rest}"
        write_file(repo.path / name, text)
        repo.commit(f"workq: digest {now(cfg):%Y-%m-%d %H:%M}")
        if not repo.push():
            return f"could not push {name} to the queue repo"
    except (OSError, subprocess.SubprocessError, WorkqError) as exc:
        return f"could not write {name}: {exc}"
    return None


def digest(cfg, to_stdout=False):
    state = State(cfg)
    with Lock(state, "digest") as got:
        if not got:
            return 0
        if cfg["planner"]["enabled"] and not to_stdout:
            run_planner_sync(cfg, state)
        repo = Repo(cfg, state.dir / "clones" / "digest")
        repo.sync()
        body = build_digest(cfg, repo, state)
        stamp = now(cfg).isoformat()
        dcfg = cfg["digest"]
        target = digest_file(cfg)
        if to_stdout or not (dcfg.get("to") or target):
            print(body)
            return 0
        errors = []
        if target:
            error = write_digest_file(cfg, repo, state, target)
            if error:
                errors.append(("digest not written: " + error, "digest not written: " + error))
            else:
                log(f"digest written to {target} in the queue repo")
        if dcfg.get("to"):
            error = send_mail(cfg, f"{dcfg['subject']} {now(cfg):%a %d %b}", body)
            if error:
                errors.append(("send failed: " + error, "digest not sent: " + error))
                if not target:
                    # A failed email must not be the only copy of the reason it failed.
                    ferr = write_digest_file(cfg, repo, state, DIGEST_FILE, note=f"Email failed: {error}")
                    if ferr:
                        errors.append(("digest not written: " + ferr, "digest not written: " + ferr))
                    else:
                        log(f"email failed; digest written to {DIGEST_FILE} in the queue repo instead")
            else:
                log(f"digest sent to {dcfg['to']}")
        if errors:
            for problem, _ in errors:
                state.problem("digest", None, problem[:300])
            print(body)
            for _, shown in errors:
                print(f"workq: {redact(shown)}", file=sys.stderr)
            return 1
        state.set_last_digest(stamp)
        return 0


# ---------------------------------------------------------------- small commands


def tools_repo(cfg):
    state = State(cfg)
    repo = Repo(cfg, state.dir / "clones" / "tools")
    return state, repo


class tools_lock:
    """These commands share one clone, so each waits its turn or gives up."""

    def __init__(self, cfg, state):
        self.lock = Lock(state, "tools", wait=float(cfg["lock_wait_s"]))

    def __enter__(self):
        if not self.lock.__enter__():
            raise WorkqError("another workq command is still running; try again in a moment")

    def __exit__(self, *exc):
        self.lock.__exit__(*exc)


def cmd_init(cfg):
    state, repo = tools_repo(cfg)
    template = PKG_DIR / "template"
    if not template.exists():
        raise WorkqError(f"template folder not found at {template}")
    with tools_lock(cfg, state):
        work = Path(tempfile.mkdtemp(prefix="workq-init-"))
        try:
            probe = repo.git("ls-remote", cfg["repo_url"], check=False, cwd=work)
            if probe.returncode != 0:
                raise WorkqError(f"cannot reach {cfg['repo_url']}: {probe.stderr.strip()}")
            if probe.stdout.strip():
                raise WorkqError("remote repo is not empty; init only fills an empty repo")
            repo.git("init", "--quiet", "-b", cfg["branch"], cwd=work)
            shutil.copytree(template, work, dirs_exist_ok=True)
            Repo(cfg, work).commit("workq: queue skeleton")
            repo.git("remote", "add", "origin", cfg["repo_url"], cwd=work)
            repo.git("push", "--quiet", "origin", f"HEAD:{cfg['branch']}", cwd=work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    log(f"queue skeleton pushed to {cfg['repo_url']} ({cfg['branch']})")


def cmd_add(cfg, title, file=None, text=None):
    state, repo = tools_repo(cfg)
    with tools_lock(cfg, state):
        repo.sync()
        name = f"{now(cfg).date().isoformat()}-{slugify(title)}"
        folder = repo.path / "inbox" / name
        n = 2
        while folder.exists():
            folder = repo.path / "inbox" / f"{name}-{n}"
            n += 1
        folder.mkdir(parents=True)
        body = Path(file).read_text() if file else (text or "")
        write_file(folder / "brief.md", f"# {title}\n\n{body.strip()}\n")
        repo.commit(f"workq: add {folder.name}")
        if not repo.push():
            raise WorkqError("push failed")
    log(f"added inbox/{folder.name}")


def cmd_pause(cfg, on):
    state, repo = tools_repo(cfg)
    with tools_lock(cfg, state):
        repo.sync()
        flag = repo.path / "PAUSE"
        if on:
            write_file(flag, "Delete this file to let workq run again.\n")
        elif flag.exists():
            flag.unlink()
        repo.commit("workq: pause" if on else "workq: resume")
        if not repo.push():
            raise WorkqError("push failed")
    log("paused" if on else "resumed")


def cmd_status(cfg):
    state, repo = tools_repo(cfg)
    with tools_lock(cfg, state):
        repo.sync()
        if is_paused(repo):
            print("PAUSED")
        for folder in ["inbox", "ready/haiku", "ready/sonnet", "ready/opus", "check", "review", "blocked"]:
            items = list_items(repo, folder)
            print(f"{folder:14} {len(items)}")
            for item in items:
                print(f"    {item.name}")
        archived = list((repo.path / "archive").glob("*/*/meta.yaml"))
        print(f"{'archive':14} {len(archived)}")
        sid = shift_id(cfg)
        for tier in TIERS:
            runs, usd = state.spend(tier, sid)
            cap = cfg["caps"][tier]
            print(f"tonight {tier:7} {runs}/{cap['runs']} runs, est. ${usd:.2f}/${cap['usd']:.2f}")
        if state.limit_until() > time.time():
            print("usage limit until", dt.datetime.fromtimestamp(state.limit_until()).strftime("%H:%M"))

# ---------------------------------------------------------------- Graph and Planner commands


def need_graph(cfg):
    if not cfg["graph"]["configured"]:
        raise WorkqError("the graph section of the config is not set up (tenant and client_id)")
    return services(cfg)


def cmd_graph(cfg, action, pasted=None):
    svc = need_graph(cfg)
    if action == "logout":
        svc.tokens.store.clear()
        print(f"signed out: deleted {cfg['graph']['token_file']}")
        return 0
    pending = svc.tokens.start_login()
    print("Sign in to Microsoft Graph as the service account (not as yourself):\n")
    print("1. Open this address in a browser on any machine:\n")
    print("   " + pending["url"] + "\n")
    print("2. Sign in. The browser ends on an http://localhost address that fails to load.")
    print("3. Copy that whole address from the browser's address bar and paste it here.\n", flush=True)
    if pasted is None:
        try:
            pasted = input("Address: ")
        except EOFError:
            raise WorkqError("no address pasted")
    try:
        account = svc.tokens.finish_login(pasted, pending)
    except ServiceError as exc:
        raise WorkqError(str(exc))
    who = account.get("name") or "?"
    print(f"\nsigned in as {who} <{account.get('upn') or '?'}> (object ID {account.get('oid') or '?'})")
    print(f"token saved in {cfg['graph']['token_file']}")
    status = 0
    if cfg["planner"]["enabled"]:
        for plan in svc.plans:
            try:
                names = {b.name for b in svc.board.buckets(plan["id"])}
                missing = [n for n in plan["buckets"].values() if n not in names]
                print(f"plan {plan['name']}: readable" + (f"; missing buckets: {', '.join(missing)} "
                                                          "(run `workq planner setup`)" if missing else ""))
            except ServiceError as exc:
                print(f"plan {plan['name']}: NOT readable: {exc}")
                status = 1
    return status


def cmd_planner(cfg, action, yes=False):
    if not cfg["planner"]["enabled"]:
        raise WorkqError("planner.enabled is false in the config")
    svc = need_graph(cfg)
    state = State(cfg)
    if action == "sync":
        result = run_planner_sync(cfg, state)
        if result is None:
            print("nothing sent (another sync is running, or it stopped: see the problems in the digest)")
            return 1
        print(f"sent {result['sent']}, dropped {result['dropped']}, still waiting {result['kept']}")
        return 1 if result["stopped"] else 0
    if action == "intake":
        status = run_intake(cfg)
        print(f"intake: {status}")
        return 1 if status == "error" else 0
    if action == "status":
        waiting = svc.outbox.read()
        print(f"outbox: {len(waiting)} update(s) waiting"
              + (f", oldest {waiting[0].get('at')}" if waiting else ""))
        status = 0
        for plan in svc.plans:
            try:
                names = {b.name for b in svc.board.buckets(plan["id"])}
                missing = [n for n in plan["buckets"].values() if n not in names]
                print(f"plan {plan['name']} ({plan['id']}): "
                      + ("all six buckets present" if not missing else "missing: " + ", ".join(missing)))
            except ServiceError as exc:
                print(f"plan {plan['name']} ({plan['id']}): {exc}")
                status = 1
        print("sign-in details: run `workq doctor`")
        return status
    # setup: create missing buckets after a yes
    plans_missing = []
    for plan in svc.plans:
        try:
            names = {b.name for b in svc.board.buckets(plan["id"])}
        except ServiceError as exc:
            raise WorkqError(f"plan {plan['name']}: {exc}")
        missing = [n for n in plan["buckets"].values() if n not in names]
        print(f"plan {plan['name']}: " + (f"missing {', '.join(missing)}" if missing else "all six buckets present"))
        if missing:
            plans_missing.append((plan, missing))
    if not plans_missing:
        return 0
    if not yes:
        try:
            answer = input("Create the missing buckets? Type yes: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("nothing created")
            return 1
    for plan, missing in plans_missing:
        for name in missing:
            try:
                svc.board.create_bucket(plan["id"], name)
                print(f"plan {plan['name']}: created {name}")
            except ServiceError as exc:
                raise WorkqError(f"could not create bucket {name} in {plan['name']}: {exc}")
    return 0


# ---------------------------------------------------------------- schedule (no root)

CRON_BEGIN = "# >>> workq schedule: written by `workq schedule on`; edits here are overwritten >>>"
CRON_END = "# <<< workq schedule <<<"
UNIT_MARK = "# Written by `workq schedule on`. Change config.yaml and run it again, don't edit."


def run_quiet(cmd, timeout=30, env=None, text_in=None):
    """Run a helper command. Returns (exit code, stdout, stderr); 127 if it can't start."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                              input=text_in)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def schedule_jobs(cfg):
    """What to run and when: a list of (name, "HH:MM", [command, ...])."""
    base = [str(PKG_DIR / "bin" / "workq"), "--config", cfg["_config_path"]]
    jobs = [
        ("night", cfg["window"]["start"], [base + ["night"]]),
        ("digest", cfg["digest"]["at"], [base + ["digest"]]),
    ]
    for shift in cfg["extra_shifts"]:
        commands = [base + ["night", "--until", shift["until"]]]
        if shift["digest"]:
            commands.append(base + ["digest"])
        jobs.append(("shift-" + shift["start"].replace(":", ""), shift["start"], commands))
    return jobs


def unit_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def sd_quote(arg):
    arg = arg.replace("%", "%%")
    return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"') if re.search(r"\s|\"|'", arg) else arg


def systemd_units(cfg):
    """File name -> contents for the user units."""
    tz = f" {cfg['timezone']}" if cfg.get("timezone") else ""
    units = {}
    for name, start, commands in schedule_jobs(cfg):
        lines = [UNIT_MARK, "[Unit]", f"Description=workq {name}", "", "[Service]", "Type=oneshot"]
        for i, command in enumerate(commands):
            # A leading "-" lets the next command run even if this one fails.
            dash = "-" if i < len(commands) - 1 else ""
            lines.append("ExecStart=" + dash + " ".join(sd_quote(a) for a in command))
        lines += ["TimeoutStartSec=12h", "Nice=10", ""]
        units[f"workq-{name}.service"] = "\n".join(lines)
        units[f"workq-{name}.timer"] = "\n".join([
            UNIT_MARK, "[Unit]", f"Description=Start workq {name}", "", "[Timer]",
            f"OnCalendar=*-*-* {start}:00{tz}",
            "# A machine that was off at the start time skips that run.",
            "Persistent=false", "", "[Install]", "WantedBy=timers.target", "",
        ])
    return units


def cron_block(cfg):
    log_file = cfg["state_dir"] / "logs" / "cron.log"
    lines = [CRON_BEGIN]
    tz = cfg.get("timezone")
    if tz and tz != system_timezone():
        lines.append(f"CRON_TZ={tz}")
    for _name, start, commands in schedule_jobs(cfg):
        hour, minute = hhmm(start)
        line = "; ".join(
            " ".join(shlex.quote(a) for a in command) + f" >> {shlex.quote(str(log_file))} 2>&1"
            for command in commands
        )
        lines.append(f"{minute} {hour} * * * {line}".replace("%", "\\%"))
    lines.append(CRON_END)
    return "\n".join(lines) + "\n"


def system_timezone():
    try:
        return os.path.realpath("/etc/localtime").split("/zoneinfo/", 1)[1]
    except IndexError:
        return None


def systemd_user_state():
    """(usable, lingering, reason). User timers only fire unattended when lingering is on."""
    if not shutil.which("systemctl"):
        return False, False, "systemctl not found"
    code, _out, err = run_quiet(["systemctl", "--user", "show-environment"])
    if code != 0:
        return False, False, "no systemd user session (" + (err.strip().splitlines() or ["no detail"])[0] + ")"
    code, out, _err = run_quiet(["loginctl", "show-user", getpass.getuser(), "--property=Linger"])
    return True, code == 0 and out.strip().endswith("=yes"), ""


def crontab_read():
    """(usable, current text, reason)."""
    if not shutil.which("crontab"):
        return False, "", "crontab not found"
    code, out, err = run_quiet(["crontab", "-l"])
    if code != 0 and "no crontab" not in err.lower():
        return False, "", (err.strip().splitlines() or ["crontab -l failed"])[0]
    return True, out if code == 0 else "", ""


def strip_cron_block(text):
    """Remove the block `workq schedule on` wrote, and nothing else. If its end marker
    has gone missing, only lines that are plainly workq's own are taken out."""
    ours = re.compile(r"^CRON_TZ=|/workq['\"]? --config .* (night|digest)\b")
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == CRON_BEGIN:
            skipping = True
        elif line.strip() == CRON_END:
            skipping = False
        elif skipping and ours.search(line.strip()):
            continue
        else:
            out.append(line)
    return "\n".join(out).rstrip("\n") + ("\n" if out else "")


def our_units():
    folder = unit_dir()
    if not folder.is_dir():
        return []
    return sorted(
        f for f in folder.glob("workq-*.*")
        if f.suffix in (".service", ".timer") and UNIT_MARK in f.read_text(errors="replace")
    )


def schedule_off():
    """Remove both kinds of schedule. Returns what was removed."""
    removed = []
    units = our_units()
    if units:
        timers = [u.name for u in units if u.suffix == ".timer"]
        if timers:
            run_quiet(["systemctl", "--user", "disable", "--now", *timers])
        for unit in units:
            unit.unlink()
        run_quiet(["systemctl", "--user", "daemon-reload"])
        removed.append(f"{len(timers)} systemd user timer(s)")
    usable, current, _why = crontab_read()
    if usable and CRON_BEGIN in current:
        run_quiet(["crontab", "-"], text_in=strip_cron_block(current))
        removed.append("the workq lines in your crontab")
    return removed


def cmd_schedule(cfg, action, scheduler="auto"):
    if not single_user(cfg):
        raise WorkqError(
            "`workq schedule` is for the no-root, single-user install. On a root install use: "
            "sudo systemctl enable --now workq-night.timer workq-digest.timer"
        )
    jobs = schedule_jobs(cfg)
    if action == "show":
        tz = cfg.get("timezone") or "system time"
        print(f"Planned ({tz}):")
        for name, start, commands in jobs:
            print(f"  {start}  {name}: " + "; then ".join(" ".join(c[3:]) for c in commands))
        units = our_units()
        usable, current, _why = crontab_read()
        if units:
            _ok, lingering, _why = systemd_user_state()
            print(f"Installed: systemd user timers ({len(units) // 2}); lingering is "
                  + ("on" if lingering else "OFF, so they only fire while you are logged in"))
            code, out, _err = run_quiet(["systemctl", "--user", "list-timers", "--all", "workq-*"])
            print(out.rstrip() if code == 0 else "  (systemctl --user list-timers failed)")
        elif usable and CRON_BEGIN in current:
            print("Installed: cron")
            block = current.split(CRON_BEGIN, 1)[1].split(CRON_END, 1)[0].strip()
            print("\n".join("  " + line for line in block.splitlines()))
        else:
            print("Installed: nothing. Run `workq schedule on`.")
        return 0
    if action == "off":
        removed = schedule_off()
        print("Removed " + " and ".join(removed) + "." if removed else "Nothing was scheduled.")
        return 0

    # on
    sd_ok, lingering, sd_why = systemd_user_state()
    if scheduler in ("auto", "systemd") and sd_ok and not lingering:
        run_quiet(["loginctl", "enable-linger"])  # allowed on some systems; harmless to try
        sd_ok, lingering, sd_why = systemd_user_state()
    cron_ok, current, cron_why = crontab_read()
    if scheduler == "auto":
        scheduler = "systemd" if (sd_ok and lingering) else "cron" if cron_ok else None
        if scheduler is None:
            raise WorkqError(
                "no way to schedule without root here: systemd user timers need lingering ("
                + (sd_why or "lingering is off") + f"), and cron is unavailable ({cron_why}). "
                f"Ask an admin for one of: `loginctl enable-linger {getpass.getuser()}`, or cron access."
            )
    # Check the chosen route works before removing whatever is scheduled now.
    if scheduler == "systemd" and not sd_ok:
        raise WorkqError(f"systemd user timers can't be used: {sd_why}")
    if scheduler == "cron" and not cron_ok:
        raise WorkqError(f"cron can't be used: {cron_why}")
    schedule_off()
    if scheduler == "systemd":
        folder = unit_dir()
        folder.mkdir(parents=True, exist_ok=True)
        units = systemd_units(cfg)
        for name, text in units.items():
            (folder / name).write_text(text)
        timers = [n for n in units if n.endswith(".timer")]
        for command in (["daemon-reload"], ["enable", "--now", *timers]):
            code, _out, err = run_quiet(["systemctl", "--user", *command])
            if code != 0:
                raise WorkqError(f"systemctl --user {' '.join(command)} failed: {err.strip()}")
        print(f"Scheduled with systemd user timers: {', '.join(timers)}")
        if not lingering:
            print(f"WARNING: lingering is off, so these only fire while you are logged in. Ask an "
                  f"admin to run `loginctl enable-linger {getpass.getuser()}`, or use "
                  "`workq schedule on --scheduler cron`.")
        print("See them: systemctl --user list-timers 'workq-*'   Logs: journalctl --user -u 'workq-*'")
    else:
        (cfg["state_dir"] / "logs").mkdir(parents=True, exist_ok=True)
        _ok, current, _why = crontab_read()
        base = strip_cron_block(current)
        code, _out, err = run_quiet(["crontab", "-"], text_in=base + cron_block(cfg))
        if code != 0:
            raise WorkqError(f"crontab refused the new schedule: {err.strip()}")
        print("Scheduled with cron. See it: crontab -l   Logs: " + str(cfg["state_dir"] / "logs" / "cron.log"))
        if all(run_quiet(["pgrep", "-x", name])[0] != 0 for name in ("crond", "cron")):
            print("WARNING: no cron service seems to be running on this machine, so nothing will fire. "
                  "An admin can start it: systemctl enable --now crond")
        if scheduler == "cron" and sd_ok and lingering:
            print("(systemd user timers would also work here.)")
        elif sd_ok and not lingering:
            print(f"(Used cron because lingering is off. For systemd timers instead, an admin runs: "
                  f"loginctl enable-linger {getpass.getuser()})")
    for name, start, _commands in jobs:
        print(f"  {start}  {name}")
    return 0


# ---------------------------------------------------------------- doctor


def private(path):
    """True if only the owner can read the file."""
    return stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0


def doctor_graph(cfg, ok, warn, bad):
    g = cfg["graph"]
    svc = services(cfg)
    ok(f"graph: tenant {g['tenant']}, app {g['client_id']}; asks for "
       + ", ".join(sc.rsplit("/", 1)[-1] for sc in svc.scopes()))
    if g.get("ca_file"):
        (ok if os.path.isfile(g["ca_file"]) else bad)(f"graph.ca_file {g['ca_file']} "
                                                       + ("found" if os.path.isfile(g["ca_file"]) else "missing"))
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    try:
        svc.client.probe()
        ok(f"Graph is reachable {'through ' + redact(proxy) if proxy else 'directly'}, and its certificate is trusted")
    except ServiceError as exc:
        bad(f"Graph: {exc}")
        return
    token = Path(g["token_file"])
    if not token.is_file():
        bad("not signed in to Graph: run `workq graph login`")
        return
    if not private(token):
        bad(f"{token} can be read by other users; run: chmod 600 {token}")
    try:
        svc.tokens.access_token()
    except ServiceError as exc:
        bad(f"Graph sign-in: {exc}")
        return
    acct = svc.tokens.account()
    ok(f"signed in to Graph as {acct.get('name') or '?'} <{acct.get('upn') or '?'}>: check this is "
       "the service account, not you")
    if not cfg["planner"]["enabled"]:
        return
    if not acct.get("oid"):
        bad("the sign-in has no account ID, so no task can be matched to it: run `workq graph login` again")
    for plan in svc.plans:
        try:
            names = {b.name for b in svc.board.buckets(plan["id"])}
        except ServiceError as exc:
            bad(f"plan {plan['name']}: {exc}")
            continue
        missing = [n for n in plan["buckets"].values() if n not in names]
        if missing:
            warn(f"plan {plan['name']} is readable but lacks buckets {', '.join(missing)}: run `workq planner setup`")
        else:
            ok(f"plan {plan['name']} is readable and has all six buckets")
    if cfg["planner"]["attachments"]["enabled"]:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        (ok if soffice else warn)(f"LibreOffice found at {soffice}: Word and PowerPoint attachments get PDF copies"
                                  if soffice else "LibreOffice (soffice) not found: Word and PowerPoint "
                                  "attachments reach Claude as Markdown only")


def cmd_doctor(cfg, live=False):
    """Check the setup and print one line per check. Exit code 1 if anything is broken."""
    rows = []

    def ok(text):
        rows.append(("ok  ", text))

    def warn(text):
        rows.append(("WARN", text))

    def bad(text):
        rows.append(("FAIL", text))

    solo = single_user(cfg)
    v = sys.version_info
    (ok if v >= (3, 9) else bad)(f"Python {v.major}.{v.minor}.{v.micro} (3.9 or later needed), {YAML_SOURCE}")

    code, out, _err = run_quiet(["git", "--version"])
    found = re.search(r"(\d+)\.(\d+)", out)
    if code != 0 or not found:
        bad("git not found. It needs an admin to install it: dnf install git")
    elif (int(found.group(1)), int(found.group(2))) < (2, 28):
        bad(f"{out.strip()} is too old; 2.28 or later needed")
    else:
        ok(out.strip())

    ok(f"config {cfg['_config_path']}; "
       + ("single-user mode" if solo else f"two-user mode, Claude runs as {cfg['claude']['agent_user']}"))
    try:
        cfg["state_dir"].mkdir(parents=True, exist_ok=True)
        probe = cfg["state_dir"] / ".doctor"
        probe.write_text("x")
        probe.unlink()
        ok(f"state folder {cfg['state_dir']} is writable")
    except OSError as exc:
        bad(f"state folder {cfg['state_dir']}: {exc}")

    secrets = [("claude.env_file", cfg["claude"].get("env_file")),
               ("git.ssh_key", cfg["git"].get("ssh_key")),
               ("git.credential_file", cfg["git"].get("credential_file")),
               ("digest.smtp.password_file", cfg["digest"]["smtp"].get("password_file"))]
    for label, path in secrets:
        if not path:
            continue
        if not os.path.isfile(path):
            bad(f"{label}: {path} does not exist")
        elif not private(path):
            bad(f"{label}: {path} can be read by other users; run: chmod 600 {path}")
        else:
            ok(f"{label}: {path} is private")

    # Claude Code
    env = claude_env(cfg)
    exe = claude_exe(cfg) if solo else None
    mode = "off"
    if not solo:
        (ok if os.path.exists(cfg["claude"]["wrapper"]) else bad)(
            f"wrapper {cfg['claude']['wrapper']} " + ("found" if os.path.exists(cfg["claude"]["wrapper"]) else "missing"))
    elif not exe:
        bad(f"Claude Code ({cfg['claude']['bin']}) not found. Install it without root: "
            "curl -fsSL https://claude.ai/install.sh | bash")
    else:
        exe = str(Path(exe).resolve())
        code, out, err = run_quiet([exe, "--version"], env=env)
        (ok if code == 0 else bad)(f"Claude Code {out.strip() or err.strip()[:200]} at {exe}")
        code, helptext, _err = run_quiet([exe, "--help"], env=env)
        needed = ["--tools", "--allowedTools", "--permission-mode", "--max-budget-usd",
                  "--no-session-persistence", "--append-system-prompt", "--settings"]
        needed += ["--restricted"] if cfg["claude"].get("restricted") else []
        needed += ["--strict-mcp-config"] if cfg["claude"].get("strict_mcp") else []
        missing = [flag for flag in needed if flag not in helptext]
        if missing:
            bad("this Claude Code lacks " + ", ".join(missing) + "; update it (run: claude update)")
        else:
            ok("Claude Code has every option workq uses")
        if any(env.get(name) for name in CREDENTIAL_VARS):
            ok("a Claude credential is set")
        else:
            warn("no Claude credential found: put CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) "
                 f"or ANTHROPIC_API_KEY in {cfg['claude'].get('env_file') or 'claude.env_file'}")
        try:
            settings_args(cfg)
            if cfg["claude"].get("settings"):
                ok(f"deny rules file {cfg['claude']['settings']} is valid")
        except WorkqError as exc:
            bad(str(exc))
        # Sandbox
        try:
            mode = sandbox_mode(cfg)
        except WorkqError as exc:
            bad(str(exc))
        if mode == "bwrap":
            probe_dir = Path(tempfile.mkdtemp(prefix="doctor-", dir=str(cfg["state_dir"])))
            (probe_dir / ".git").mkdir()
            code, out, err = run_quiet(sandbox_wrap(cfg, probe_dir, [exe, "--version"]), env=env)
            shutil.rmtree(probe_dir, ignore_errors=True)
            if code == 0:
                ok("sandbox: bubblewrap works, and Claude Code starts inside it")
            else:
                bad("sandbox: Claude Code does not start inside bubblewrap: "
                    + (err.strip() or out.strip())[:300]
                    + ". Add the folders it needs to claude.sandbox_binds, or set claude.sandbox: off")
        elif cfg["bash_allow"]:
            warn("no sandbox, and bash_allow lets agents run commands as you: they could read "
                 "your keys and tokens. Empty bash_allow, or get bubblewrap installed")
        else:
            warn("no sandbox (bubblewrap missing or not permitted). Agents are held to their clone "
                 "by Claude Code's own restricted mode only. An admin can add it: dnf install bubblewrap")

    # Queue repo
    if redact(cfg["repo_url"]) != str(cfg["repo_url"]):
        bad("repo_url has a password or token in it, which ends up in logs and git's own "
            "config. Take it out and put it in the file named by git.credential_file")
    repo = Repo(cfg, cfg["state_dir"])
    code, out, err = run_quiet([*repo.base, "ls-remote", "--heads", cfg["repo_url"]], timeout=45, env=repo.env)
    if code != 0:
        bad(f"cannot reach the queue repo {cfg['repo_url']}: " + (" ".join(err.split()) or "no detail")[:300])
    elif f"refs/heads/{cfg['branch']}" in out:
        ok(f"queue repo reachable, branch {cfg['branch']} exists")
    elif not out.strip():
        warn("queue repo reachable but empty: run `workq init`")
    else:
        bad(f"queue repo reachable but has no branch {cfg['branch']}")

    # Time zone, mail, schedule
    if cfg.get("timezone"):
        try:
            now(cfg)
            ok(f"time zone {cfg['timezone']}; the shift runs {cfg['window']['start']} to {cfg['window']['end']}")
        except Exception as exc:  # zoneinfo raises several kinds
            bad(f"time zone {cfg['timezone']}: {exc}")
    else:
        warn(f"no timezone in the config; using this machine's ({system_timezone() or 'unknown'})")
    smtp = cfg["digest"]["smtp"]
    target = digest_file(cfg)
    if target:
        ok(f"digest is written to {target} in the queue repo"
           + ("" if cfg["digest"].get("to") else " (no email: digest.to is empty)"))
    if not cfg["digest"].get("to"):
        if not target:
            warn("digest.to is empty and digest.repo_file is off: the digest goes nowhere. "
                 "Read it with `workq digest --stdout`")
    elif check_recipients(cfg)[1]:
        bad(check_recipients(cfg)[1])
    elif cfg["digest"]["route"] == "graph":
        ok(f"digest goes to {cfg['digest']['to']} through Graph, sent as the signed-in account")
    elif smtp.get("host"):
        ok(f"digest goes to {cfg['digest']['to']} through {smtp['host']}:{smtp.get('port')}")
    else:
        program = shlex.split(cfg["digest"]["send_command"])[0]
        (ok if shutil.which(program) else bad)(
            f"digest goes to {cfg['digest']['to']} with `{cfg['digest']['send_command']}`"
            + ("" if shutil.which(program) else f", but {program} is not installed. Set digest.smtp.host instead"))
    if cfg["graph"]["configured"]:
        doctor_graph(cfg, ok, warn, bad)
    if solo:
        usable, current, _why = crontab_read()
        if our_units():
            _sd, lingering, _why = systemd_user_state()
            (ok if lingering else warn)("scheduled with systemd user timers"
                                         + ("" if lingering else "; lingering is OFF, so they only fire while you are logged in"))
        elif usable and CRON_BEGIN in current:
            ok("scheduled with cron")
        else:
            warn("nothing is scheduled yet: run `workq schedule on`")

    if live and solo and exe:
        cmd = [exe, "-p", "--model", cfg["models"]["haiku"], "--output-format", "json",
               "--permission-mode", "dontAsk", "--no-session-persistence", "--max-turns", "1"]
        cmd += ["--restricted"] if cfg["claude"].get("restricted") else []
        cmd += ["--strict-mcp-config"] if cfg["claude"].get("strict_mcp") else []
        cmd += ["--tools", ""]
        if mode == "bwrap":
            probe_dir = Path(tempfile.mkdtemp(prefix="doctor-", dir=str(cfg["state_dir"])))
            (probe_dir / ".git").mkdir()
            cmd = sandbox_wrap(cfg, probe_dir, cmd)
        code, out, err = run_quiet(cmd, timeout=180, env=env, text_in="Reply with the single word OK.")
        if mode == "bwrap":
            shutil.rmtree(probe_dir, ignore_errors=True)
        try:
            reply = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            reply = {}
        if code == 0 and not reply.get("is_error") and reply.get("result"):
            ok(f"live test: Claude answered ({str(reply['result']).strip()[:40]!r})")
        else:
            bad("live test failed: " + (str(reply.get("result") or "") or err.strip() or out.strip())[:300])

    for level, text in rows:
        print(f"[{level}] {redact(text)}")
    fails = sum(1 for level, _ in rows if level == "FAIL")
    warns = sum(1 for level, _ in rows if level == "WARN")
    print(f"\n{fails} problem(s), {warns} warning(s).")
    return 1 if fails else 0



def main(argv=None):
    parser = argparse.ArgumentParser(prog="workq", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=default_config_path(),
                        help="default: $WORKQ_CONFIG, then ~/.config/workq/config.yaml, "
                             "then /etc/workq/config.yaml")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("add")
    p.add_argument("title")
    p.add_argument("-f", "--file")
    p.add_argument("-m", "--message")
    p = sub.add_parser("run")
    p.add_argument("stage", choices=STAGES)
    p = sub.add_parser("night")
    p.add_argument("--until", help="end time HH:MM (default: window end)")
    p = sub.add_parser("digest")
    p.add_argument("--stdout", action="store_true", help="print instead of sending")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    p = sub.add_parser("doctor")
    p.add_argument("--live", action="store_true",
                   help="also make one tiny real call to Claude, to prove sign-in works")
    p = sub.add_parser("schedule")
    p.add_argument("action", choices=["show", "on", "off"])
    p.add_argument("--scheduler", choices=["auto", "systemd", "cron"], default="auto")
    p = sub.add_parser("graph", help="sign in to Microsoft Graph, or out")
    p.add_argument("action", choices=["login", "logout"])
    p = sub.add_parser("planner", help="the Planner link")
    p.add_argument("action", choices=["setup", "status", "sync", "intake"])
    p.add_argument("--yes", action="store_true", help="setup: create missing buckets without asking")
    args = parser.parse_args(argv)

    def stop(signum, _frame):
        # Unwind normally, so a running agent is stopped and its clone is unlocked.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        cfg = load_config(args.config)
        # Everything the runner writes is private. In two-user mode the one clone an
        # agent is working in is opened to it for the length of that run.
        os.umask(0o077)
        apply_env(cfg)
        if args.cmd == "doctor":
            return cmd_doctor(cfg, args.live)
        if args.cmd == "schedule":
            return cmd_schedule(cfg, args.action, args.scheduler)
        if args.cmd == "graph":
            return cmd_graph(cfg, args.action)
        if args.cmd == "planner":
            return cmd_planner(cfg, args.action, args.yes)
        if args.cmd == "init":
            cmd_init(cfg)
        elif args.cmd == "add":
            cmd_add(cfg, args.title, args.file, args.message)
        elif args.cmd == "run":
            status = run_stage(cfg, args.stage)
            log(f"[{args.stage}] {status}")
            return 1 if status in ("error", "broken", "skipped") else 0
        elif args.cmd == "night":
            return night(cfg, args.until)
        elif args.cmd == "digest":
            return digest(cfg, args.stdout)
        elif args.cmd == "status":
            cmd_status(cfg)
        elif args.cmd in ("pause", "resume"):
            cmd_pause(cfg, args.cmd == "pause")
    except WorkqError as exc:
        print(f"workq: {redact(exc)}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        quiet_stdout()   # `workq status | head`: leave quietly
        return 0
    except Exception as exc:
        traceback.print_exc()
        print(f"workq: unexpected error: {type(exc).__name__}: {redact(exc)}", file=sys.stderr)
        return 2
    return 0


def cli():
    code = main()
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        quiet_stdout()
    return code
