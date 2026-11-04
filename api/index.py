"""The demo site's only server code: one Vercel Python function that serves every path.

- Every path except /login needs the session cookie, which only the site password
  (SITE_PASSWORD) can get; with no password configured, nobody gets in.
- GET  /                     the app (site/app.html)
- GET  /data/personas.json   persona recaps built ahead of time
- GET  /gallery              the review gallery for the same recaps
- GET  /downloads/<file>     the template and persona workbooks
- POST /api/recap            an uploaded workbook (raw .xlsx bytes) -> recap JSON;
                             calls Claude with ANTHROPIC_API_KEY
- Uploads are processed in memory and never stored; log lines carry counts only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SITE = ROOT / "site"
COOKIE = "recap_demo"
MAX_UPLOAD = 4 * 1024 * 1024          # Vercel's request body limit is 4.5 MB
SESSION_DAYS = 30
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
SAFE_DOWNLOAD = re.compile(r"^[a-z0-9_\-]+(?:\.[a-z0-9_\-]+)*\.xlsx$")


def _token(password: str) -> str:
    return hmac.new(password.encode("utf-8"), b"recap-demo-session-v1", hashlib.sha256).hexdigest()


def _password() -> str:
    return os.environ.get("SITE_PASSWORD", "")


class handler(BaseHTTPRequestHandler):  # noqa: N801  (Vercel looks for this name)
    server_version = "recap-demo"
    sys_version = ""
    # HTTP/1.1: every response sends Content-Length and every request body is read in
    # full. Under the default HTTP/1.0 about 1 in 10 local requests stalled (timed out).
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):      # no request lines in the logs
        pass

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), TYPES[".json"])

    def _file(self, path: Path, headers: dict | None = None) -> None:
        if not path.is_file():
            return self._json(404, {"error": "not found"})
        self._send(200, path.read_bytes(), TYPES.get(path.suffix, "application/octet-stream"), headers)

    def _redirect(self, to: str, cookie: str | None = None) -> None:
        headers = {"Location": to}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(303, b"", "text/plain", headers)

    def _authed(self) -> bool:
        pw = _password()
        if not pw:
            return False
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and hmac.compare_digest(value, _token(pw)):
                return True
        return False

    # ------------------------------------------------------------ routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self._file(SITE / "login.html")
        if path == "/logout":
            return self._redirect("/login", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")
        if not self._authed():
            return self._redirect("/login") if not path.startswith(("/api/", "/data/")) \
                else self._json(401, {"error": "Sign in first."})
        if path in ("/", "/index.html"):
            return self._file(SITE / "app.html")
        if path == "/data/personas.json":
            return self._file(SITE / "data" / "personas.json")
        if path == "/gallery":
            return self._file(SITE / "gallery.html")
        if path.startswith("/downloads/"):
            name = path.removeprefix("/downloads/")
            if not SAFE_DOWNLOAD.match(name):
                return self._json(404, {"error": "not found"})
            return self._file(SITE / "downloads" / name,
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        # Read the whole body before answering: replying with unread data on the socket
        # can reset the connection before the client sees the reply.
        data, left = b"", length
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            if len(data) <= MAX_UPLOAD:
                data += chunk
            left -= len(chunk)
        if path == "/login":
            body = data[:4096].decode("utf-8", "replace")
            given = (parse_qs(body).get("password") or [""])[0]
            pw = _password()
            if pw and hmac.compare_digest(given.encode("utf-8"), pw.encode("utf-8")):
                cookie = (f"{COOKIE}={_token(pw)}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
                          "HttpOnly; Secure; SameSite=Lax")
                return self._redirect("/", cookie)
            return self._redirect("/login?e=1" if pw else "/login?e=setup")
        if not self._authed():
            return self._json(401, {"error": "Sign in first."})
        if path != "/api/recap":
            return self._json(404, {"error": "not found"})
        if length <= 0:
            return self._json(400, {"problems": ["The upload was empty. Choose a workbook first."]})
        if length > MAX_UPLOAD:
            return self._json(413, {"problems": ["The workbook is larger than 4 MB. Use a smaller sample of trades."]})
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return self._json(503, {"problems": ["The site's Claude API key is not set up yet."]})
        from pipeline.demo import run_upload
        from pipeline.workbook import WorkbookError
        try:
            result = run_upload(data)
        except WorkbookError as e:
            print(f"recap upload: refused, {len(e.problems)} problem(s)")
            return self._json(400, {"problems": e.problems})
        except Exception as e:  # noqa: BLE001  (log the type only; never the workbook)
            print(f"recap upload: failed with {type(e).__name__}")
            return self._json(500, {"problems": ["Something went wrong while writing the recap. Try again in a minute."]})
        print(f"recap upload: {len(data)} bytes, {len(result['facts'])} facts, {result['status']}, "
              f"{len(result['attempts'])} attempt(s), ${result['cost_usd']}")
        return self._json(200, result)
