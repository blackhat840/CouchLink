# SPDX-License-Identifier: GPL-3.0-or-later
"""Watching the PC: ping, and the SSH session that hears it sleep, restart or
shut down."""

import subprocess
import time

from .common import log


# Run on the PC by the power watcher. gdbus comes with GLib, which SteamOS and
# Bazzite both have. Without it, the session still tells shutdown (it closes)
# from sleep (it freezes).
POWER_WATCH_COMMAND = ("if command -v gdbus >/dev/null 2>&1; then exec gdbus monitor --system "
                       "--dest org.freedesktop.login1 --object-path /org/freedesktop/login1; "
                       "else echo couchlink-watching; exec sleep infinity; fi")


def parse_power_line(line):
    """One line of gdbus monitor output -> sleep | resume | reboot | poweroff | shutdown | None."""
    if "PrepareForSleep (true" in line:
        return "sleep"
    if "PrepareForSleep (false" in line:
        return "resume"
    if "PrepareForShutdownWithMetadata (true" in line:
        return "reboot" if "'reboot'" in line or "'kexec'" in line else "poweroff"
    if "PrepareForShutdown (true" in line:
        return "shutdown"
    return None


def power_link_end(stderr):
    """How the watcher's SSH session ended: lost (the PC stopped answering, as
    in sleep) or closed (the PC ended it, as in shutdown)."""
    return "lost" if "not responding" in stderr or "timed out" in stderr.lower() else "closed"


def power_watcher(cfg, events):
    """Keep an SSH session open while the PC is up and post ("pc_power", ...) events."""
    target = f"{cfg.ssh_user}@{cfg.pc_host}"
    warned = False
    while True:
        try:
            up = subprocess.run(["ping", "-c", "1", "-W", "1", cfg.pc_host], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            up = False
        if not up:
            time.sleep(cfg.ping_interval)
            continue
        connected = False
        try:
            proc = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                 "-o", "ServerAliveCountMax=3", target, POWER_WATCH_COMMAND],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in proc.stdout:
                if not connected:
                    connected = True
                    warned = False
                    events.put(("pc_power", "link_up"))
                what = parse_power_line(line)
                if what:
                    events.put(("pc_power", what))
            proc.wait()
            err = proc.stderr.read()
        except OSError as e:
            err = str(e)
        if connected:
            events.put(("pc_power", "link_" + power_link_end(err)))
        else:
            if not warned:
                log("WATCHER", f"Can't watch the PC's power state over SSH ({err.strip() or 'no answer'}). "
                               "The status page shows 'Asleep or off' until the SSH key works", level="WARN")
                warned = True
            time.sleep(30)
        time.sleep(cfg.ping_interval)


def pinger(cfg, events):
    warned = False
    while True:
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "1", cfg.pc_host],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            events.put(("ping", r.returncode == 0))
            warned = False
        except Exception as e:      # keep the watcher alive no matter what
            if not warned:
                log("WATCHER", f"Can't ping the PC ({e!r}). Install iputils-ping. "
                               "PC state stays unknown until this works", level="ERROR")
                warned = True
        time.sleep(cfg.ping_interval)
