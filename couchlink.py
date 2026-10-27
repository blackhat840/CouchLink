#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
CouchLink 2.5.1

Makes a SteamOS or Bazzite gaming PC behave like a console on the TV. Runs on a
Raspberry Pi on the TV's HDMI-CEC bus and keeps the PC's power state in line
with the TV:

    TV on the PC's input              ->  PC awake   (Wake-on-LAN, retried)
    TV on any other input, or standby ->  PC asleep  (suspend over SSH, retried)

and the other way round: if the PC wakes by itself (controller, power button)
the TV turns on and switches to it, and if the PC goes to sleep by itself the
TV can switch to another input.

Design notes:
  * One controller thread owns all state. The CEC reader, the pinger and the
    SSH worker only post events to a queue, so there are no shared-state locks
    and the logic can be tested with a simulated clock.
  * Settings live in /etc/couchlink/config.toml, so updating never touches them.
  * --dry-run watches the bus and logs what it would do without doing it.

Talks to the CEC bus through cec-client from libcec (Pulse-Eight,
GPL-2.0-or-later), which it runs as a separate program. See docs/credits.md.

Run with --help for options. See README.md and docs/ for setup.
"""
import argparse
import difflib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

VERSION = "2.5.1"
DEFAULT_CONFIG_PATH = "/etc/couchlink/config.toml"
EX_CONFIG = 78

# --------------------------------------------------------------------------
# settings and their defaults (override them in the TOML config file)
# --------------------------------------------------------------------------
DEFAULTS = {
    # target PC
    "pc_host": None,                    # IP (recommended) or hostname
    "pc_name": None,                    # name shown on the status page; looked up from the PC if unset
    "pc_mac": None,                     # MAC of the network card that receives Wake-on-LAN
    "ssh_user": "deck",
    "suspend_command": "sudo -n systemctl suspend",
    "wol_broadcast": "255.255.255.255", # or your subnet broadcast, e.g. "192.168.1.255"
    "wol_port": 9,

    # CEC physical addresses ("30:00" or "3.0.0.0")
    "pc_addresses": None,               # TV input(s) that mean "the PC is on screen"
    "pi_address": None,                 # TV input the Pi is plugged into
    "soundbar_address": None,           # optional: TV input the soundbar (ARC) is on
    "soundbar_pc_audio_address": None,  # optional: soundbar input carrying the PC's audio
    "guard_addresses": [],              # extra inputs to treat as automatic switches after a wake
    "pi_logical_address": 1,            # cec-client registers as recording device 1 by default
    "ignore_announcements_from": [],    # logical addresses to ignore, e.g. [5] for the soundbar

    # behavior
    "claim_pc_input": True,             # Pi answers for the PC's input (Active Source)
    "select_soundbar_audio": False,     # ask the soundbar to switch to soundbar_pc_audio_address
    "auto_switch_on_pc_wake": "always", # "always" | "if_tv_on" | "never"
    "on_pc_sleep": "nothing",           # PC went to sleep while on screen: "switch" | "standby" | "nothing"
    "pc_sleep_switch_to": None,         # with "switch": the input to change to, e.g. "11:00"
    "verify_non_tv_standby": True,
    "watch_pc_power": True,             # keep an SSH session open to hear if the PC sleeps, restarts or shuts down
    "watch_routing_messages": True,
    "hid_passthrough": True,
    "hidg_device": "/dev/hidg0",
    "dashboard_port": 8080,             # 0 disables the status page

    # timing (seconds)
    "away_debounce": 5,
    "suspend_retry_interval": 20,
    "max_suspend_attempts": 4,
    "wol_retry_interval": 10,
    "max_wol_attempts": 6,
    "wake_correlation": 45,             # PC online this soon after our WoL = our wake
    "guard_window": 15,                 # after a wake / PC coming online
    "guard_delay": 1.5,
    "max_guard_recoveries": 2,
    "auto_switch_cooldown": 30,
    "tv_boot_timeout": 20,              # PC woke with the TV off: wait this long for the TV to report on
    "tv_boot_reclaims": 3,              # then claim the PC's input this many more times
    "tv_boot_reclaim_interval": 2,      # this far apart, while the TV finishes starting
    "pc_sleep_confirm": 12,             # PC must stay offline this long before it counts as asleep
    "power_poll_interval": 60,          # 0 disables periodic TV power checks
    "power_query_delays": [2, 8],
    "ping_interval": 3,
    "tick_interval": 1,

    # diagnostics
    "raw_debug": True,
    "cec_client_command": ["cec-client", "-d", "8", "-o", "CouchLink"],  # -o: name the TV shows for the Pi
}

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
PC_ANNOUNCED = {"sleep": "asleep", "reboot": "restarting", "poweroff": "off", "shutdown": "off"}
TV_BOOT_POLL = 2            # seconds between "is the TV on yet?" queries while it starts

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


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def normalize_addr(value):
    """'30:00', '3000' or '3.0.0.0' -> '30:00'. None if invalid."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]\.[0-9a-f]\.[0-9a-f]\.[0-9a-f]", v):
        d = v.replace(".", "")
    elif re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{2}", v):
        d = v.replace(":", "")
    elif re.fullmatch(r"[0-9a-f]{4}", v):
        d = v
    else:
        return None
    return f"{d[:2]}:{d[2:]}"


def load_config(path):
    """Returns (settings dict, problems list)."""
    if tomllib is None:
        return None, ["Python 3.11+ (or the 'tomli' package) is needed to read the config file"]
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return None, [f"Config file {path} not found (copy couchlink.example.toml there)"]
    except tomllib.TOMLDecodeError as e:
        return None, [f"Config file {path} is not valid TOML: {e}"]
    flat = {}
    for key, value in raw.items():          # [sections] are only for readability
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    problems = [f"Unknown setting '{k}' (typo?)" for k in sorted(set(flat) - set(DEFAULTS))]
    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in flat.items() if k in DEFAULTS})
    return settings, problems


