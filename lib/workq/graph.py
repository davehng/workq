"""Microsoft Graph: sign-in, tokens and HTTP. Nothing here knows about the queue.

Sign-in is the browser kind (authorization code with PKCE), for a public client with
no secret: `workq graph login` prints an address, you sign in as the service account,
and paste back the http://localhost address the browser ends on. After that the
refresh token in the token file keeps the runner signed in.
"""

import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .interfaces import ServiceError

DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
DEFAULT_ENDPOINT = "https://graph.microsoft.com"
REDIRECT_URI = "http://localhost"
GRAPH_SCOPE = "https://graph.microsoft.com/"
SIGNIN_SCOPES = ["offline_access", "openid", "profile"]
MAX_WAIT_S = 120


class GraphError(ServiceError):
    pass


def kind_for_status(status):
    if status == 401:
        return "signin"
    if status == 403:
        return "denied"
    if status == 404:
        return "gone"
    if status in (409, 412):
        return "conflict"
    if status in (429, 503, 504):
        return "throttled"
    if status == 400:
        return "invalid"
    return "other"


def is_local(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def check_base_url(url, label):
    """An endpoint must be https, except a local stand-in used by the tests."""
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme == "https" and parts.hostname:
        return str(url).rstrip("/")
    if parts.scheme == "http" and is_local(url):
        return str(url).rstrip("/")
    raise ValueError(f"{label} must be an https:// address: {url!r}")


def scopes_for(planner=False, attachments=False, mail=False):
    """The delegated permissions to ask for, from what is switched on."""
    wanted = []
    if planner:
        wanted.append("Tasks.ReadWrite")
    if planner and attachments:
        wanted.append("Files.SelectedOperations.Selected")
    if mail:
        wanted.append("Mail.Send")
    return [GRAPH_SCOPE + name for name in wanted] + SIGNIN_SCOPES


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the Authorization header to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ssl_context(ca_file=None):
    ctx = ssl.create_default_context()
    if ca_file:
        ctx.load_verify_locations(cafile=str(ca_file))
    return ctx


class Http:
    """A small HTTPS client: proxy from the environment, an optional extra CA file, no
    redirects, and a cap on how much of a response is read."""

    def __init__(self, ca_file=None, timeout=60):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=ssl_context(ca_file)),
            _NoRedirect(),
        )

    def send(self, method, url, data=None, headers=None, max_bytes=50 * 1024 * 1024):
        """Returns (status, headers, body bytes). HTTP error statuses are returned, not
        raised; a failure to connect raises GraphError(kind="network")."""
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            resp = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read(min(max_bytes, 1024 * 1024)) if exc.fp else b""
            return exc.code, dict(exc.headers or {}), body
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            text = str(reason)
            if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in text.upper():
                text = (f"TLS certificate not trusted ({text}). If a company proxy inspects TLS, "
                        "set graph.ca_file to its certificate")
            raise GraphError("network", f"cannot reach {urllib.parse.urlsplit(url).hostname}: {text}")
        with resp:
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise GraphError("other", f"response from {urllib.parse.urlsplit(url).hostname} "
                                          f"is over {max_bytes // (1024 * 1024)} MB")
            return resp.status, dict(resp.headers or {}), body


def parse_json(body):
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def error_message(status, data):
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return f"{status} {err.get('code') or ''}: {err.get('message') or ''}".strip()
    if isinstance(err, str):  # the token endpoint
        return f"{status} {err}: {str(data.get('error_description') or '').splitlines()[0][:200]}"
    return f"HTTP {status}"


# ---------------------------------------------------------------- tokens

