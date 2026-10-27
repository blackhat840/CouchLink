# SPDX-License-Identifier: GPL-3.0-or-later
"""API keys and lockouts after wrong keys."""

import hashlib
import threading
import time


def hash_api_key(key):
    """What the config stores instead of the key itself."""
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def bearer_key(headers):
    """The key from an "Authorization: Bearer <key>" header, or None."""
    value = headers.get("Authorization") or ""
    scheme, _, key = value.partition(" ")
    return key.strip() if scheme.lower() == "bearer" and key.strip() else None


class KeyGuard:
    """Locks out an address for a minute after 5 wrong API keys within a minute."""
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
