"""A stand-in for Entra sign-in and the parts of Microsoft Graph that workq uses.

It runs in a thread inside the test process and keeps everything in memory, so a test
can set up plans, buckets, tasks and files, run workq against it (in-process or as a
subprocess), then look at what was moved, commented, mailed and downloaded.

Written from Microsoft's documentation, not recorded from Graph, so it shows that workq
matches its own reading of the API; the real thing is checked in the trial.
"""

import base64
import hashlib
import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def fake_jwt(claims):
    return f"{b64url(b'{}')}.{b64url(json.dumps(claims).encode())}.sig"


class FakeGraph:
    def __init__(self, account=None):
        self.account = account or {"oid": "11111111-2222-3333-4444-555555555555",
                                   "name": "svc-workq", "preferred_username": "svc-workq@corp.test",
                                   "tid": "tenant-guid"}
        self.lock = threading.Lock()
        self.plans = {}             # plan id -> {"buckets": {id: name}}
        self.tasks = {}             # task id -> task dict (Graph shape)
        self.details = {}           # task id -> details dict (Graph shape)
        self.messages = []          # (task id, body)
        self.mail = []              # sendMail bodies
        self.files = {}             # url -> {"name", "content", "etag"}
        self.downloads = []         # (key, had Authorization header)
        self.calls = []             # (method, path)
        self.faults = []            # [(method, path prefix, status, headers, body)] used once each
        self.codes = {}             # auth code -> {"challenge", "scope"}
        self.refresh_tokens = set()
        self.access_tokens = set()
        self.revoked = False
        self.message_status = None  # force every comment POST to fail with this status
        self.mention_status = None  # force comments WITH a mention to fail with this status
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- setting up

    def add_plan(self, plan_id="plan-1", buckets=()):
        self.plans[plan_id] = {"buckets": {}}
        for name in buckets:
            self.add_bucket(plan_id, name)
        return plan_id

    def add_bucket(self, plan_id, name):
        bid = "b-" + uuid.uuid4().hex[:8]
        self.plans[plan_id]["buckets"][bid] = name
        return bid

    def bucket_id(self, plan_id, name):
        return next(b for b, n in self.plans[plan_id]["buckets"].items() if n == name)

    def bucket_name(self, task_id):
        t = self.tasks[task_id]
        return self.plans[t["planId"]]["buckets"].get(t["bucketId"])

    def add_task(self, plan_id, bucket, title, created_by, assigned=(), description="", checklist=(),
                 references=(), percent=0, due=None):
        tid = "t-" + uuid.uuid4().hex[:8]
        self.tasks[tid] = {
            "id": tid, "planId": plan_id, "bucketId": self.bucket_id(plan_id, bucket), "title": title,
            "percentComplete": percent, "dueDateTime": due,
            "createdBy": {"user": {"id": created_by}},
            "assignments": {a: {"@odata.type": "#microsoft.graph.plannerAssignment"} for a in assigned},
            "@odata.etag": 'W/"1"',
        }
        enc = lambda u: urllib.parse.quote(u, safe="").replace(".", "%2E")  # noqa: E731
        self.details[tid] = {
            "description": description,
            "checklist": {uuid.uuid4().hex[:6]: {"title": c, "orderHint": f"{i:04d}"} for i, c in enumerate(checklist)},
            "references": {enc(url): {"alias": alias, "type": "Other"} for url, alias in references},
            "@odata.etag": 'W/"1"',
        }
        return tid

    def add_file(self, url, name, content):
        self.files[url] = {"name": name, "content": content, "etag": '"{v1,1}"'}

    def fail_next(self, method, path_prefix, status, headers=None, body=None):
        self.faults.append((method, path_prefix, status, headers or {}, body))

    def comments(self, task_id):
        return [b for t, b in self.messages if t == task_id]

    # -- the server

    def _handler(self):
        graph = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body=None, headers=None):
                data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if data:
                    self.send_header("Content-Type", "application/json" if isinstance(body, (dict, list))
                                     else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PATCH(self):
                self._route("PATCH")

            def _route(self, method):
                parts = urllib.parse.urlsplit(self.path)
                path = parts.path
                query = urllib.parse.parse_qs(parts.query)
                raw = self._body()
                with graph.lock:
                    graph.calls.append((method, path))
                    for i, (m, prefix, status, hdrs, body) in enumerate(graph.faults):
                        if m == method and path.startswith(prefix):
                            graph.faults.pop(i)
                            return self._send(status, body if body is not None else
                                              {"error": {"code": "Fault", "message": "injected"}}, hdrs)
                    try:
                        return self._dispatch(method, path, query, raw)
                    except KeyError:
                        return self._send(404, {"error": {"code": "NotFound", "message": "not found"}})

            # ---- sign-in

            def _dispatch(self, method, path, query, raw):
                if path.endswith("/oauth2/v2.0/authorize"):
                    code = uuid.uuid4().hex
                    graph.codes[code] = {"challenge": query["code_challenge"][0], "scope": query["scope"][0]}
                    loc = "http://localhost/?" + urllib.parse.urlencode({"code": code, "state": query["state"][0]})
                    return self._send(302, b"", {"Location": loc})
                if path.endswith("/oauth2/v2.0/token"):
                    return self._token(urllib.parse.parse_qs(raw.decode()))
                if path.startswith("/download/"):
                    key = urllib.parse.unquote(path[len("/download/"):])
                    graph.downloads.append((key, bool(self.headers.get("Authorization"))))
                    return self._send(200, graph.files[key]["content"])
                # ---- Graph: every call needs a live token
                if path in ("/v1.0/", "/v1.0"):
                    return self._send(400, {"error": {"code": "BadRequest", "message": "root"}})
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in graph.access_tokens:
                    return self._send(401, {"error": {"code": "InvalidAuthenticationToken", "message": "no"}})
                body = json.loads(raw) if raw else {}
                return self._graph(method, path, body)

            def _token(self, form):
                f = {k: v[0] for k, v in form.items()}
                if f.get("grant_type") == "authorization_code":
                    want = graph.codes.pop(f.get("code"), None)
                    good = want and b64url(hashlib.sha256(f.get("code_verifier", "").encode()).digest()) == want["challenge"]
                    if not good or f.get("redirect_uri") != "http://localhost":
                        return self._send(400, {"error": "invalid_grant", "error_description": "bad code"})
                elif f.get("grant_type") == "refresh_token":
                    if graph.revoked or f.get("refresh_token") not in graph.refresh_tokens:
                        return self._send(400, {"error": "invalid_grant",
                                                "error_description": "AADSTS700082: The refresh token has expired."})
                    graph.refresh_tokens.discard(f["refresh_token"])
                else:
                    return self._send(400, {"error": "unsupported_grant_type"})
                access, refresh = "at-" + uuid.uuid4().hex, "rt-" + uuid.uuid4().hex
                graph.access_tokens.add(access)
                graph.refresh_tokens.add(refresh)
                return self._send(200, {"access_token": access, "refresh_token": refresh, "expires_in": 3600,
                                        "token_type": "Bearer", "scope": f.get("scope", ""),
                                        "id_token": fake_jwt(graph.account)})

            # ---- Graph proper

            def _graph(self, method, path, body):
                seg = path.strip("/").split("/")
                version, rest = seg[0], seg[1:]
                if rest[:2] == ["planner", "plans"] and rest[3:] == ["buckets"] and method == "GET":
                    plan = graph.plans[rest[2]]
                    return self._send(200, {"value": [{"id": b, "name": n, "planId": rest[2]}
                                                      for b, n in plan["buckets"].items()]})
                if rest == ["planner", "buckets"] and method == "POST":
                    bid = graph.add_bucket(body["planId"], body["name"])
                    return self._send(201, {"id": bid, "name": body["name"], "planId": body["planId"]})
                if rest[:2] == ["planner", "buckets"] and rest[3:] == ["tasks"]:
                    tasks = [t for t in graph.tasks.values() if t["bucketId"] == rest[2]]
                    # one page at a time, to prove nextLink is followed
                    if len(tasks) > 1 and "page2" not in self.path:
                        return self._send(200, {"value": tasks[:1],
                                                "@odata.nextLink": f"{graph.base}{path}?page2=1"})
                    return self._send(200, {"value": tasks[1:] if "page2" in self.path else tasks})
                if rest[:2] == ["planner", "tasks"] and len(rest) == 3:
                    task = graph.tasks[rest[2]]
                    if method == "GET":
                        return self._send(200, task)
                    if method == "PATCH":
                        if self.headers.get("If-Match") != task["@odata.etag"]:
                            return self._send(412, {"error": {"code": "PreconditionFailed", "message": "etag"}})
                        task.update(body)
                        task["@odata.etag"] = f'W/"{int(task["@odata.etag"][3:-1]) + 1}"'
                        return self._send(204)
                if rest[:2] == ["planner", "tasks"] and rest[3:] == ["details"]:
                    d = graph.details[rest[2]]
                    if method == "GET":
                        return self._send(200, d)
                    if self.headers.get("If-Match") != d["@odata.etag"]:
                        return self._send(412, {"error": {"code": "PreconditionFailed", "message": "etag"}})
                    d.update(body)
                    d["@odata.etag"] = f'W/"{int(d["@odata.etag"][3:-1]) + 1}"'
                    return self._send(204)
                if rest[:2] == ["planner", "tasks"] and rest[3:] == ["messages"] and version == "beta":
                    if rest[2] not in graph.tasks:
                        return self._send(404, {"error": {"code": "NotFound", "message": "task"}})
                    if graph.message_status:
                        return self._send(graph.message_status, {"error": {"code": "X", "message": "comments down"}})
                    if body.get("mentions") and graph.mention_status:
                        return self._send(graph.mention_status, {"error": {"code": "X", "message": "not a member"}})
                    graph.messages.append((rest[2], body))
                    return self._send(201, {"id": uuid.uuid4().hex, "content": body.get("content")})
                if rest == ["me", "sendMail"] and method == "POST":
                    graph.mail.append(body)
                    return self._send(202)
                if rest[:1] == ["shares"] and rest[2:] == ["driveItem"]:
                    url = base64.urlsafe_b64decode(rest[1][2:] + "=" * (-len(rest[1][2:]) % 4)).decode()
                    if url not in graph.files:
                        return self._send(403, {"error": {"code": "accessDenied", "message": "no grant"}})
                    f = graph.files[url]
                    return self._send(200, {"id": "i1", "name": f["name"], "size": len(f["content"]),
                                            "eTag": f["etag"], "file": {"mimeType": "application/octet-stream"},
                                            "@microsoft.graph.downloadUrl":
                                                f"{graph.base}/download/{urllib.parse.quote(url, safe='')}"})
                return self._send(404, {"error": {"code": "NotFound", "message": f"no route {path}"}})

        return Handler

    # -- signing in from a test, without a browser

    def sign_in(self, source):
        """Run the browser sign-in against this server: returns the account."""
        import urllib.request

        pending = source.start_login()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            opener.open(pending["url"])
        except urllib.error.HTTPError as exc:
            location = exc.headers["Location"]
        return source.finish_login(location, pending)

    def landing_address(self, auth_url):
        """What the browser would end on, for an authorize address workq printed."""
        import urllib.error
        import urllib.request

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            opener.open(auth_url)
        except urllib.error.HTTPError as exc:
            return exc.headers["Location"]
        raise AssertionError("authorize did not redirect")


import urllib.error  # noqa: E402  (used by sign_in)
time = time
