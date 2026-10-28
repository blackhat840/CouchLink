# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: defaults, loading, checking, writing single values, and bringing
configs from older versions up to date (--migrate-config)."""

import difflib
import json
import os
import re
from types import SimpleNamespace

from . import VERSION


try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


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
    "status_page_buttons": True,        # wake and sleep buttons on the status page, no key needed
    "api_key_hash": "",                 # set by "sudo couchlink --new-api-key". Lets scripts use the API

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
    problems = [f"Unknown setting '{k}' (typo?)" for k in sorted(set(flat) - set(DEFAULTS) - set(REMOVED_SETTINGS))]
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
    if not isinstance(s["status_page_buttons"], bool):
        problems.append("status_page_buttons must be true or false")
    if s["api_key_hash"] and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(s["api_key_hash"])):
        problems.append("api_key_hash is not valid. Generate a new key with: sudo couchlink --new-api-key")
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


def set_config_value(text, key, value):
    """Set key = value in TOML text, keeping its comment and position. A
    commented-out key is switched on. A missing key is added at the end."""
    rendered = json.dumps(value) if isinstance(value, (str, list)) else str(value).lower()
    m = None
    for prefix in (r"", r"#[ \t]*"):       # an active line first, then a commented-out one
        m = re.compile(rf'^([ \t]*){prefix}{key}([ \t]*)=[ \t]*("[^"]*"|[^#\n]*?)([ \t]*#.*)?$', re.M).search(text)
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


def unset_config_value(text, key):
    """Comment out key's active line, if there is one."""
    return re.sub(rf'^([ \t]*)({key}[ \t]*=.*)$', r"\1# \2", text, count=1, flags=re.M)


RENAMED_SETTINGS = {}       # old name: new name, for settings renamed in a later version


REMOVED_SETTINGS = {       # name: why, for settings a later version dropped
    "remote_session_hours": "remote sessions no longer end on a timer. They end when the PC goes to "
                            "sleep by itself, or with End remote session or Put PC to sleep",
}


REQUIRED_SETTINGS = {"pc_host", "pc_mac", "pc_addresses", "pi_address"}


EXAMPLE_PATHS = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "couchlink.example.toml"),
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