def validate(settings):
    """Checks and normalizes settings. Returns (namespace, problems)."""
    s = dict(settings)
    problems = []

    if not isinstance(s["pc_host"], str) or not s["pc_host"] or s["pc_host"] == "target-pc.local":
        problems.append("pc_host is not set")
    mac = s["pc_mac"]
    if not isinstance(mac, str) or not re.fullmatch(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", mac, re.I) \
            or mac.upper().replace("-", ":") == "AA:BB:CC:DD:EE:FF":
        problems.append("pc_mac is not set or not in the form aa:bb:cc:dd:ee:ff")
    if s["pc_name"] is not None and (not isinstance(s["pc_name"], str)
                                     or not s["pc_name"].strip() or len(s["pc_name"]) > 40):
        problems.append("pc_name must be text of 1 to 40 characters")
    for key in ("ssh_user", "suspend_command"):
        if not isinstance(s[key], str) or not s[key].strip():
            problems.append(f"{key} must be a non-empty string")

    pcs = s["pc_addresses"]
    if isinstance(pcs, str):
        pcs = [pcs]
    if not isinstance(pcs, list) or not pcs:
        problems.append("pc_addresses is not set (e.g. [\"30:00\"])")
        pcs = []
    norm_pcs = []
    for a in pcs:
        n = normalize_addr(a)
        if n is None or n in ("00:00", "ff:ff"):
            problems.append(f"pc_addresses entry {a!r} is not a valid input address")
        else:
            norm_pcs.append(n)
    s["pc_addresses"] = norm_pcs

    for key, required in (("pi_address", True), ("soundbar_address", False),
                          ("soundbar_pc_audio_address", False)):
        if s[key] is None:
            if required:
                problems.append(f"{key} is not set")
            continue
        n = normalize_addr(s[key])
        if n is None or n in ("00:00", "ff:ff"):
            problems.append(f"{key} {s[key]!r} is not a valid input address")
        s[key] = n
    guards = []
    for a in s["guard_addresses"] or []:
        n = normalize_addr(a)
        if n is None:
            problems.append(f"guard_addresses entry {a!r} is not a valid address")
        else:
            guards.append(n)
    s["guard_addresses"] = guards

    for key in ("pi_address", "soundbar_address"):
        if s[key] and s[key] in norm_pcs:
            problems.append(f"{key} {s[key]} is also listed in pc_addresses. If the PC is "
                            "behind the soundbar, leave soundbar_address unset")
    if not isinstance(s["watch_pc_power"], bool):
        problems.append("watch_pc_power must be true or false")
    if s["select_soundbar_audio"] and not s["soundbar_pc_audio_address"]:
        problems.append("select_soundbar_audio needs soundbar_pc_audio_address")

    for la in s["ignore_announcements_from"]:
        if not isinstance(la, int) or not 1 <= la <= 14:
            problems.append(f"ignore_announcements_from entry {la!r} must be 1-14 (the TV, 0, can't be ignored)")
    if not isinstance(s["pi_logical_address"], int) or not 1 <= s["pi_logical_address"] <= 14:
        problems.append("pi_logical_address must be 1-14")
    if s["auto_switch_on_pc_wake"] not in ("always", "if_tv_on", "never"):
        problems.append("auto_switch_on_pc_wake must be \"always\", \"if_tv_on\" or \"never\"")
    if s["on_pc_sleep"] not in ("switch", "standby", "nothing"):
        problems.append("on_pc_sleep must be \"switch\", \"standby\" or \"nothing\"")
    if s["pc_sleep_switch_to"] is not None:
        n = normalize_addr(s["pc_sleep_switch_to"])
        if n is None or n == "ff:ff":
            problems.append(f"pc_sleep_switch_to {s['pc_sleep_switch_to']!r} is not a valid address")
        elif n in norm_pcs:
            problems.append("pc_sleep_switch_to can't be one of the PC's own addresses")
        s["pc_sleep_switch_to"] = n
    if s["on_pc_sleep"] == "switch" and not s["pc_sleep_switch_to"]:
        problems.append("on_pc_sleep = \"switch\" needs pc_sleep_switch_to (e.g. \"11:00\")")

    for key in ("away_debounce", "suspend_retry_interval", "wol_retry_interval", "wake_correlation",
                "guard_window", "guard_delay", "auto_switch_cooldown", "power_poll_interval",
                "pc_sleep_confirm", "tv_boot_timeout", "tv_boot_reclaim_interval",
                "ping_interval", "tick_interval"):
        if not isinstance(s[key], (int, float)) or isinstance(s[key], bool) or s[key] < 0:
            problems.append(f"{key} must be a number >= 0")
    for key in ("max_suspend_attempts", "max_wol_attempts", "max_guard_recoveries", "wol_port",
                "tv_boot_reclaims"):
        if not isinstance(s[key], int) or isinstance(s[key], bool) or s[key] < 0:
            problems.append(f"{key} must be a whole number >= 0")
    if not isinstance(s["power_query_delays"], list) or \
            not all(isinstance(x, (int, float)) and x >= 0 for x in s["power_query_delays"]):
        problems.append("power_query_delays must be a list of numbers, e.g. [2, 8]")
    if not isinstance(s["dashboard_port"], int) or not 0 <= s["dashboard_port"] <= 65535:
        problems.append("dashboard_port must be 0-65535 (0 = off)")
    if not isinstance(s["cec_client_command"], list) or not s["cec_client_command"]:
        problems.append("cec_client_command must be a list, e.g. [\"cec-client\", \"-d\", \"8\"]")
    if s["ping_interval"] == 0 or s["tick_interval"] == 0:
        problems.append("ping_interval and tick_interval must be greater than 0")
    return SimpleNamespace(**s), problems


# --------------------------------------------------------------------------
# CEC message parsing
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# controller: owns all state, runs on one thread
# --------------------------------------------------------------------------

class Controller:
    def __init__(self, cfg, actions, clock=time.monotonic):
        self.cfg = cfg
        self.actions = actions
        self.now = clock
        self.pc_set = set(cfg.pc_addresses)
        self.pc_primary = cfg.pc_addresses[0]
        self.la = cfg.pi_logical_address
        self.suspects = ({cfg.pi_address, cfg.soundbar_address, cfg.soundbar_pc_audio_address}
                         | set(cfg.guard_addresses)) - {None} - self.pc_set
        t = self.now()
        self.tv_power = None            # "on" | "standby" | None (unknown)
        self.tv_input = None            # address, "standby", or None (unknown)
        self.input_since = t
        self.pc_online = None
        self.pc_online_since = None
        self.last_wol = None
        self.wol_attempts = 0
        self.wake_gave_up = False
        self.last_suspend = None
        self.suspend_attempts = 0
        self.sleep_gave_up = False
        self.suspend_inflight = False
        self.guard_until = 0
        self.guard_count = 0
        self.last_auto_switch = None
        self.tv_boot_started = None     # we turned the TV on and are waiting for it to report on
        self.next_boot_poll = 0
        self.tv_settle_until = 0        # the TV reported on, re-claims run until then
        self.pullback_pending = False
        self.pc_name = cfg.pc_name       # from the config, or looked up from the PC
        self.name_lookup_inflight = False
        self.name_lookup_tries = 0
        self.pc_seen_up = False         # PC answered ping during this stay on its input
        self.pc_offline_since = None    # PC dropped off while on screen, waiting to confirm sleep
        self.pc_down_since = None       # PC stopped answering ping
        self.first_ping_at = None       # first ping result, so "since" times before it are unknown
        self.power_note = None          # (what, time): the PC announced sleep, reboot or poweroff
        self.power_link = None          # (state, time): the power watcher's SSH session, up | closed | lost
        self.timers = []                # (due, description, function)
        self.next_power_poll = t + 3    # first query shortly after start
        self.startup_check_at = t + 6
        self.startup_pending = True
        self.started_at = t

    # ---- naming --------------------------------------------------------
    def input_name(self, addr):
        if addr is None:
            return "unknown"
        if addr == "standby":
            return "TV standby"
        if addr == "00:00":
            return "TV / built-in apps"
        digits = addr.replace(":", "").rstrip("0")
        label = "HDMI " + ".".join(str(int(c, 16)) for c in digits)
        if addr in self.pc_set:
            label += " (PC)"
        elif addr == self.cfg.pi_address:
            label += " (Raspberry Pi)"
        elif addr == self.cfg.soundbar_pc_audio_address:
            label += " (soundbar, PC audio)"
        elif addr == self.cfg.soundbar_address:
            label += " (soundbar)"
        return label

    # ---- helpers -------------------------------------------------------
    def desired(self):
        if self.tv_power == "standby" or self.tv_input == "standby":
            return "asleep"
        if self.tv_input is None:
            return None
        return "awake" if self.tv_input in self.pc_set else "asleep"

    def after(self, delay, description, fn):
        self.timers.append((self.now() + delay, description, fn))

    def cec(self, command, why):
        log("CEC-TX", f"{command}  ({why})")
        self.actions.cec_send(command)

    def assert_pc_input(self, why):
        self.cec(f"tx {self.la:x}F:82:{self.pc_primary}", why)

    def pull_back(self, why):
        """Claim the PC's input after guard_delay. Repeated calls before it fires add nothing."""
        if self.pullback_pending:
            return
        self.pullback_pending = True

        def fire():
            self.pullback_pending = False
            if self.tv_input in self.pc_set:
                self.assert_pc_input(why)
        self.after(self.cfg.guard_delay, "pull back", fire)

    def request_soundbar_audio(self):
        if not (self.cfg.select_soundbar_audio and self.cfg.soundbar_pc_audio_address):
            return
        self.cec(f"tx {self.la:x}5:70:{self.cfg.soundbar_pc_audio_address}",
                 "asking the soundbar to play the PC's audio input")

    def set_input(self, addr, source, detail=""):
        """Record the TV's input. source: tv | self | startup | standby | power."""
        prev = self.tv_input
        if addr == prev:
            log("INPUT", f"TV input unchanged: {self.input_name(addr)}{detail}")
            return False
        self.tv_input = addr
        self.input_since = self.now()
        self.suspend_attempts = 0
        self.sleep_gave_up = False
        self.wol_attempts = 0
        self.wake_gave_up = False
        self.pc_seen_up = bool(self.pc_online) and addr in self.pc_set
        self.pc_offline_since = None
        if addr not in ("standby", None):
            self.tv_power = "on"
        log("INPUT", f"TV input: {self.input_name(prev)} -> {self.input_name(addr)}{detail}")
        if addr in self.pc_set:
            self.on_pc_input_selected(source)
        return True

    def on_pc_input_selected(self, source):
        t = self.now()
        self.guard_until = t + self.cfg.guard_window
        self.guard_count = 0
        if source == "self":
            self.after(1.0, "switch TV to PC", lambda: self.assert_pc_input("switching the TV to the PC"))
        elif source == "startup":
            self.assert_pc_input("PC already on at startup")
        elif source == "tv" and self.cfg.claim_pc_input:
            self.assert_pc_input("answering for the PC's input")
        if self.pc_online:
            self.after(1.0, "soundbar audio", self.request_soundbar_audio)

    # ---- events --------------------------------------------------------
    def handle(self, event):
        kind = event[0]
        if kind == "cec":
            self.on_cec(event[1])
        elif kind == "key":
            self.on_key(event[1])
        elif kind == "ping":
            self.on_ping(event[1])
        elif kind == "suspend_done":
            self.on_suspend_done(event[1], event[2])
        elif kind == "pc_name":
            self.on_pc_name(event[1])
        elif kind == "cec_out":
            self.on_cec_out(event[1])
        elif kind == "pc_power":
            self.on_pc_power(event[1])

    def lookup_pc_name(self):
        """Ask the PC for its name once it is reachable, unless the config sets one."""
        if self.pc_name or self.name_lookup_inflight or self.name_lookup_tries >= 3:
            return
        self.name_lookup_inflight = True
        self.name_lookup_tries += 1
        self.actions.fetch_pc_name()

    def on_pc_name(self, name):
        self.name_lookup_inflight = False
        if name and not self.pc_name:
            self.pc_name = name
            log("WATCHER", f"PC name: {name}")

    def on_cec(self, msg):
        op, p = msg.opcode, msg.params
        if op in ("82", "86") and msg.broadcast and len(p) >= 2:
            self.on_announcement(msg.initiator, op, f"{p[0]}:{p[1]}")
        elif op == "81" and msg.broadcast and len(p) >= 2 and self.cfg.watch_routing_messages:
            self.on_announcement(msg.initiator, op, f"{p[0]}:{p[1]}")
        elif op == "80" and msg.broadcast and len(p) >= 4 and self.cfg.watch_routing_messages:
            self.on_announcement(msg.initiator, op, f"{p[2]}:{p[3]}")
        elif op == "36" and (msg.broadcast or msg.destination == 0):
            self.on_standby(msg.initiator, msg.destination)
        elif op == "90" and msg.initiator == 0 and p:
            self.on_power_status(p[0])
        elif op == "85" and msg.broadcast and self.cfg.claim_pc_input and self.tv_input in self.pc_set:
            # Someone asked who the active source is. Answer for the PC.
            self.after(0.3, "answer active source", lambda: self.assert_pc_input(
                "answering Request Active Source for the PC"))

    def on_cec_out(self, msg):
        """A message the Pi sent. cec-client answers some requests by itself, for
        example with Active Source for the Pi's own input, which pulls the TV there."""
        p = msg.params
        if not (msg.opcode == "82" and msg.broadcast and len(p) >= 2
                and f"{p[0]}:{p[1]}" == self.cfg.pi_address):
            return
        if self.tv_boot_started is None and self.tv_input in self.pc_set and self.pc_online:
            log("RECOVER", f"cec-client made the Pi the active source ({self.input_name(self.cfg.pi_address)}) "
                           "while the PC is on. Switching the TV back to the PC")
            self.pull_back("undoing a switch to the Pi's own input")

    def on_announcement(self, initiator, opcode, addr):
        sender = device_name(initiator)
        detail = f" ({ANNOUNCE_OPCODES[opcode]} from {sender}, {addr})"
        if initiator in self.cfg.ignore_announcements_from:
            log("INPUT", f"Ignored {self.input_name(addr)}{detail}: sender is in ignore_announcements_from")
            return
        t = self.now()
        if self.tv_input in self.pc_set and addr not in self.pc_set:
            if self.tv_boot_started is not None:
                log("RECOVER", f"Ignoring {self.input_name(addr)}{detail}: the TV is still starting. "
                               "It is switched to the PC once it reports that it is on")
                return
            if t < self.tv_settle_until:
                log("RECOVER", f"TV switched to {self.input_name(addr)} while it was still starting{detail}. "
                               "Switching it back to the PC")
                self.pull_back("undoing a switch made while the TV was starting")
                return
            if addr == self.cfg.pi_address and self.pc_online:
                log("RECOVER", f"TV switched to {self.input_name(addr)} while the PC is on{detail}. "
                               "Switching it back to the PC")
                self.pull_back("undoing a switch to the Pi's own input")
                return
        if (self.tv_input in self.pc_set and addr in self.suspects and t < self.guard_until
                and self.guard_count < self.cfg.max_guard_recoveries):
            self.guard_count += 1
            log("RECOVER", f"TV switched to {self.input_name(addr)} right after switching to the PC{detail}. "
                           f"Treating it as automatic and switching back "
                           f"({self.guard_count}/{self.cfg.max_guard_recoveries}). The PC stays awake")
            self.after(self.cfg.guard_delay, "guard",
                       lambda: self.assert_pc_input("undoing an automatic switch after wake"))
            return
        self.set_input(addr, "tv", detail)

    def on_standby(self, initiator, destination):
        if initiator == 0:
            log("TV", "Standby broadcast from the TV")
            self.tv_boot_started = None
            self.tv_settle_until = 0
            self.tv_power = "standby"
            self.set_input("standby", "standby")
            return
        who = device_name(initiator)
        to = "everyone" if destination == 15 else "the TV"
        if self.cfg.verify_non_tv_standby:
            log("TV", f"Standby from {who} to {to}, asking the TV whether it is really off")
            for d in self.cfg.power_query_delays:
                self.after(d, "power query", lambda: self.cec("pow 0", "TV power check"))
        else:
            log("TV", f"Standby from {who} to {to}, treating it as TV standby")
            self.tv_power = "standby"
            self.set_input("standby", "standby")

    def on_power_status(self, code):
        status = POWER_STATUS.get(code, f"unknown ({code})")
        if self.tv_boot_started is not None:
            if code == "00":
                log("TV", "TV reports that it is on")
                self.tv_power = "on"
                self.tv_ready()
            else:
                log("TV", f"TV power status: {status}, still waiting for it to start", level="DEBUG")
            return
        changed = (self.tv_power != ("standby" if code in ("01", "03") else "on"))
        log("TV", f"TV power status: {status}", level="INFO" if changed else "DEBUG")
        if code in ("01", "03"):
            self.tv_power = "standby"
            self.set_input("standby", "power")
        elif code in ("00", "02"):
            self.tv_power = "on"
            if self.tv_input == "standby":
                # The TV came on without announcing an input. Don't guess.
                log("TV", "TV is on but hasn't announced an input yet, waiting for one")
                self.tv_input = None
                self.input_since = self.now()

    def on_key(self, name):
        if not self.cfg.hid_passthrough:
            return
        if self.tv_input not in self.pc_set:
            log("KEY", f"CEC key '{name}' ignored: the TV isn't on the PC's input", level="DEBUG")
            return
        code = KEYCODES.get(name)
        if code is None:
            log("KEY", f"Unmapped CEC key '{name}'", level="WARN")
            return
        self.actions.key(code, name)

    def on_pc_power(self, what):
        """What the power watcher heard from the PC: sleep, resume, reboot, poweroff,
        shutdown, or link_up / link_closed / link_lost for its own SSH session."""
        t = self.now()
        if what.startswith("link_"):
            self.power_link = (what[5:], t)
            log("WATCHER", f"Power watcher session {what[5:]}", level="DEBUG")
            return
        if what == "resume":
            self.power_note = None
            log("WATCHER", "PC says it woke up")
            return
        if what not in PC_ANNOUNCED:
            return
        note = self.power_note
        if what == "shutdown" and note and note[0] in ("reboot", "poweroff") and t - note[1] < 10:
            return      # the plain signal that follows the one saying which kind of shutdown
        self.power_note = (what, t)
        log("WATCHER", {"sleep": "PC says it is going to sleep", "reboot": "PC says it is restarting",
                        "poweroff": "PC says it is shutting down",
                        "shutdown": "PC says it is shutting down or restarting"}[what])

    def power_state(self):
        """on | asleep | off | restarting | unknown, or None before the first ping."""
        if self.pc_online is None:
            return None
        if self.pc_online:
            return "on"
        t = self.now()
        since = self.pc_down_since if self.pc_down_since is not None else self.started_at

        def recent(event):
            return event is not None and event[1] >= since - 120

        if recent(self.power_note):
            state = PC_ANNOUNCED[self.power_note[0]]
            if state == "restarting" and t - since > 300:
                return "off"        # it never came back
            return state
        if recent(self.power_link) and self.power_link[0] == "closed":
            return "off"            # shutting down stops the SSH server, sleep doesn't
        if self.last_suspend is not None and self.last_suspend >= since - 120:
            return "asleep"
        if self.power_link and (self.power_link[0] == "up" or
                                (self.power_link[0] == "lost" and recent(self.power_link))):
            return "asleep"         # the session froze rather than closed
        return "unknown"

    def on_ping(self, online):
        prev = self.pc_online
        self.pc_online = online
        t = self.now()
        if online:
            self.lookup_pc_name()
        if prev is None:
            self.first_ping_at = t
            if online:
                self.pc_online_since = t
            else:
                self.pc_down_since = t
            log("WATCHER", f"PC is {'reachable' if online else 'not reachable'}")
            return
        if online == prev:
            return
        if not online:
            self.pc_down_since = t
            state = self.power_state()
            log("WATCHER", "PC is now unreachable (" + {"asleep": "asleep", "off": "turned off",
                                                       "restarting": "restarting"}.get(state, "asleep or off") + ")")
            if self.suspend_attempts:
                log("SUSPEND", "Confirmed: the PC went to sleep")
            elif self.desired() == "awake" and self.pc_seen_up:
                self.pc_offline_since = t
                log("WATCHER", f"PC went offline while on screen. If it stays offline for "
                               f"{self.cfg.pc_sleep_confirm}s, CouchLink takes it as put to sleep or "
                               f"turned off on purpose and won't wake it again")
            return
        self.pc_online_since = t
        was = self.power_note[0] if self.power_note else None
        self.power_note = None
        if was in ("reboot", "shutdown") and self.pc_down_since is not None and t - self.pc_down_since < 600:
            log("WATCHER", "PC is back after restarting")
        if self.pc_offline_since is not None:
            if was not in ("reboot", "shutdown"):
                log("WATCHER", "PC is back (it was only a network blip)")
            self.pc_offline_since = None
        ours = self.last_wol is not None and t - self.last_wol <= self.cfg.wake_correlation
        d = self.desired()
        if d == "awake":
            log("WATCHER", "PC is now reachable" + (" (woken by CouchLink)" if ours else ""))
            # Its HDMI outputs just came alive. This is when a soundbar or TV may switch by itself.
            self.guard_until = max(self.guard_until, t + self.cfg.guard_window)
            self.after(2.0, "soundbar audio", self.request_soundbar_audio)
        elif ours:
            log("WATCHER", "PC woke up from CouchLink's Wake-on-LAN, but the TV has left its input. "
                           "Putting it back to sleep")
        else:
            log("WATCHER", "PC woke up by itself")
            self.auto_switch_to_pc()

    def handle_pc_slept(self):
        """The PC went to sleep by itself (Steam menu, power button) while on screen."""
        self.pc_offline_since = None
        went = "was turned off" if self.power_state() == "off" else "went to sleep"
        mode = self.cfg.on_pc_sleep
        if mode == "switch":
            target = self.cfg.pc_sleep_switch_to
            self.cec(f"tx {self.la:x}F:82:{target}",
                     f"PC {went}: switching to {self.input_name(target)}")
            self.set_input(target, "self", f" (PC {went})")
        elif mode == "standby":
            self.cec("standby 0", f"PC {went}: putting the TV in standby")
            self.tv_power = "standby"
            self.set_input("standby", "standby", f" (PC {went})")
        else:
            log("WATCHER", f"PC {went} while on screen. Leaving the TV as it is "
                           "(on_pc_sleep = \"nothing\")")

    def auto_switch_to_pc(self):
        mode = self.cfg.auto_switch_on_pc_wake
        t = self.now()
        tv_off = self.tv_power == "standby" or self.tv_input == "standby"
        if mode == "never" or (mode == "if_tv_on" and tv_off):
            log("WATCHER", f"Not switching the TV (auto_switch_on_pc_wake = \"{mode}\"). "
                           "The PC goes back to sleep if the TV stays on another input")
            return
        if self.last_auto_switch is not None and t - self.last_auto_switch < self.cfg.auto_switch_cooldown:
            log("WATCHER", f"Not switching the TV: it was switched to the PC less than "
                           f"{self.cfg.auto_switch_cooldown}s ago (auto_switch_cooldown)")
            return
        self.last_auto_switch = t
        self.cec("on 0", "PC woke by itself: turning the TV on")
        if self.tv_power == "on" and self.tv_input != "standby":
            self.set_input(self.pc_primary, "self", " (PC woke by itself)")
            return
        # The TV is off or its state is unknown. A starting TV may switch to the Pi
        # (Image View On came from the Pi's input) or route to another device, so
        # the PC's input is only claimed once the TV reports that it is on.
        self.tv_boot_started = t
        self.next_boot_poll = t
        self.tv_settle_until = 0
        log("TV", f"Waiting up to {self.cfg.tv_boot_timeout}s for the TV to report that it is on "
                  "before switching it to the PC")
        self.set_input(self.pc_primary, "boot", " (PC woke by itself, TV starting)")

    def tv_ready(self):
        """The TV finished starting (or never answered). Claim the PC's input, then again a few times."""
        self.tv_boot_started = None
        if self.tv_input not in self.pc_set:
            return
        n, gap = self.cfg.tv_boot_reclaims, self.cfg.tv_boot_reclaim_interval
        t = self.now()
        self.tv_settle_until = t + n * gap
        self.guard_until = max(self.guard_until, self.tv_settle_until + self.cfg.guard_window)
        self.guard_count = 0
        self.assert_pc_input("the TV is on: switching it to the PC")

        def reclaim(i):
            if self.tv_input in self.pc_set and self.tv_boot_started is None:
                self.assert_pc_input(f"the TV may still be starting: claiming the PC's input again ({i}/{n})")
        for i in range(1, n + 1):
            self.after(i * gap, "re-claim", lambda i=i: reclaim(i))

    def on_suspend_done(self, returncode, detail):
        self.suspend_inflight = False
        if returncode == 0:
            log("SUSPEND", "Suspend command accepted, waiting for the PC to go offline")
        elif returncode == 255:
            log("SUSPEND", f"SSH ended with 255 ({detail or 'connection closed'}). That can be "
                           "normal if the PC suspended mid-command. Checking with ping", level="WARN")
        elif returncode is None:
            log("SUSPEND", f"Suspend command timed out: {detail}", level="WARN")
        else:
            log("SUSPEND", f"Suspend command failed ({returncode}): {detail}", level="ERROR")

    # ---- periodic --------------------------------------------------------
    def tick(self):
        t = self.now()
        due = [x for x in self.timers if x[0] <= t]
        self.timers = [x for x in self.timers if x[0] > t]
        for _, _, fn in sorted(due, key=lambda x: x[0]):
            fn()

        if self.tv_boot_started is not None:
            if t - self.tv_boot_started >= self.cfg.tv_boot_timeout:
                log("TV", f"The TV did not report that it is on within {self.cfg.tv_boot_timeout}s. "
                          "Switching it to the PC anyway", level="WARN")
                self.tv_ready()
            elif t >= self.next_boot_poll:
                self.next_boot_poll = t + TV_BOOT_POLL
                self.actions.cec_send("pow 0")

        if self.cfg.power_poll_interval and t >= self.next_power_poll:
            self.next_power_poll = t + self.cfg.power_poll_interval
            self.actions.cec_send("pow 0")
        elif not self.cfg.power_poll_interval and self.next_power_poll and t >= self.next_power_poll:
            self.next_power_poll = None     # still do the one startup query
            self.actions.cec_send("pow 0")

        if self.startup_pending and t >= self.startup_check_at:
            if self.pc_online is not None:
                self.startup_pending = False
                if self.pc_online and self.tv_input is None and self.tv_power != "standby":
                    log("STARTUP", "PC is already on and the TV's input is unknown: "
                                   "assuming the PC is in use")
                    self.set_input(self.pc_primary, "startup", " (startup)")
            elif t >= self.startup_check_at + 15:
                self.startup_pending = False

        d = self.desired()
        if d == "awake" and self.pc_online is True:
            self.pc_seen_up = True
        if (self.pc_offline_since is not None and self.pc_online is False and d == "awake"
                and t - self.pc_offline_since >= self.cfg.pc_sleep_confirm
                and self.power_state() != "restarting"):
            self.handle_pc_slept()
            d = self.desired()
        if d == "awake":
            self.ensure_awake(t)
        elif d == "asleep":
            self.ensure_asleep(t)

    def ensure_awake(self, t):
        if self.pc_online is True or self.wake_gave_up:
            return
        if self.pc_seen_up:
            return      # it was up on this input and went down by itself: don't fight the user
        if self.wol_attempts and t - self.last_wol < self.cfg.wol_retry_interval:
            return
        if self.wol_attempts >= self.cfg.max_wol_attempts:
            log("WOL", f"Giving up after {self.wol_attempts} Wake-on-LAN packets. The PC never "
                       "answered ping", level="ERROR")
            self.wake_gave_up = True
            return
        self.wol_attempts += 1
        self.last_wol = t
        log("WOL", f"Sending Wake-on-LAN to {self.cfg.pc_mac} "
                   f"(attempt {self.wol_attempts}/{self.cfg.max_wol_attempts})")
        self.actions.wol()

    def ensure_asleep(self, t):
        if self.pc_online is not True or self.suspend_inflight or self.sleep_gave_up:
            return
        away_for = t - self.input_since
        if away_for < self.cfg.away_debounce:
            return
        if self.last_suspend is not None and t - self.last_suspend < self.cfg.suspend_retry_interval:
            return
        if self.suspend_attempts >= self.cfg.max_suspend_attempts:
            log("SUSPEND", f"Giving up after {self.suspend_attempts} suspend attempts. The PC still "
                           f"answers while the TV is on {self.input_name(self.tv_input)}", level="ERROR")
            self.sleep_gave_up = True
            return
        self.suspend_attempts += 1
        self.last_suspend = t
        self.suspend_inflight = True
        log("SUSPEND", f"Suspending the PC, attempt {self.suspend_attempts}/"
                       f"{self.cfg.max_suspend_attempts} (TV on {self.input_name(self.tv_input)} "
                       f"for {int(away_for)}s)")
        self.actions.suspend()

    # ---- status ---------------------------------------------------------
    def pc_state_started(self):
        """When the PC last came up or went down, as far as CouchLink has seen."""
        if self.pc_online is None:
            return None
        return self.pc_online_since if self.pc_online else self.pc_down_since

    def pc_state_seconds(self, t):
        since = self.pc_state_started()
        return int(t - since) if since is not None else None

    def snapshot(self):
        t = self.now()
        return {
            "version": VERSION,
            "dry_run": getattr(self.actions, "dry_run", False),
            "tv_power": self.tv_power,
            "tv_input": self.input_name(self.tv_input),
            "tv_input_for_seconds": int(t - self.input_since),
            "pc_should_be": self.desired() or "unknown",
            "pc_reachable": self.pc_online,
            "pc_power": self.power_state() or "unknown",
            "pc_state_seconds": self.pc_state_seconds(t),
            "pc_state_since_start": (self.first_ping_at is not None
                                     and self.pc_state_started() == self.first_ping_at),
            "wol_attempts": self.wol_attempts,
            "suspend_attempts": self.suspend_attempts,
            "guard_active": t < self.guard_until and self.tv_input in self.pc_set,
            "tv_starting": self.tv_boot_started is not None,
            "pc_sleep_check_seconds_left": (max(0, int(self.cfg.pc_sleep_confirm - (t - self.pc_offline_since)))
                                            if self.pc_offline_since is not None else None),
            "tv_input_address": self.tv_input,
            "pc_host": self.cfg.pc_host,
            "pc_name": self.pc_name,
            "pc_inputs": [self.input_name(a) for a in self.cfg.pc_addresses],
            "on_pc_sleep": self.cfg.on_pc_sleep,
            "on_pc_sleep_target": (self.input_name(self.cfg.pc_sleep_switch_to)
                                   if self.cfg.pc_sleep_switch_to else None),
            "power_poll_interval": self.cfg.power_poll_interval,
            "last_wol_seconds_ago": int(t - self.last_wol) if self.last_wol is not None else None,
            "last_suspend_seconds_ago": int(t - self.last_suspend) if self.last_suspend is not None else None,
            "uptime_seconds": int(t - self.started_at),
            "recent_log": list(LOG_RING)[-40:],
            "recent_traffic": list(TRAFFIC_RING)[-40:],
        }


# --------------------------------------------------------------------------
# side effects
# --------------------------------------------------------------------------

def send_magic_packet(mac, broadcast, port):
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))


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


