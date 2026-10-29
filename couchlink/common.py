# SPDX-License-Identifier: GPL-3.0-or-later
"""Logging and constants shared by every part of CouchLink."""

import os
import tempfile
import time
from collections import deque


DEFAULT_CONFIG_PATH = "/etc/couchlink/config.toml"


# The service runs as the couchlink user. Its home and state folder holds the SSH key the
# PC trusts. Every ssh call names the key and known_hosts, so they're the same whether the
# service runs it or root does, from sudo couchlink --setup-ssh.
STATE_DIR = "/var/lib/couchlink"
SSH_KEY = f"{STATE_DIR}/.ssh/id_ed25519"
KNOWN_HOSTS = f"{STATE_DIR}/.ssh/known_hosts"


def ssh_command(*args):
    """An ssh command line with CouchLink's own key and known_hosts, that never asks anything."""
    return ["ssh", "-i", SSH_KEY, "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", *args]


def own_like(path, reference):
    """When root writes a file the service uses, such as from sudo couchlink --setup, give it
    the same owner as reference: the couchlink user's folder. Otherwise the service couldn't
    read or replace it."""
    if getattr(os, "geteuid", lambda: -1)() == 0:
        st = os.stat(reference)
        os.chown(path, st.st_uid, st.st_gid)


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


def write_private(path, text, mode=0o600):
    """Replace path with text, readable by root only unless mode says otherwise. The text
    goes to a new file next to it, which then takes its place in one step. A power cut
    leaves the old file or the new one, never an empty or half-written one."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, temp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w") as f:           # mkstemp makes it 0600
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode != 0o600:
            os.chmod(temp, mode)
        own_like(temp, folder)
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    sync_folder(folder)


def sync_folder(folder):
    """Make a rename in folder survive a power cut. Windows, where the tests also run, can't."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


DOCS_URL = "https://github.com/blackhat840/CouchLink/blob/main/docs"
