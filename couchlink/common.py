# SPDX-License-Identifier: GPL-3.0-or-later
"""Logging and constants shared by every part of CouchLink."""

import time
from collections import deque


DEFAULT_CONFIG_PATH = "/etc/couchlink/config.toml"


EX_CONFIG = 78


LOG_RING = deque(maxlen=200)


TRAFFIC_RING = deque(maxlen=100)


SHOW_DEBUG = True           # set from raw_debug at startup


def log(component, message, level="INFO"):
    if level == "DEBUG" and not SHOW_DEBUG:
        return
    line = f"[{level}] [{component}] {message}"
    if level != "DEBUG":
        LOG_RING.append(f"{time.strftime('%H:%M:%S')} {line}")
    print(line, flush=True)


DOCS_URL = "https://github.com/blackhat840/CouchLink/blob/main/docs"