class RealActions:
    dry_run = False

    def __init__(self, cfg, cec, events):
        self.cfg = cfg
        self.cec = cec
        self.events = events

    def cec_send(self, command):
        try:
            self.cec.send(command)
        except (OSError, ValueError) as e:
            log("CEC-TX", f"Could not send '{command}': {e}", level="ERROR")

    def wol(self):
        try:
            send_magic_packet(self.cfg.pc_mac, self.cfg.wol_broadcast, self.cfg.wol_port)
        except OSError as e:
            log("WOL", f"Could not send magic packet: {e}", level="ERROR")

    def suspend(self):
        def run():
            try:
                r = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                     f"{self.cfg.ssh_user}@{self.cfg.pc_host}", self.cfg.suspend_command],
                    capture_output=True, text=True, timeout=20)
                self.events.put(("suspend_done", r.returncode, r.stderr.strip()))
            except subprocess.TimeoutExpired:
                self.events.put(("suspend_done", None, "no answer within 20s"))
            except OSError as e:
                self.events.put(("suspend_done", -1, str(e)))
        threading.Thread(target=run, daemon=True).start()

    def fetch_pc_name(self):
        """Find the PC's name: ask it over SSH, then fall back to reverse DNS.
        Posts ("pc_name", name or None). Read-only, so dry runs do it too."""
        def clean(name):
            name = (name or "").strip().split(".")[0]
            return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name) else None

        def run():
            name = None
            try:
                r = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                     f"{self.cfg.ssh_user}@{self.cfg.pc_host}", "hostname"],
                    capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    name = clean(r.stdout.splitlines()[0] if r.stdout else "")
            except (subprocess.TimeoutExpired, OSError):
                pass
            if not name:
                try:
                    name = clean(socket.gethostbyaddr(self.cfg.pc_host)[0])
                except OSError:
                    pass
            self.events.put(("pc_name", name))
        threading.Thread(target=run, daemon=True).start()

    def key(self, code, name):
        report = bytearray(8)
        report[2] = code
        try:
            fd = os.open(self.cfg.hidg_device, os.O_WRONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            log("KEY", "HID gadget not available, keypress skipped", level="WARN")
            return
        except OSError as e:
            log("KEY", f"Could not open {self.cfg.hidg_device}: {e}", level="WARN")
            return
        try:
            os.write(fd, bytes(report))
            time.sleep(0.05)
            for _ in range(10):
                try:
                    os.write(fd, bytes(8))
                    break
                except BlockingIOError:
                    time.sleep(0.01)
            log("KEY", f"CEC key '{name}' -> HID 0x{code:02x}")
        except BlockingIOError:
            log("KEY", "PC isn't reading keyboard input (asleep or USB unplugged), keypress dropped",
                level="WARN")
        except OSError as e:
            log("KEY", f"Keypress failed: {e}", level="WARN")
        finally:
            os.close(fd)


class DryRunActions(RealActions):
    """Reads the bus and asks the TV for its power status, but changes nothing."""
    dry_run = True

    def cec_send(self, command):
        if command == "pow 0":
            super().cec_send(command)       # read-only query
        else:
            log("DRY-RUN", f"Would send CEC: {command}")

    def wol(self):
        log("DRY-RUN", "Would send Wake-on-LAN")

    def suspend(self):
        log("DRY-RUN", f"Would run: ssh {self.cfg.ssh_user}@{self.cfg.pc_host} {self.cfg.suspend_command}")
        self.events.put(("suspend_done", 0, "dry run"))

    def key(self, code, name):
        log("DRY-RUN", f"Would send key '{name}' -> HID 0x{code:02x}")


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


# The status page. One self-contained file (no external fonts or scripts, so it
# works without internet access). It polls /api every 2 seconds.
DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#070a12">
<title>CouchLink</title>
<style>
:root {
  --bg: #070a12;
  --panel: rgba(16, 22, 36, 0.78);
  --panel-edge: rgba(148, 163, 184, 0.14);
  --text: #e6edf6;
  --muted: #8794aa;
  --dim: #5b6679;
  --cyan: #22d3ee;
  --violet: #a78bfa;
  --pink: #f472b6;
  --green: #34d399;
  --amber: #fbbf24;
  --red: #f87171;
  --accent: var(--cyan);
  --accent-soft: rgba(34, 211, 238, 0.35);
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, "Helvetica Neue", sans-serif;
}
body[data-state="online"]   { --accent: var(--green);  --accent-soft: rgba(52, 211, 153, 0.35); }
body[data-state="waking"]   { --accent: var(--cyan);   --accent-soft: rgba(34, 211, 238, 0.35); }
body[data-state="sleeping"] { --accent: var(--amber);  --accent-soft: rgba(251, 191, 36, 0.35); }
body[data-state="asleep"]   { --accent: var(--violet); --accent-soft: rgba(167, 139, 250, 0.30); }
body[data-state="off"]      { --accent: #64748b;       --accent-soft: rgba(100, 116, 139, 0.30); }
body[data-state="unknown"]  { --accent: #94a3b8;       --accent-soft: rgba(148, 163, 184, 0.25); }

* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  min-height: 100vh;
  background-color: var(--bg);
  background-image:
    radial-gradient(1100px 520px at 0% -10%, rgba(34, 211, 238, 0.10), transparent 60%),
    radial-gradient(900px 480px at 100% 0%, rgba(167, 139, 250, 0.12), transparent 60%),
    linear-gradient(rgba(148, 163, 184, 0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(148, 163, 184, 0.045) 1px, transparent 1px);
  background-size: auto, auto, 34px 34px, 34px 34px;
  background-attachment: fixed;
  color: var(--text);
  font-family: var(--sans);
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 22px 16px 40px; }

/* header */
header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
.brand { display: flex; align-items: center; gap: 12px; }
.brand svg { width: 40px; height: 40px; filter: drop-shadow(0 0 10px rgba(34, 211, 238, 0.45)); }
.word {
  font-weight: 800; font-size: 21px; letter-spacing: 0.24em; text-transform: uppercase;
  background: linear-gradient(90deg, var(--cyan), var(--violet) 55%, var(--pink));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.chip {
  font: 600 11px/1 var(--mono); letter-spacing: 0.1em; text-transform: uppercase;
  padding: 6px 10px; border-radius: 999px; color: var(--muted);
  border: 1px solid var(--panel-edge); background: rgba(10, 14, 24, 0.7);
}
.chip.dry { color: var(--amber); border-color: rgba(251, 191, 36, 0.5); box-shadow: 0 0 14px rgba(251, 191, 36, 0.18); }
.chip[hidden] { display: none; }
.spacer { flex: 1; }
.live { display: flex; align-items: center; gap: 8px; font: 600 11px var(--mono); letter-spacing: 0.14em; color: var(--muted); }
.live .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); animation: blink 2s ease-in-out infinite; }
.live.off .dot { background: var(--red); box-shadow: 0 0 10px var(--red); animation: none; }
.live.off { color: var(--red); }

/* panels */
.panel {
  position: relative; border-radius: 16px; background: var(--panel);
  border: 1px solid var(--panel-edge);
  -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
}

/* hero */
.hero { display: grid; grid-template-columns: auto 1fr; gap: 22px; align-items: center; padding: 24px; overflow: hidden; }
.hero::after {
  content: ""; position: absolute; inset: -1px; border-radius: inherit; pointer-events: none;
  background: linear-gradient(115deg, var(--accent-soft), transparent 35%, transparent 70%, rgba(167, 139, 250, 0.25));
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor; mask-composite: exclude; padding: 1px;
}
.orb {
  position: relative; width: 104px; height: 104px; border-radius: 50%;
  display: grid; place-items: center; color: var(--accent);
  border: 2px solid var(--accent);
  box-shadow: 0 0 34px var(--accent-soft), inset 0 0 26px var(--accent-soft);
  transition: color 0.4s, border-color 0.4s, box-shadow 0.4s;
}
.orb svg { width: 52px; height: 52px; }
body[data-state="waking"] .orb::before,
body[data-state="sleeping"] .orb::before {
  content: ""; position: absolute; inset: -8px; border-radius: 50%;
  border: 2px solid transparent; border-top-color: var(--accent); border-right-color: var(--accent);
  animation: spin 1.1s linear infinite;
}
.kicker { font: 600 13px var(--mono); letter-spacing: 0.2em; color: var(--text); opacity: 0.85; text-transform: uppercase; }
.state {
  margin: 6px 0 6px; font-size: clamp(30px, 6vw, 46px); font-weight: 800; letter-spacing: 0.06em;
  color: var(--accent); text-shadow: 0 0 22px var(--accent-soft); text-transform: uppercase; line-height: 1.05;
}
.sub { color: var(--text); font-size: 15px; }
.sub .muted { color: var(--muted); }

/* signal path */
.path { display: flex; align-items: center; gap: 0; margin-top: 16px; }
.node {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 10px;
  border: 1px solid var(--panel-edge); background: rgba(8, 12, 20, 0.6);
  font: 600 12px var(--mono); letter-spacing: 0.06em; color: var(--dim); white-space: nowrap;
  transition: color 0.3s, border-color 0.3s, box-shadow 0.3s;
}
.node .led { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.node.on { color: var(--text); border-color: var(--accent); box-shadow: 0 0 16px var(--accent-soft); }
.node.on .led { background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.link { flex: 1; min-width: 18px; height: 2px; background: rgba(148, 163, 184, 0.18); position: relative; overflow: hidden; }
.link.on { background: rgba(148, 163, 184, 0.25); }
.link.on::after {
  content: ""; position: absolute; top: 0; left: -40%; width: 40%; height: 100%;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: flow 1.6s linear infinite;
}

/* tiles */
.grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
@media (max-width: 900px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.tile { padding: 14px 16px 15px; overflow: hidden; }
.tile .label { display: flex; align-items: center; gap: 8px; font: 600 11px var(--mono); letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted); }
.tile .label svg { width: 15px; height: 15px; color: var(--violet); flex: none; }
.tile .value { margin-top: 10px; font-size: 21px; font-weight: 700; letter-spacing: 0.02em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tile .hint { margin-top: 4px; font: 12px var(--mono); color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.good { color: var(--green); }
.warn { color: var(--amber); }
.bad  { color: var(--red); }
.cool { color: var(--cyan); }
.calm { color: var(--violet); }
.dim  { color: var(--dim); }
.tile::before {
  content: ""; position: absolute; left: 0; top: 14px; bottom: 14px; width: 3px; border-radius: 0 3px 3px 0;
  background: var(--tile-accent, transparent); box-shadow: 0 0 10px var(--tile-accent, transparent);
}

/* console */
.console { padding: 0; overflow: hidden; }
.tabs { display: flex; align-items: center; gap: 4px; padding: 10px 12px 0; border-bottom: 1px solid var(--panel-edge); }
.tab {
  appearance: none; border: 0; background: none; cursor: pointer;
  font: 600 12px var(--mono); letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted);
  padding: 9px 12px 11px; border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--cyan); }
.tab:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; border-radius: 6px; }
.count { color: var(--dim); margin-left: 6px; }
.lines {
  height: 340px; overflow: auto; padding: 10px 14px 14px; font: 12.5px/1.65 var(--mono);
  scrollbar-width: thin; scrollbar-color: rgba(148, 163, 184, 0.3) transparent;
}
.line { display: flex; gap: 10px; white-space: pre-wrap; word-break: break-word; }
.line .t { color: var(--dim); flex: none; }
.line .lv { flex: none; font-weight: 700; width: 46px; }
.line .c { flex: none; color: var(--violet); }
.line .m { color: var(--text); }
.lv.INFO { color: var(--cyan); }
.lv.WARN { color: var(--amber); }
.lv.ERROR { color: var(--red); }
.dir { flex: none; font-weight: 700; padding: 0 6px; border-radius: 4px; font-size: 11px; line-height: 20px; height: 20px; }
.dir.rx { color: var(--cyan); background: rgba(34, 211, 238, 0.12); }
.dir.tx { color: var(--pink); background: rgba(244, 114, 182, 0.12); }
.empty { color: var(--dim); padding: 6px 0; }

footer { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 14px; font: 12px var(--mono); color: var(--dim); }
footer a { color: var(--muted); }

@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes flow { to { left: 100%; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
@media (max-width: 560px) {
  .wrap { padding-top: 16px; }
  .hero { gap: 16px; padding: 18px 16px; align-items: start; }
  .orb { margin-top: 18px; }
  .orb { width: 64px; height: 64px; }
  .orb svg { width: 34px; height: 34px; }
  .state { font-size: 28px; }
  .sub { font-size: 14px; }
  .sub .sep { display: none; }
  .sub .muted { display: block; }
  .path { margin-left: -80px; }
  .node { padding: 7px 9px; font-size: 11px; }
  .grid { gap: 10px; }
  .tile { padding: 12px 12px 13px; }
  .tile .label { font-size: 10px; letter-spacing: 0.12em; }
  .tile .value { font-size: 17px; }
  .tile .hint { font-size: 11px; }
  .lines { height: 320px; font-size: 12px; padding: 8px 12px 12px; }
  .line { display: block; margin-bottom: 4px; }
  .line > span { margin-right: 8px; }
  .line .lv { display: inline-block; width: auto; }
}
</style>
</head>
<body data-state="unknown">
<div class="wrap">
  <header>
    <div class="brand">
      <svg viewBox="0 0 48 48" fill="none" aria-hidden="true">
        <defs><linearGradient id="g" x1="0" y1="0" x2="48" y2="48"><stop offset="0" stop-color="#22d3ee"/><stop offset=".6" stop-color="#a78bfa"/><stop offset="1" stop-color="#f472b6"/></linearGradient></defs>
        <rect x="3" y="9" width="42" height="27" rx="5" stroke="url(#g)" stroke-width="3"/>
        <path d="M17 42h14" stroke="url(#g)" stroke-width="3" stroke-linecap="round"/>
        <path d="M15 22.5h6M18 19.5v6" stroke="url(#g)" stroke-width="2.6" stroke-linecap="round"/>
        <circle cx="29" cy="21" r="1.9" fill="url(#g)"/><circle cx="33.5" cy="25" r="1.9" fill="url(#g)"/>
      </svg>
      <span class="word">CouchLink</span>
    </div>
    <span class="chip" id="version">v-</span>
    <span class="chip dry" id="dry" hidden>Dry run</span>
    <span class="spacer"></span>
    <span class="live" id="live"><span class="dot"></span><span id="liveText">Connecting</span></span>
  </header>

  <section class="panel hero" aria-live="polite">
    <div class="orb">
      <svg viewBox="0 0 64 64" fill="none" aria-hidden="true">
        <path d="M18 20h28c6.5 0 10.5 5 11.6 12.4l1.8 12.2c.6 4.3-2.3 7.4-6.2 7.4-2.6 0-4.4-1.4-5.8-3.5L44 43H20l-3.4 5.5C15.2 50.6 13.4 52 10.8 52c-3.9 0-6.8-3.1-6.2-7.4l1.8-12.2C7.5 25 11.5 20 18 20z" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/>
        <path d="M17 28v9M12.5 32.5h9" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
        <circle cx="44" cy="29" r="2.6" fill="currentColor"/><circle cx="49.5" cy="35" r="2.6" fill="currentColor"/>
      </svg>
    </div>
    <div>
      <div class="kicker" id="kicker">Gaming PC</div>
      <div class="state" id="state">Connecting</div>
      <div class="sub" id="sub"><span class="muted">Waiting for CouchLink</span></div>
      <div class="path" aria-label="Signal path">
        <span class="node" id="nTv"><span class="led"></span>TV</span>
        <span class="link" id="l1"></span>
        <span class="node" id="nIn"><span class="led"></span><span id="nInText">Input</span></span>
        <span class="link" id="l2"></span>
        <span class="node" id="nPc"><span class="led"></span>PC</span>
      </div>
    </div>
  </section>

  <section class="grid">
    <div class="panel tile" id="tTv"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="5" width="20" height="13" rx="2"/><path d="M8 21h8"/></svg>TV power</div><div class="value" id="vTv">-</div><div class="hint" id="hTv">-</div></div>
    <div class="panel tile" id="tIn"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h11M4 17h16M15 4l3 3-3 3"/></svg>TV input</div><div class="value" id="vIn">-</div><div class="hint" id="hIn">-</div></div>
    <div class="panel tile" id="tNet"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1"/></svg>PC network</div><div class="value" id="vNet">-</div><div class="hint" id="hNet">-</div></div>
    <div class="panel tile" id="tWol"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v10M6.3 6.3a8 8 0 1 0 11.4 0"/></svg>Wake-on-LAN</div><div class="value" id="vWol">-</div><div class="hint" id="hWol">-</div></div>
    <div class="panel tile" id="tSus"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>Suspend</div><div class="value" id="vSus">-</div><div class="hint" id="hSus">-</div></div>
    <div class="panel tile" id="tGuard"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z"/></svg>Input guard</div><div class="value" id="vGuard">-</div><div class="hint" id="hGuard">-</div></div>
    <div class="panel tile" id="tSleep"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9 2h6"/></svg>Sleep check</div><div class="value" id="vSleep">-</div><div class="hint" id="hSleep">-</div></div>
    <div class="panel tile" id="tUp"><div class="label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>CouchLink uptime</div><div class="value" id="vUp">-</div><div class="hint" id="hUp">-</div></div>
  </section>

  <section class="panel console">
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" id="tabLog" aria-selected="true">Events<span class="count" id="cLog"></span></button>
      <button class="tab" role="tab" id="tabCec" aria-selected="false">CEC traffic<span class="count" id="cCec"></span></button>
    </div>
    <div class="lines" id="lines" role="log"></div>
  </section>

  <footer>
    <span id="updated">Not updated yet</span>
    <span>Refreshes every 2 seconds</span>
    <a href="/api">Raw JSON</a>
    <a href="https://github.com/blackhat840/CouchLink" target="_blank" rel="noopener">CouchLink on GitHub</a>
  </footer>
</div>

<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var tab = "log";
  var last = null;

  function dur(s) {
    if (s === null || s === undefined) return "-";
    s = Math.max(0, Math.floor(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    var days = Math.floor(h / 24);
    if (days) return days + "d " + (h % 24) + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + x + "s";
    return x + "s";
  }
  function lastSent(s) { return (s === null || s === undefined) ? "None sent yet" : "Last sent " + dur(s) + " ago"; }
  function every(s) {
    if (!s) return "Checked at startup";
    if (s % 60 === 0) return s === 60 ? "Checked every minute" : "Checked every " + s / 60 + " minutes";
    return "Checked every " + s + "s";
  }
  function set(id, text, cls) {
    var el = $(id);
    el.textContent = text;
    if (cls !== undefined) el.className = "value " + cls;
  }
  function accent(tileId, color) { $(tileId).style.setProperty("--tile-accent", color || "transparent"); }

  function view(d) {
    var want = d.pc_should_be, up = d.pc_reachable, power = d.pc_power;
    var waiting = want === "awake" ? "The TV is still on the PC's input" : "Waiting for the TV to switch to it";
    if (want === "awake" && up) return ["online", "Online", "Ready to play"];
    if (want === "asleep" && up) return ["sleeping", "Going to sleep", "The TV is on another input"];
    if (power === "restarting") return ["waking", "Restarting", "Waiting for the PC to come back"];
    if (want === "awake" && d.wol_attempts) return ["waking", "Waking up", "Sending Wake-on-LAN to the PC"];
    if (power === "off") return ["off", "Off", waiting];
    if (power === "asleep") return ["asleep", "Asleep", waiting];
    if (want === "awake") return ["waking", "Waking up", "Sending Wake-on-LAN to the PC"];
    if (want === "asleep") return ["asleep", "Asleep or off", waiting];
    return ["unknown", "Standing by", "Waiting to hear which input the TV is on"];
  }

  function render(d) {
    last = d;
    var v = view(d);
    document.body.setAttribute("data-state", v[0]);
    $("state").textContent = v[1];
    $("kicker").textContent = d.pc_name || "Gaming PC";
    document.title = (d.pc_name ? d.pc_name + " | " : "") + "CouchLink";
    var sub = $("sub");
    sub.textContent = "";
    sub.appendChild(document.createTextNode(v[2]));
    sub.appendChild(span("sep", "  |  "));
    sub.appendChild(span("muted", d.pc_host || "PC"));

    $("version").textContent = "v" + d.version;
    $("dry").hidden = !d.dry_run;

    var tvOn = d.tv_power === "on";
    var onPc = d.pc_should_be === "awake";
    $("nTv").className = "node" + (tvOn ? " on" : "");
    $("nIn").className = "node" + (tvOn && d.tv_input_address ? " on" : "");
    $("nInText").textContent = d.tv_input_address === "standby" ? "Off" : d.tv_input_address ? d.tv_input : "Input";
    $("nPc").className = "node" + (up(d) ? " on" : "");
    $("l1").className = "link" + (tvOn ? " on" : "");
    $("l2").className = "link" + (onPc && d.pc_reachable ? " on" : "");

    var power = d.tv_power === "on" ? ["On", "good"] : d.tv_power === "standby" ? ["Standby", "calm"] : ["Unknown", "dim"];
    set("vTv", power[0], power[1]);
    $("hTv").textContent = every(d.power_poll_interval);
    accent("tTv", d.tv_power === "on" ? "var(--green)" : null);

    set("vIn", d.tv_input, onPc ? "good" : "");
    $("hIn").textContent = "for " + dur(d.tv_input_for_seconds);
    accent("tIn", onPc ? "var(--green)" : null);

    var offline = {asleep: "Asleep", off: "Off", restarting: "Restarting"}[d.pc_power] || "Offline";
    set("vNet", d.pc_reachable === null ? "Checking" : d.pc_reachable ? "Reachable" : offline,
        d.pc_reachable ? "good" : d.pc_reachable === false ? "calm" : "dim");
    $("hNet").textContent = d.pc_state_seconds === null || d.pc_state_seconds === undefined ? "-"
        : (d.pc_state_since_start ? "for at least " : "for ") + dur(d.pc_state_seconds);
    accent("tNet", d.pc_reachable ? "var(--green)" : null);

    set("vWol", d.wol_attempts ? d.wol_attempts + " sent" : "Idle", d.wol_attempts ? "cool" : "dim");
    $("hWol").textContent = lastSent(d.last_wol_seconds_ago);
    accent("tWol", d.wol_attempts ? "var(--cyan)" : null);

    set("vSus", d.suspend_attempts ? d.suspend_attempts + " sent" : "Idle", d.suspend_attempts ? "warn" : "dim");
    $("hSus").textContent = lastSent(d.last_suspend_seconds_ago);
    accent("tSus", d.suspend_attempts ? "var(--amber)" : null);

    set("vGuard", d.guard_active ? "Armed" : "Idle", d.guard_active ? "cool" : "dim");
    $("hGuard").textContent = "Undoes stray switches";
    accent("tGuard", d.guard_active ? "var(--cyan)" : null);

    var left = d.pc_sleep_check_seconds_left;
    set("vSleep", left === null || left === undefined ? "Idle" : left + "s", left === null || left === undefined ? "dim" : "warn");
    $("hSleep").textContent = {switch: "Then: TV to " + (d.on_pc_sleep_target || "another input"),
                               standby: "Then: TV off"}[d.on_pc_sleep] || "Then: TV left alone";
    accent("tSleep", left === null || left === undefined ? null : "var(--amber)");

    set("vUp", dur(d.uptime_seconds), "");
    var started = new Date(Date.now() - d.uptime_seconds * 1000);
    var at = started.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.uptime_seconds >= 86400) at = started.toLocaleDateString([], { month: "short", day: "numeric" }) + ", " + at;
    $("hUp").textContent = "Since " + at;

    $("cLog").textContent = (d.recent_log || []).length;
    $("cCec").textContent = (d.recent_traffic || []).length;
    drawLines();

    var now = new Date();
    $("updated").textContent = "Updated " + now.toLocaleTimeString();
  }
  function up(d) { return d.pc_reachable === true; }

  var LOG_RE = /^(\d\d:\d\d:\d\d) \[(\w+)\] \[([\w-]+)\] (.*)$/;
  var CEC_RE = /^(\d\d:\d\d:\d\d) .*?(>>|<<)\s*(.*)$/;

  function span(cls, text) {
    var s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  }

  function drawLines() {
    if (!last) return;
    var box = $("lines");
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    var items = tab === "log" ? (last.recent_log || []) : (last.recent_traffic || []);
    var frag = document.createDocumentFragment();
    items.forEach(function (text) {
      var row = document.createElement("div");
      row.className = "line";
      var m;
      if (tab === "log" && (m = LOG_RE.exec(text))) {
        row.appendChild(span("t", m[1]));
        row.appendChild(span("lv " + m[2], m[2]));
        row.appendChild(span("c", m[3]));
        row.appendChild(span("m", m[4]));
      } else if (tab === "cec" && (m = CEC_RE.exec(text))) {
        row.appendChild(span("t", m[1]));
        row.appendChild(span("dir " + (m[2] === ">>" ? "rx" : "tx"), m[2] === ">>" ? "RX" : "TX"));
        row.appendChild(span("m", m[3]));
      } else {
        row.appendChild(span("m", text));
      }
      frag.appendChild(row);
    });
    box.textContent = "";
    if (!items.length) box.appendChild(span("empty", tab === "log" ? "No events yet" : "No CEC traffic yet"));
    box.appendChild(frag);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function selectTab(name) {
    tab = name;
    $("tabLog").setAttribute("aria-selected", name === "log");
    $("tabCec").setAttribute("aria-selected", name === "cec");
    drawLines();
    var box = $("lines");
    box.scrollTop = box.scrollHeight;
  }
  $("tabLog").addEventListener("click", function () { selectTab("log"); });
  $("tabCec").addEventListener("click", function () { selectTab("cec"); });

  function setLive(ok) {
    $("live").className = "live" + (ok ? "" : " off");
    $("liveText").textContent = ok ? "Live" : "Offline";
    if (!ok && !last) {
      $("state").textContent = "No connection";
      $("sub").textContent = "CouchLink is not answering";
    }
  }

  function poll() {
    fetch("/api", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { render(d); setLive(true); })
      .catch(function () { setLive(false); });
  }
  poll();
  setInterval(poll, 2000);
})();
</script>
</body>
</html>
'''


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


def start_dashboard(port, controller):
    page = DASHBOARD_HTML.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api"):
                body = json.dumps(controller.snapshot(), indent=2).encode()
                ctype = "application/json"
            elif self.path in ("/", "/index.html"):
                body = page
                ctype = "text/html; charset=utf-8"
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
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    urls = status_page_urls(port, own_address(controller.cfg.pc_host), socket.gethostname())
    log("STARTUP", f"Status page on {' or '.join(urls)}")


# --------------------------------------------------------------------------
# setup: the settings that don't need CEC addresses
# --------------------------------------------------------------------------
DOCS_URL = "https://github.com/blackhat840/CouchLink/blob/main/docs"
PLACEHOLDERS = {"target-pc.local", "aa:bb:cc:dd:ee:ff"}
MAC_RE = re.compile(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", re.IGNORECASE)


def set_config_value(text, key, value):
    """Set key = value in TOML text, keeping its comment and position. A
    commented-out key is switched on. A missing key is added at the end."""
    rendered = json.dumps(value) if isinstance(value, str) else str(value).lower()
    m = None
    for prefix in (r"", r"#\s*"):          # an active line first, then a commented-out one
        m = re.compile(rf'^(\s*){prefix}{key}(\s*)=\s*("[^"]*"|[^#\n]*?)(\s*#.*)?$', re.M).search(text)
        if m:
            break
    if m is None:
        return text.rstrip("\n") + f"\n{key} = {rendered}\n"
    indent, pad, _, comment = m.groups()
    line = f"{indent}{key}{pad}= {rendered}"
    if comment:
        column = m.start(4) - m.start() + len(comment) - len(comment.lstrip())
        line = line.ljust(column - 1) + " " + comment.lstrip()
    return text[:m.start()] + line + text[m.end():]


def find_mac(host):
    """The MAC address the Pi sees for host, from the ARP table, or None."""
    try:
        subprocess.run(["ping", "-c", "1", "-W", "1", host], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
        ip = socket.gethostbyname(host)
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 4 and fields[0] == ip and fields[3] != "00:00:00:00:00:00":
                    return fields[3].lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def setup_ssh_key(host, user, out):
    """Create root's SSH key if needed, copy it to the PC, and test it."""
    key = "/root/.ssh/id_ed25519"
    if not os.path.exists(key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], check=False)
    out(f"Copying the key to {user}@{host}. Type the PC's password when asked.")
    subprocess.run(["ssh-copy-id", "-i", key + ".pub", f"{user}@{host}"], check=False)
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{user}@{host}", "echo ok"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip() == "ok":
        out("SSH works without a password.")
        return True
    out(f"SSH still asks for a password or can't connect. See {DOCS_URL}/preparing-the-pc.md")
    return False


def run_setup(path, ask=input, out=print, mac_finder=find_mac, ssh_setup=setup_ssh_key):
    """Ask for the settings that don't need CEC addresses and write them to the config."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        out(f"{path} not found. Run the installer first.")
        return 1
    current, _ = load_config(path)
    current = current or dict(DEFAULTS)

    def prompt(question, default=None, check=None, error=""):
        while True:
            hint = f" [{default}]" if default else ""
            answer = ask(f"{question}{hint}: ").strip() or (default or "")
            if answer and (check is None or check(answer)):
                return answer
            out(error or "Please enter a value.")

    def known(key):
        value = current.get(key)
        return value if isinstance(value, str) and value.lower() not in PLACEHOLDERS else None

    out("CouchLink setup. Press Enter to keep the value in brackets.")
    out("")
    out("The PC's IP address. Reserve it in your router so it never changes.")
    host = prompt("PC IP address", known("pc_host"),
                  lambda a: re.fullmatch(r"[A-Za-z0-9.:-]+", a) and a.lower() not in PLACEHOLDERS,
                  "Enter an address such as 192.168.1.50.")

    out("")
    out("The MAC address of the PC's network card, used to wake it. A wired card works best.")
    found = mac_finder(host)
    if found:
        out(f"Found {found} for {host}.")
    mac = prompt("PC MAC address", found or known("pc_mac"),
                 lambda a: MAC_RE.fullmatch(a) and a.lower().replace("-", ":") not in PLACEHOLDERS,
                 "Enter it in the form aa:bb:cc:dd:ee:ff.")

    out("")
    out("The user the Pi logs in to the PC as. It is deck on SteamOS. On Bazzite, use your own username.")
    user = prompt("SSH user", current.get("ssh_user") or "deck",
                  lambda a: re.fullmatch(r"[A-Za-z0-9._-]+", a), "Enter a username such as deck.")

    out("")
    sleep_mode = None
    if current.get("on_pc_sleep") == "switch":
        out("When the PC goes to sleep by itself, the TV switches to another input. Keeping that.")
    else:
        out("When the PC goes to sleep by itself, for example from the Steam menu:")
        out("  1) leave the TV alone")
        out("  2) turn the TV off")
        out("Switching the TV to another input is also possible once you know its CEC address.")
        default = "2" if current.get("on_pc_sleep") == "standby" else "1"
        choice = prompt("Choose 1 or 2", default, lambda a: a in ("1", "2"), "Enter 1 or 2.")
        sleep_mode = "standby" if choice == "2" else "nothing"

    original = text
    for key, value in (("pc_host", host), ("pc_mac", mac.lower().replace("-", ":")),
                       ("ssh_user", user), ("on_pc_sleep", sleep_mode)):
        if value is not None:
            text = set_config_value(text, key, value)
    try:
        with open(path + ".bak", "w") as f:
            f.write(original)
        with open(path, "w") as f:
            f.write(text)
    except PermissionError:
        out(f"Can't write {path}. Run it with sudo: sudo couchlink --setup")
        return 1
    out("")
    out(f"Saved to {path}. The previous version is in {path}.bak.")

    out("")
    out("The Pi needs an SSH key the PC trusts, so it can put the PC to sleep.")
    out("The PC needs a password and its SSH server turned on first (steps 1 and 2 of")
    out(f"{DOCS_URL}/preparing-the-pc.md).")
    if prompt("Set up the SSH key now? (y/n)", "y", lambda a: a.lower() in ("y", "n")).lower() == "y":
        ssh_setup(host, user, out)
    else:
        out("Skipped. Run sudo couchlink --setup again later to do it.")

    settings, _ = load_config(path)
    _, problems = validate(settings)
    out("")
    if problems:
        out("Still to do:")
        for p in problems:
            out(f"  {p}")
        out(f"Find the CEC addresses with {DOCS_URL}/configuration.md")
    else:
        out("The config is complete. Check it with: sudo couchlink --check-config")
    return 0


# --------------------------------------------------------------------------
# migrate-config: bring a config from an older version up to date
# --------------------------------------------------------------------------
RENAMED_SETTINGS = {}       # old name: new name, for settings renamed in a later version
REMOVED_SETTINGS = {}       # name: why, for settings a later version dropped
REQUIRED_SETTINGS = {"pc_host", "pc_mac", "pc_addresses", "pi_address"}
EXAMPLE_PATHS = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "couchlink.example.toml"),
                 "/etc/couchlink/config.example.toml"]
KEY_LINE = r"^[ \t]*{key}[ \t]*=.*$"


def example_settings(example_text):
    """[(section, key, line)] for every active setting in the example config."""
    found, section = [], None
    for line in example_text.splitlines():
        m = re.match(r"\s*\[([A-Za-z0-9_-]+)\]", line)
        if m:
            section = m.group(1)
            continue
        m = re.match(r"([A-Za-z0-9_]+)\s*=", line)
        if m:
            found.append((section, m.group(1), line))
    return found


def add_to_section(text, section, line):
    """Add line at the end of [section], or add the section at the end of the file."""
    lines = text.rstrip("\n").split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(rf"\s*\[{section}\]\s*(#.*)?$", l)), None)
    if start is None:
        return "\n".join(lines) + f"\n\n[{section}]\n{line}\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + [line] + lines[end:]) + "\n"


def migrate_config_text(text, example_text):
    """Returns (new text, changes, problems). changes and problems are lists of
    sentences for the user. problems are things only the user can fix."""
    if tomllib is None:
        return text, [], ["Python 3.11+ (or the 'tomli' package) is needed to read the config file"]
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return text, [], [f"The config is not valid TOML, so it was not changed: {e}"]
    flat = {}
    for key, value in raw.items():
        flat.update(value if isinstance(value, dict) else {key: value})
    changes = []

    for key in sorted(set(flat) - set(DEFAULTS)):
        line_re = re.compile(KEY_LINE.format(key=re.escape(key)), re.M)
        guess = RENAMED_SETTINGS.get(key)
        why = ""
        if guess is None and key not in REMOVED_SETTINGS:
            close = difflib.get_close_matches(key, list(DEFAULTS), n=1, cutoff=0.85)
            guess, why = (close[0], " (it looks like a typo)") if close else (None, "")
        if guess and guess not in flat:
            text = line_re.sub(lambda m: re.sub(rf"\b{re.escape(key)}\b", guess, m.group(0), count=1), text, count=1)
            flat[guess] = flat.pop(key)
            changes.append(f"Renamed {key} to {guess}{why}")
            continue
        reason = REMOVED_SETTINGS.get(key) or "CouchLink does not know this setting"
        text = line_re.sub(lambda m: f"# {reason}. Commented out by --migrate-config:\n# {m.group(0).strip()}",
                           text, count=1)
        changes.append(f"Commented out {key}: {reason}")

    for key, value in sorted(flat.items()):
        default = DEFAULTS.get(key)
        fixed = None
        if isinstance(default, bool) and isinstance(value, str) and value.lower() in ("true", "false", "yes", "no"):
            fixed = value.lower() in ("true", "yes")
        elif (isinstance(default, (int, float)) and not isinstance(default, bool) and isinstance(value, str)
              and re.fullmatch(r"\d+(\.\d+)?", value.strip())):
            fixed = float(value) if "." in value else int(value)
        if fixed is not None:
            text = set_config_value(text, key, fixed)
            changes.append(f"Changed {key} from \"{value}\" to {str(fixed).lower()}")

    for section, key, line in example_settings(example_text):
        if key not in flat and key in DEFAULTS and key not in REQUIRED_SETTINGS:
            text = add_to_section(text, section, line)
            flat[key] = DEFAULTS[key]
            changes.append(f"Added {key} to [{section}], with its default value")

    problems = []
    try:
        settings = dict(DEFAULTS)
        new = tomllib.loads(text)
        for key, value in new.items():
            settings.update(value if isinstance(value, dict) else {key: value})
        problems = validate(settings)[1]
    except tomllib.TOMLDecodeError as e:
        problems = [f"The migrated config is not valid TOML: {e}"]
    return text, changes, problems


def migrate_config(path, dry_run=False, out=print):
    """Bring the config at path up to date. Returns an exit code."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        out(f"{path} not found")
        return 1
    example = next((p for p in EXAMPLE_PATHS if os.path.exists(p)), None)
    example_text = open(example).read() if example else ""
    new, changes, problems = migrate_config_text(text, example_text)
    if any("not valid TOML" in p for p in problems) and new == text:
        for p in problems:
            out(p)
        return 1
    if not changes:
        out(f"{path} is up to date for CouchLink {VERSION}. Nothing to change.")
    else:
        out(f"{'Would make' if dry_run else 'Making'} these changes to {path}:")
        for c in changes:
            out(f"  - {c}")
        if not dry_run:
            try:
                with open(path + ".bak", "w") as f:
                    f.write(text)
                with open(path, "w") as f:
                    f.write(new)
            except PermissionError:
                out(f"Can't write {path}. Run it with sudo.")
                return 1
            out(f"Saved. The previous version is in {path}.bak.")
    if problems:
        out("These need your attention:")
        for p in problems:
            out(f"  - {p}")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="CouchLink: keep a gaming PC's power state in line with the TV over HDMI-CEC")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"config file (default {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--dry-run", action="store_true",
                    help="watch the bus and log decisions without waking, suspending or switching anything")
    ap.add_argument("--check-config", action="store_true", help="validate the config and exit")
    ap.add_argument("--setup", action="store_true",
                    help="ask for the settings that don't need CEC addresses and save them")
    ap.add_argument("--migrate-config", action="store_true",
                    help="bring a config from an older version up to date, explaining each change "
                         "(with --dry-run, only show the changes)")
    args = ap.parse_args(argv)
    if args.migrate_config:
        sys.exit(migrate_config(args.config, dry_run=args.dry_run))
    if args.setup:
        if os.geteuid() != 0:
            print("Run it with sudo: sudo couchlink --setup")
            sys.exit(1)
        sys.exit(run_setup(args.config))

    settings, problems = load_config(args.config)
    cfg = None
    if settings is not None:
        cfg, more = validate(settings)
        problems += more
    if problems:
        for p in problems:
            log("CONFIG", p, level="ERROR")
        sys.exit(EX_CONFIG)
    global SHOW_DEBUG
    SHOW_DEBUG = cfg.raw_debug
    if args.check_config:
        for k, v in sorted(vars(cfg).items()):
            print(f"{k} = {v!r}")
        print("Config OK")
        return

    events = queue.Queue()
    try:
        cec = CecClient(cfg.cec_client_command, events, cfg.raw_debug)
    except FileNotFoundError:
        log("STARTUP", f"{cfg.cec_client_command[0]} is not installed (sudo apt install cec-utils)",
            level="ERROR")
        sys.exit(EX_CONFIG)
    actions = (DryRunActions if args.dry_run else RealActions)(cfg, cec, events)
    ctl = Controller(cfg, actions)
    log("STARTUP", f"CouchLink {VERSION}{' DRY RUN' if args.dry_run else ''} starting. "
                   f"PC input: {', '.join(ctl.input_name(a) for a in cfg.pc_addresses)}, "
                   f"Pi: {ctl.input_name(cfg.pi_address)}, PC address: {cfg.pc_host}")
    threading.Thread(target=pinger, args=(cfg, events), daemon=True).start()
    if cfg.watch_pc_power:
        threading.Thread(target=power_watcher, args=(cfg, events), daemon=True).start()
    if cfg.dashboard_port:
        start_dashboard(cfg.dashboard_port, ctl)

    last_tick = 0.0
    while True:
        try:
            event = events.get(timeout=cfg.tick_interval)
        except queue.Empty:
            event = None
        if event is not None:
            if event[0] == "cec_exit":
                log("STARTUP", f"cec-client exited with code {event[1]}. Exiting so systemd restarts CouchLink",
                    level="ERROR")
                sys.exit(1)
            try:
                ctl.handle(event)
            except Exception as e:      # one bad message must not stop CouchLink
                log("CONTROL", f"Error handling {event[0]} event: {e!r}", level="ERROR")
        if time.monotonic() - last_tick >= min(cfg.tick_interval, 0.25) or event is not None:
            last_tick = time.monotonic()
            try:
                ctl.tick()
            except Exception as e:
                log("CONTROL", f"Error in tick: {e!r}", level="ERROR")


if __name__ == "__main__":
    main()
