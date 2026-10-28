# SPDX-License-Identifier: GPL-3.0-or-later
"""Talking to the HDMI-CEC bus through cec-client, and reading what it prints."""

import queue
import re
import subprocess
import threading
import time
from types import SimpleNamespace

from .common import TRAFFIC_RING, log
from .config import normalize_addr


KEYCODES = {
    'up': 0x52, 'down': 0x51, 'left': 0x50, 'right': 0x4F,
    'select': 0x28, 'exit': 0x29, 'back': 0x29,
    'play': 0x28, 'pause': 0x2C, 'stop': 0x29,
    'rewind': 0x50, 'fast forward': 0x4F,
    'root menu': 0x29, 'power': 0x66,
}


LOGICAL_NAMES = {
    0: "TV", 1: "recording device 1", 2: "recording device 2", 3: "tuner 1",
    4: "playback device 1", 5: "audio system", 6: "tuner 2", 7: "tuner 3",
    8: "playback device 2", 9: "recording device 3", 10: "tuner 4",
    11: "playback device 3", 14: "specific-use device",
}


ANNOUNCE_OPCODES = {"82": "Active Source", "86": "Set Stream Path",
                    "80": "Routing Change", "81": "Routing Information"}


POWER_STATUS = {"00": "on", "01": "standby", "02": "turning on", "03": "turning off"}


RX_RE = re.compile(r">>\s*([0-9a-f]{2}(?::[0-9a-f]{2})*)", re.IGNORECASE)


TX_RE = re.compile(r"<<\s*([0-9a-f]{2}(?::[0-9a-f]{2})*)", re.IGNORECASE)


KEY_RE = re.compile(r"key pressed:\s*'?([a-zA-Z ]+)'?", re.IGNORECASE)


def parse_rx(line):
    """Parse a received cec-client traffic line into a message, or None."""
    return _parse_traffic(RX_RE, line)


def parse_tx(line):
    """Parse a line for a message the Pi sent (ours or cec-client's own replies), or None."""
    return _parse_traffic(TX_RE, line)


def _parse_traffic(regex, line):
    m = regex.search(line)
    if not m:
        return None
    data = m.group(1).lower().split(":")
    if len(data) < 2:
        return None                     # polling message
    return SimpleNamespace(
        initiator=int(data[0][0], 16), destination=int(data[0][1], 16),
        opcode=data[1], params=data[2:], broadcast=(data[0][1] == "f"),
        raw=m.group(1).lower())


def parse_key(line):
    m = KEY_RE.search(line)
    return m.group(1).strip().lower() if m else None


def device_name(logical):
    return "TV" if logical == 0 else f"{LOGICAL_NAMES.get(logical, 'device')} ({logical})"


class CecClient:
    """Owns the cec-client process. Posts ("cec", msg) and ("key", name) events."""

    def __init__(self, command, events, raw_debug):
        self.events = events
        self.raw_debug = raw_debug
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()

    def stop(self):
        """Close cec-client, so a new CouchLink can open the CEC adapter."""
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()

    def send(self, command):
        with self.lock:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()

    def _reader(self):
        for line in self.proc.stdout:
            if ">>" in line or "<<" in line:
                TRAFFIC_RING.append(f"{time.strftime('%H:%M:%S')} {line.strip()}")
                if self.raw_debug:
                    log("CEC", line.strip(), level="DEBUG")
            key = parse_key(line)
            if key:
                self.events.put(("key", key))
            msg = parse_rx(line)
            if msg:
                self.events.put(("cec", msg))
            else:
                out = parse_tx(line)
                if out:
                    self.events.put(("cec_out", out))
        self.events.put(("cec_exit", self.proc.wait()))


SCAN_DEVICE_RE = re.compile(r"^device #([0-9a-f]+):\s*(.*)$", re.IGNORECASE)


SCAN_FIELD_RE = re.compile(r"^(address|vendor|osd string):\s*(.*)$", re.IGNORECASE)


def parse_scan(text):
    """The devices in cec-client's "scan" output, as dicts with logical, name, address, vendor, osd."""
    devices = []
    for line in text.splitlines():
        line = line.strip()
        m = SCAN_DEVICE_RE.match(line)
        if m:
            devices.append({"logical": int(m.group(1), 16), "name": m.group(2).strip(),
                            "address": None, "vendor": "", "osd": ""})
            continue
        m = SCAN_FIELD_RE.match(line)
        if m and devices:
            field, value = m.group(1).lower(), m.group(2).strip()
            if field == "address":
                devices[-1]["address"] = normalize_addr(value)
            else:
                devices[-1]["vendor" if field == "vendor" else "osd"] = value
    return [d for d in devices if d["address"]]


def announced_address(msg):
    """The input a CEC message says is now active, or None."""
    p = msg.params if msg else []
    if not (msg and msg.broadcast):
        return None
    if msg.opcode in ("82", "86", "81") and len(p) >= 2:
        return f"{p[0]}:{p[1]}"
    if msg.opcode == "80" and len(p) >= 4:
        return f"{p[2]}:{p[3]}"
    return None


def dotted(addr):
    """'12:00' -> '1.2.0.0'."""
    return ".".join(addr.replace(":", ""))


def cec_scan(command):
    """Ask every device on the bus who it is. Returns parse_scan's list."""
    try:
        r = subprocess.run([command[0], "-s", "-d", "1"], input="scan\n", capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_scan(r.stdout)


def cec_watch(command, seconds, wanted, out=print):
    """Listen to the bus until a message names an input that wanted() accepts. Returns it or None."""
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError:
        return None
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()
    try:
        ready = time.monotonic() + 15
        while time.monotonic() < ready:         # cec-client takes a few seconds to open the adapter
            try:
                if "waiting for input" in lines.get(timeout=1).lower():
                    break
            except queue.Empty:
                pass
        out(f"Listening now. You have {seconds} seconds.")
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                addr = announced_address(parse_rx(lines.get(timeout=1)))
            except queue.Empty:
                continue
            if addr and wanted(addr):
                return addr
        return None
    finally:
        proc.kill()
