# SPDX-License-Identifier: GPL-3.0-or-later
"""The status page and its API."""

import json
import os
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .admin import Admin
from .auth import KeyGuard, bearer_key, find_api_key
from .common import log


POST_ACTIONS = {"/api/wake": "wake", "/api/sleep": "sleep", "/api/session/end": "end_session"}

# The status page loads nothing from the internet, so it works without internet
# access. It polls /api every 2 seconds.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_PATHS = {"/": "status.html", "/index.html": "status.html", "/style.css": "style.css",
                "/status.js": "status.js", "/admin": "admin.html", "/admin/": "admin.html",
                "/admin.js": "admin.js"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8"}


def load_static():
    """The page's files, read once at startup: name -> (bytes, content type)."""
    files = {}
    for name in set(STATIC_PATHS.values()):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            files[name] = (f.read(), CONTENT_TYPES[os.path.splitext(name)[1]])
    return files


def own_address(toward):
    """The Pi's IP address on the route to another host, or None. Nothing is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((toward, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def status_page_urls(port, ip, hostname):
    """Where to open the status page, best first."""
    urls = [f"http://{ip}:{port}"] if ip else []
    if hostname:
        urls.append(f"http://{hostname}.local:{port}")
    return urls or [f"http://<pi-address>:{port}"]


def from_status_page(headers):
    """Whether a browser says the request came from the status page itself. Browsers set these
    headers and other web sites can't change them, but any program on the network can."""
    if (headers.get("Sec-Fetch-Site") or "") == "same-origin":
        return True
    origin = headers.get("Origin")
    return bool(origin) and origin != "null" and \
        origin.split("://", 1)[-1].rstrip("/").lower() == (headers.get("Host") or "").lower()


def request_problem(headers):
    """Why a POST to the API is refused, or None. Refuses requests a browser sends from another web site."""
    origin = headers.get("Origin")
    if origin and origin != "null":
        host = origin.split("://", 1)[-1].rstrip("/")
        if host.lower() != (headers.get("Host") or "").lower():
            return "Refused a request from another web site"
    elif origin == "null":
        return "Refused a request from another web site"
    if (headers.get("Sec-Fetch-Site") or "same-origin") not in ("same-origin", "none"):
        return "Refused a request from another web site"
    return None


def start_dashboard(port, controller, events=None, config_path=None):
    """Serve the status page, /api and the settings page. Returns the server, or None if the
    port is taken."""
    files = load_static()
    guard = KeyGuard()
    admin = Admin(controller, events, config_path, guard)

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, payload, headers=()):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def answer(self, status, ok, result, message):
            self.send_json(status, {"ok": ok, "result": result, "message": message})

        def read_json(self):
            """The request body as a dict, or None."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if not 0 <= length <= 16384:
                return None
            try:
                params = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return None
            return params if isinstance(params, dict) else None

        def do_POST(self):
            source = self.client_address[0]
            path = self.path.split("?")[0]
            if path.startswith("/admin/"):
                params = self.read_json()
                if params is None:
                    return self.answer(400, False, "bad_request", "The body must be a JSON object, such as {}")
                after = None
                if not from_status_page(self.headers):
                    # Only the settings page itself, so another web site can't use your login
                    status, payload, headers = 403, {"ok": False, "result": "refused",
                                                     "message": "Use the settings page"}, ()
                else:
                    status, payload, headers, after = admin.post(path, params, self.headers, source)
                self.send_json(status, payload, headers)
                if after:
                    after()
                return
            name = POST_ACTIONS.get(path)
            if name is None:
                return self.answer(404, False, "not_found", "No such action")
            if events is None:
                return self.answer(503, False, "no_answer", "CouchLink is not running")
            problem = request_problem(self.headers)
            if problem:
                log("ACTION", f"Refused a request from {source} to {self.path}: {problem}", level="WARN")
                return self.answer(403, False, "refused", problem)
            keys = controller.cfg.api_keys
            if from_status_page(self.headers):
                # The status page's buttons need no key. The network is what protects them.
                if not controller.cfg.status_page_buttons:
                    return self.answer(403, False, "buttons_off",
                                       "The status page buttons are off (status_page_buttons = false)")
                source += " (status page)"
            elif not keys:
                return self.answer(403, False, "actions_off",
                                   "Scripts need an API key. Generate one with: sudo couchlink --new-api-key")
            else:
                if guard.locked(source):
                    return self.answer(429, False, "locked_out", "Too many wrong API keys. Try again in a minute")
                key = bearer_key(self.headers)
                key_name = find_api_key(key, keys)
                if key_name is None:
                    guard.failed(source)
                    log("ACTION", f"{'Wrong' if key else 'Missing'} API key from {source} for {self.path}",
                        level="WARN")
                    return self.answer(401, False, "bad_key", "Wrong API key" if key else
                                       "Send the API key as: Authorization: Bearer <key>")
                source += f" (key {key_name})"
            params = self.read_json()
            if params is None:
                return self.answer(400, False, "bad_request", "The body must be a JSON object, such as {}")
            reply = queue.Queue()
            events.put(("action", name, params, source, reply))
            try:
                ok, result, message = reply.get(timeout=5)
            except queue.Empty:
                return self.answer(503, False, "no_answer", "CouchLink did not answer in time")
            self.answer(200 if ok else 429 if result == "too_soon" else 400, ok, result, message)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/admin/api/state":
                status, payload = admin.state(self.headers)
                return self.send_json(status, payload)
            if path.startswith("/api"):
                body = json.dumps(controller.snapshot(), indent=2).encode()
                ctype = "application/json"
            elif path in STATIC_PATHS:
                body, ctype = files[STATIC_PATHS[path]]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        log("STARTUP", f"Status page disabled, port {port} unavailable: {e}", level="WARN")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    urls = status_page_urls(port, own_address(controller.cfg.pc_host), socket.gethostname())
    log("STARTUP", f"Status page on {' or '.join(urls)}")
    if controller.cfg.api_keys:
        names = ", ".join(sorted(controller.cfg.api_keys))
        log("STARTUP", f"The API is on for scripts, with these keys: {names}")
    return server
