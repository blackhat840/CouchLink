# SPDX-License-Identifier: GPL-3.0-or-later
"""The status page and its API."""

import hmac
import json
import os
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .auth import KeyGuard, bearer_key, hash_api_key
from .common import log


POST_ACTIONS = {"/api/wake": "wake", "/api/sleep": "sleep", "/api/session/end": "end_session"}

# The status page loads nothing from the internet, so it works without internet
# access. It polls /api every 2 seconds.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_PATHS = {"/": "status.html", "/index.html": "status.html", "/style.css": "style.css",
                "/status.js": "status.js"}
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


def start_dashboard(port, controller, events=None):
    """Serve the status page and /api. Returns the server, or None if the port is taken."""
    files = load_static()
    guard = KeyGuard()

    class Handler(BaseHTTPRequestHandler):
        def answer(self, status, ok, result, message):
            body = json.dumps({"ok": ok, "result": result, "message": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            name = POST_ACTIONS.get(self.path)
            source = self.client_address[0]
            if name is None:
                return self.answer(404, False, "not_found", "No such action")
            if events is None:
                return self.answer(503, False, "no_answer", "CouchLink is not running")
            problem = request_problem(self.headers)
            if problem:
                log("ACTION", f"Refused a request from {source} to {self.path}: {problem}", level="WARN")
                return self.answer(403, False, "refused", problem)
            stored = controller.cfg.api_key_hash
            if from_status_page(self.headers):
                # The status page's buttons need no key. The network is what protects them.
                if not controller.cfg.status_page_buttons:
                    return self.answer(403, False, "buttons_off",
                                       "The status page buttons are off (status_page_buttons = false)")
                source += " (status page)"
            elif not stored:
                return self.answer(403, False, "actions_off",
                                   "Scripts need an API key. Generate one with: sudo couchlink --new-api-key")
            else:
                if guard.locked(source):
                    return self.answer(429, False, "locked_out", "Too many wrong API keys. Try again in a minute")
                key = bearer_key(self.headers)
                if key is None or not hmac.compare_digest(hash_api_key(key), stored):
                    guard.failed(source)
                    log("ACTION", f"{'Wrong' if key else 'Missing'} API key from {source} for {self.path}",
                        level="WARN")
                    return self.answer(401, False, "bad_key", "Wrong API key" if key else
                                       "Send the API key as: Authorization: Bearer <key>")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= 4096:
                return self.answer(400, False, "bad_request", "The request body is too large")
            try:
                params = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                params = None
            if not isinstance(params, dict):
                return self.answer(400, False, "bad_request", "The body must be a JSON object, such as {}")
            reply = queue.Queue()
            events.put(("action", name, params, source, reply))
            try:
                ok, result, message = reply.get(timeout=5)
            except queue.Empty:
                return self.answer(503, False, "no_answer", "CouchLink did not answer in time")
            self.answer(200 if ok else 429 if result == "too_soon" else 400, ok, result, message)

        def do_GET(self):
            if self.path.startswith("/api"):
                body = json.dumps(controller.snapshot(), indent=2).encode()
                ctype = "application/json"
            elif self.path.split("?")[0] in STATIC_PATHS:
                body, ctype = files[STATIC_PATHS[self.path.split("?")[0]]]
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
    if controller.cfg.api_key_hash:
        log("STARTUP", "The API is on for scripts. They need the API key")
    return server
