# SPDX-License-Identifier: GPL-3.0-or-later
"""API keys, the settings page's password and login cookie, and lockouts after
wrong keys or passwords."""

import hashlib
import hmac
import os
import re
import secrets
import threading
import time

PASSWORD_ITERATIONS = 100_000       # about a second on a Pi Zero, so guessing is slow
PASSWORD_RE = re.compile(r"pbkdf2_sha256\$(\d+)\$([0-9a-f]{32})\$([0-9a-f]{64})")
SESSION_DAYS = 30
KEY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")


def hash_api_key(key):
    """What the config stores instead of the key itself."""
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def new_api_key():
    return secrets.token_urlsafe(24)


def find_api_key(key, keys):
    """The name of the key in keys (name -> hash) that matches, or None."""
    if not key:
        return None
    hashed = hash_api_key(key)
    match = None
    for name, stored in keys.items():
        if hmac.compare_digest(hashed, str(stored)):
            match = name        # keep looping, so the time taken doesn't depend on which one matched
    return match


def bearer_key(headers):
    """The key from an "Authorization: Bearer <key>" header, or None."""
    value = headers.get("Authorization") or ""
    scheme, _, key = value.partition(" ")
    return key.strip() if scheme.lower() == "bearer" and key.strip() else None


def hash_password(password, iterations=None, salt=None):
    """What the config stores instead of the password: a salted PBKDF2 hash."""
    iterations = iterations or PASSWORD_ITERATIONS
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    m = PASSWORD_RE.fullmatch(stored or "")
    if not m or not password:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(m.group(2)), int(m.group(1)))
    return hmac.compare_digest(digest.hex(), m.group(3))


def make_session(secret, now=None):
    """A login cookie value: when it expires, signed with secret (the password hash).
    It survives restarts, and stops working when the password changes."""
    expires = int((now if now is not None else time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(secret.encode(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def session_ok(token, secret, now=None):
    if not token or not secret:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit():
        return False
    good = hmac.new(secret.encode(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and int(expires) > (now if now is not None else time.time())


def cookie(headers, name):
    """A cookie's value from the request headers, or None."""
    for part in (headers.get("Cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


class KeyGuard:
    """Locks out an address for a minute after 5 wrong API keys or passwords within a minute."""
    LIMIT, WINDOW = 5, 60

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.failures = {}

    def locked(self, source):
        with self.lock:
            t = self.clock()
            recent = [x for x in self.failures.get(source, []) if t - x < self.WINDOW]
            self.failures[source] = recent
            return len(recent) >= self.LIMIT

    def failed(self, source):
        with self.lock:
            self.failures.setdefault(source, []).append(self.clock())