class TokenStore:
    """The refresh token and the account it belongs to, in one private JSON file."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(self.path.name + ".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.path)

    def clear(self):
        for path in (self.path, self.path.with_name(self.path.name + ".lock")):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    @contextlib.contextmanager
    def lock(self):
        """One refresh at a time: the night shift and the digest can overlap."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def decode_id_token(token):
    """The claims in an ID token. It came straight from the token endpoint over TLS,
    so it is read, not verified."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, AttributeError):
        return {}


class RefreshTokenSource:
    """Access tokens for Graph, from the refresh token saved at sign-in."""

    def __init__(self, store, tenant, client_id, scopes, http, authority=DEFAULT_AUTHORITY,
                 clock=time.time):
        self.store = store
        self.tenant = tenant
        self.client_id = client_id
        self.scopes = list(scopes)
        self.http = http
        self.authority = authority.rstrip("/")
        self.clock = clock

    @property
    def token_url(self):
        return f"{self.authority}/{urllib.parse.quote(self.tenant)}/oauth2/v2.0/token"

    @property
    def authorize_url(self):
        return f"{self.authority}/{urllib.parse.quote(self.tenant)}/oauth2/v2.0/authorize"

    def account(self):
        return self.store.load().get("account") or {}

    def signed_in(self):
        return bool(self.store.load().get("refresh_token"))

    def access_token(self):
        with self.store.lock():
            data = self.store.load()
            if not data.get("refresh_token"):
                raise GraphError("signin", "not signed in to Microsoft Graph: run `workq graph login`")
            if data.get("access_token") and float(data.get("expires_at") or 0) - 120 > self.clock():
                return data["access_token"]
            fresh = self._redeem({"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
            self._keep(data, fresh)
            self.store.save(data)
            return data["access_token"]

    def invalidate(self):
        with self.store.lock():
            data = self.store.load()
            if data.pop("access_token", None):
                data.pop("expires_at", None)
                self.store.save(data)

    def _keep(self, data, fresh):
        data["access_token"] = fresh["access_token"]
        data["expires_at"] = self.clock() + float(fresh.get("expires_in") or 3600)
        if fresh.get("refresh_token"):        # usually a new one each time
            data["refresh_token"] = fresh["refresh_token"]
        data["refreshed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def _redeem(self, fields):
        form = dict(fields, client_id=self.client_id, scope=" ".join(self.scopes))
        status, _headers, body = self.http.send(
            "POST", self.token_url, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        data = parse_json(body)
        if status == 200 and data.get("access_token"):
            return data
        error = str(data.get("error") or "")
        if error in ("invalid_grant", "interaction_required", "consent_required", "login_required"):
            raise GraphError("signin", "Graph sign-in has expired or was revoked "
                                       f"({error_message(status, data)}): run `workq graph login`", status)
        if status in (429, 503, 504):
            raise GraphError("throttled", f"sign-in service busy: {error_message(status, data)}", status)
        if status >= 500:
            raise GraphError("other", f"sign-in service error: {error_message(status, data)}", status)
        raise GraphError("signin", f"sign-in refused: {error_message(status, data)}", status)

    # -- the browser sign-in

    def start_login(self):
        """The address to open, and what finish_login needs to check the answer."""
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "response_mode": "query",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        })
        return {"url": f"{self.authorize_url}?{query}", "state": state, "verifier": verifier}

    def finish_login(self, pasted, pending):
        """Swap the code in the pasted http://localhost address for tokens, and save them."""
        pasted = (pasted or "").strip()
        parts = urllib.parse.urlsplit(pasted)
        if parts.scheme not in ("http", "https") or not parts.query:
            raise GraphError("invalid", "that is not the address the browser ended on "
                                        "(it starts with http://localhost/?code=)")
        query = urllib.parse.parse_qs(parts.query)
        if query.get("error"):
            detail = (query.get("error_description") or [""])[0].splitlines()[0][:300]
            raise GraphError("signin", f"sign-in failed: {query['error'][0]}: {detail}")
        if (query.get("state") or [""])[0] != pending["state"]:
            raise GraphError("invalid", "the pasted address belongs to a different sign-in; run "
                                        "`workq graph login` again and use the new address")
        code = (query.get("code") or [""])[0]
        if not code:
            raise GraphError("invalid", "the pasted address has no code in it")
        fresh = self._redeem({"grant_type": "authorization_code", "code": code,
                              "redirect_uri": REDIRECT_URI, "code_verifier": pending["verifier"]})
        claims = decode_id_token(fresh.get("id_token", ""))
        account = {
            "oid": claims.get("oid", ""),
            "name": claims.get("name", ""),
            "upn": claims.get("preferred_username", ""),
            "tid": claims.get("tid", ""),
        }
        if not fresh.get("refresh_token"):
            raise GraphError("signin", "no refresh token came back: offline_access was not granted")
        data = {"account": account, "scopes": self.scopes,
                "signed_in_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        self._keep(data, fresh)
        with self.store.lock():
            self.store.save(data)
        return account


# ---------------------------------------------------------------- Graph calls

class CallLog:
    """Every Graph call the runner makes (method, path, result), kept 90 days."""

    def __init__(self, path, keep_days=90):
        self.path = Path(path)
        self.keep_days = keep_days
        self._trimmed = False

    def write(self, method, path, status):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._trimmed:
            self._trim()
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        path = path.split("?")[0]
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(f"{stamp} {method} {path} {status}\n")

    def _trim(self):
        self._trimmed = True
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return
        cutoff = (dt.datetime.now() - dt.timedelta(days=self.keep_days)).isoformat()
        kept = [line for line in lines if line[:19] >= cutoff[:19]]
        if len(kept) != len(lines):
            self.path.write_text("".join(line + "\n" for line in kept))


class GraphClient:
    """HTTP for Graph and nothing else: the token header, retries when Graph asks us to
    slow down, one retry with a fresh token after a 401, and errors turned into
    GraphError kinds."""

    def __init__(self, tokens, http, endpoint=DEFAULT_ENDPOINT, sleep=time.sleep, log=None,
                 tries=3, max_wait=MAX_WAIT_S):
        self.tokens = tokens
        self.http = http
        self.endpoint = endpoint.rstrip("/")
        self.sleep = sleep
        self.log = log
        self.tries = tries
        self.max_wait = max_wait

    def url(self, path, version="v1.0"):
        if path.startswith(("http://", "https://")):
            # Only ever send the token to Graph itself (an @odata.nextLink stays on Graph).
            if not path.startswith(self.endpoint + "/"):
                raise GraphError("invalid", f"refusing to send the Graph token to {path[:80]}")
            return path
        return f"{self.endpoint}/{version}{path}"

    def call(self, method, path, body=None, headers=None, version="v1.0"):
        """Returns (status, headers, parsed JSON)."""
        url = self.url(path, version)
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            hdrs = {"Authorization": "Bearer " + self.tokens.access_token(), "Accept": "application/json"}
            if data is not None:
                hdrs["Content-Type"] = "application/json"
            hdrs.update(headers or {})
            status, rhdrs, raw = self.http.send(method, url, data=data, headers=hdrs)
            if self.log:
                self.log.write(method, urllib.parse.urlsplit(url).path, status)
            parsed = parse_json(raw)
            if status == 401 and not refreshed:
                refreshed = True
                self.tokens.invalidate()
                continue
            if status in (429, 503, 504):
                if attempt >= self.tries:
                    raise GraphError("throttled", f"Graph kept asking to slow down ({status})", status)
                wait = retry_after(rhdrs, default=5 * attempt)
                self.sleep(min(wait, self.max_wait))
                continue
            if status >= 400:
                raise GraphError(kind_for_status(status), error_message(status, parsed), status)
            return status, rhdrs, parsed

    def get_all(self, path, version="v1.0", limit=50):
        """Every item of a list, following @odata.nextLink pages."""
        items = []
        next_path = path
        for _ in range(limit):
            _status, _h, data = self.call("GET", next_path, version=version)
            items += data.get("value") or []
            next_path = data.get("@odata.nextLink")
            if not next_path:
                break
        return items

    def download(self, url, max_bytes):
        """Fetch a pre-signed download address. The token is never sent with it."""
        if not (url.startswith("https://") or (url.startswith("http://") and is_local(self.endpoint))):
            raise GraphError("invalid", "download address is not https")
        status, _h, body = self.http.send("GET", url, max_bytes=max_bytes)
        if self.log:
            self.log.write("GET", "(download)", status)
        if status >= 400:
            raise GraphError(kind_for_status(status), f"download failed: HTTP {status}", status)
        return body

    def probe(self):
        """Can this machine reach Graph and trust its certificate? Any HTTP answer will do."""
        status, _h, _b = self.http.send("GET", f"{self.endpoint}/v1.0/", headers={"Accept": "application/json"})
        return status


def retry_after(headers, default=5):
    for key, value in (headers or {}).items():
        if key.lower() == "retry-after":
            try:
                return max(1, int(float(value)))
            except ValueError:
                return default
    return default
