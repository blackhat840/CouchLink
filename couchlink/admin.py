# SPDX-License-Identifier: GPL-3.0-or-later
"""The settings page, at /admin, behind a password: changing settings, API keys
and the password, finding the HDMI inputs, and the first-time setup. The page
itself is static/admin.html and static/admin.js."""

import re
import time

from .auth import (KEY_NAME_RE, MIN_PASSWORD, SESSION_DAYS, check_password, cookie, hash_api_key,
                   hash_password, make_session, new_api_key, read_setup_code, remove_setup_code,
                   session_ok, setup_code_ok)
from .cec import NoCec, dotted
from .common import log
from .config import (DEFAULTS, load_config, remove_api_key, save_config, set_api_key, set_config_value,
                     settings_from_text, unset_config_value, validate)
from .setup_pc import PLACEHOLDERS, check_ssh, find_mac

COOKIE = "couchlink_admin"

# The settings on the page: (group, key, label, kind, options). Everything else stays a
# matter for the config file.
FIELDS = [
    ("The PC", "pc_name", "Name on the status page", "text", {"blank": "Ask the PC for its name"}),
    ("The PC", "pc_host", "IP address", "text", {}),
    ("The PC", "pc_mac", "MAC address, for Wake-on-LAN", "text", {}),
    ("The PC", "ssh_user", "SSH user", "text", {}),
    ("TV and HDMI inputs", "use_tv", "A TV is plugged into the Pi. Off: only wake the PC for remote play",
     "bool", {}),
    ("TV and HDMI inputs", "pc_addresses", "The PC's input, such as 12:00. Separate several with commas",
     "addresses", {"blank": "Not needed without a TV"}),
    ("TV and HDMI inputs", "pi_address", "The Pi's input", "address", {"blank": "Not needed without a TV"}),
    ("TV and HDMI inputs", "soundbar_address", "The soundbar's or receiver's input", "address",
     {"blank": "No soundbar or receiver"}),
    ("When the PC wakes or sleeps", "auto_switch_on_pc_wake", "When the PC wakes by itself", "select",
     {"options": [["always", "Turn the TV on and switch it to the PC"],
                  ["if_tv_on", "Switch the TV to the PC only if it is on"],
                  ["never", "Leave the TV alone"]]}),
    ("When the PC wakes or sleeps", "on_pc_sleep", "When the PC goes to sleep by itself", "select",
     {"options": [["nothing", "Leave the TV alone"], ["standby", "Turn the TV off"],
                  ["switch", "Switch the TV to another input"]]}),
    ("When the PC wakes or sleeps", "pc_sleep_switch_to", "The input to switch to", "address",
     {"blank": "Only needed with Switch the TV to another input"}),
    ("Status page", "status_page_buttons", "Wake and sleep buttons on the status page", "bool", {}),
    ("Timing", "away_debounce", "Seconds off the PC's input before the PC is suspended", "number", {}),
    ("Timing", "pc_sleep_confirm", "Seconds offline before the PC counts as asleep", "number", {}),
    ("Diagnostics", "raw_debug", "Log raw CEC traffic", "bool", {}),
]
KINDS = {key: kind for _, key, _, kind, _ in FIELDS}

# What still has to be filled in, said plainly, for the setting each config problem names
TODO = {
    "pc_host": "The PC's IP address",
    "pc_mac": "The PC's MAC address. \"Find it from the IP address\" can fill it in",
    "pc_addresses": "The PC's HDMI input. \"Find the HDMI inputs\" can fill it in",
    "pi_address": "The Pi's HDMI input. \"Find the HDMI inputs\" can fill it in",
}


def todo(problems):
    return [TODO.get(p.split(" ", 1)[0], p) for p in problems]


def field_value(kind, value):
    """A setting's value as the page shows it."""
    if kind == "addresses":
        return ", ".join(value or [])
    if kind in ("text", "address"):
        return value or ""
    return value


def parse_field(kind, value, options):
    """(value to save, or None to remove it, error or None) for what the page sent."""
    if kind == "bool":
        return (value, None) if isinstance(value, bool) else (None, "must be on or off")
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "must be a number"
        return (int(value) if float(value).is_integer() else value), None
    if not isinstance(value, str):
        return None, "must be text"
    value = value.strip()
    if kind == "select":
        return (value, None) if value in [o[0] for o in options["options"]] else (None, "is not one of the choices")
    if kind == "addresses":
        items = [a for a in value.replace(",", " ").split() if a]
        if items:
            return items, None
        return (None, None) if "blank" in options else (None, "needs at least one input")
    if not value:
        return (None, None) if "blank" in options else (None, "can't be empty")
    return value, None


class Admin:
    def __init__(self, controller, events, config_path, guard, cec=None):
        self.controller = controller
        self.events = events
        self.path = config_path
        self.guard = guard
        self.cec = cec          # the running cec-client, for finding the inputs

    # ---- reading the config -----------------------------------------------
    def read(self):
        """(text, settings) of the config file, or (None, None)."""
        if not self.path:
            return None, None
        try:
            with open(self.path) as f:
                text = f.read()
        except OSError:
            return None, None
        settings, _ = load_config(self.path)
        return text, settings

    def password_hash(self):
        _, settings = self.read()
        return (settings or {}).get("admin_password_hash") or ""

    def logged_in(self, headers):
        return session_ok(cookie(headers, COOKIE), self.password_hash())

    def session_cookie(self, secret):
        token = make_session(secret)
        return ("Set-Cookie", f"{COOKIE}={token}; Path=/admin; HttpOnly; SameSite=Strict; "
                              f"Max-Age={SESSION_DAYS * 86400}")

    # ---- GET /admin/api/state -------------------------------------------
    def state(self, headers):
        """(status, payload) for the page: what to show, and the current values."""
        text, settings = self.read()
        if settings is None:
            return 503, {"ok": False, "result": "no_config", "message": "CouchLink can't read its config file"}
        if not settings.get("admin_password_hash"):
            if read_setup_code(self.path):
                return 403, {"ok": False, "result": "setup",
                             "message": "Enter the setup code the installer showed, and choose a password"}
            return 403, {"ok": False, "result": "no_password",
                         "message": "Get a setup code with: sudo couchlink --setup-code"}
        if not session_ok(cookie(headers, COOKIE), settings["admin_password_hash"]):
            return 401, {"ok": False, "result": "login_needed", "message": "Log in"}
        fields = []
        for group, key, label, kind, options in FIELDS:
            fields.append({"group": group, "key": key, "label": label, "kind": kind,
                           "value": field_value(kind, settings.get(key)),
                           "options": options.get("options"), "blank": options.get("blank")})
        keys = sorted((settings.get("api_keys") or {}))
        return 200, {"ok": True, "fields": fields, "keys": keys, "todo": todo(validate(settings)[1]),
                     "cec": self.cec_ready(), "pi_logical_address": settings.get("pi_logical_address", 1)}

    def cec_ready(self):
        return self.cec is not None and not isinstance(self.cec, NoCec)

    # ---- POST /admin/... -------------------------------------------------
    def post(self, path, params, headers, source):
        """(status, payload, headers, after). after runs once the answer is sent."""
        if path == "/admin/login":
            return self.login(params, source)
        if path == "/admin/setup":
            return self.first_password(params, source)
        if path == "/admin/logout":
            return 200, {"ok": True, "result": "logged_out"}, \
                [("Set-Cookie", f"{COOKIE}=; Path=/admin; HttpOnly; SameSite=Strict; Max-Age=0")], None
        if not self.logged_in(headers):
            return 401, {"ok": False, "result": "login_needed", "message": "Log in"}, (), None
        handlers = {"/admin/settings": self.save_settings, "/admin/keys/new": self.create_key,
                    "/admin/keys/revoke": self.revoke_key, "/admin/password": self.change_password,
                    "/admin/cec/scan": self.cec_scan, "/admin/cec/heard": self.cec_heard,
                    "/admin/find-mac": self.find_mac, "/admin/check-ssh": self.check_ssh}
        handler = handlers.get(path)
        if handler is None:
            return 404, {"ok": False, "result": "not_found", "message": "No such page"}, (), None
        return handler(params, source)

    def login(self, params, source):
        stored = self.password_hash()
        if not stored:
            return 403, {"ok": False, "result": "no_password",
                         "message": "No password is set yet. Use the setup code"}, (), None
        if self.guard.locked(source):
            return 429, {"ok": False, "result": "locked_out",
                         "message": "Too many wrong passwords. Try again in a minute"}, (), None
        if not check_password(str(params.get("password") or ""), stored):
            self.guard.failed(source)
            log("ADMIN", f"Wrong password for the settings page from {source}", level="WARN")
            return 401, {"ok": False, "result": "wrong_password", "message": "Wrong password"}, (), None
        log("ADMIN", f"Logged in to the settings page from {source}")
        return 200, {"ok": True, "result": "logged_in"}, [self.session_cookie(stored)], None

    def first_password(self, params, source):
        """The first visit: the setup code proves it's the person who installed CouchLink."""
        text, settings = self.read()
        if text is None:
            return 503, {"ok": False, "result": "no_config", "message": "CouchLink can't read its config file"}, (), None
        if settings.get("admin_password_hash"):
            return 409, {"ok": False, "result": "has_password", "message": "A password is already set. Log in"}, (), None
        if self.guard.locked(source):
            return 429, {"ok": False, "result": "locked_out",
                         "message": "Too many wrong codes. Try again in a minute"}, (), None
        if not setup_code_ok(str(params.get("code") or ""), self.path):
            self.guard.failed(source)
            log("ADMIN", f"Wrong setup code for the settings page from {source}", level="WARN")
            return 401, {"ok": False, "result": "wrong_code", "message": "Wrong setup code"}, (), None
        password = str(params.get("password") or "")
        if len(password) < MIN_PASSWORD:
            return 400, {"ok": False, "result": "too_short",
                         "message": f"Use at least {MIN_PASSWORD} characters"}, (), None
        hashed = hash_password(password)
        self.write(text, set_config_value(text, "admin_password_hash", hashed), source,
                   "Settings page password set with the setup code")
        remove_setup_code(self.path)
        return 200, {"ok": True, "result": "password_set", "message": "Password set"}, \
            [self.session_cookie(hashed)], None

    # ---- finding the HDMI inputs, through the running cec-client ----------
    def cec_scan(self, params, source):
        if not self.cec_ready():
            return 409, {"ok": False, "result": "no_cec", "message": "CouchLink isn't using the CEC adapter. Turn on "
                         "\"A TV is plugged into the Pi\", save, and try again"}, (), None
        log("ADMIN", f"Scanning the HDMI bus for the settings page, from {source}")
        devices = self.cec.scan()
        if not devices:
            return 504, {"ok": False, "result": "no_devices", "message": "No CEC devices answered. Check that the TV "
                         "is on, that the Pi is plugged into it, and that CEC is turned on in the TV's settings. "
                         "TVs call it Anynet+, Bravia Sync, SimpLink, CEC or HDMI control"}, (), None
        for d in devices:
            d["dotted"] = dotted(d["address"])
        return 200, {"ok": True, "devices": devices}, (), None

    def cec_heard(self, params, source):
        """The inputs switched to since the page started listening. With no since, starts listening."""
        if not self.cec_ready():
            return 409, {"ok": False, "result": "no_cec", "message": "CouchLink isn't using the CEC adapter"}, (), None
        since = params.get("since")
        if not isinstance(since, (int, float)) or isinstance(since, bool):
            return 200, {"ok": True, "since": time.monotonic(), "heard": []}, (), None
        heard = self.cec.heard_since(since)
        return 200, {"ok": True, "since": since, "heard": [{"address": a, "dotted": dotted(a)} for a in heard]}, (), None

    def find_mac(self, params, source):
        host = str(params.get("host") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host) or host.lower() in PLACEHOLDERS:
            return 400, {"ok": False, "result": "bad_host", "message": "Enter the PC's IP address first"}, (), None
        mac = find_mac(host)
        if not mac:
            return 404, {"ok": False, "result": "not_found", "message": "The Pi couldn't see the PC. Check that "
                         "the PC is on and the address is right, then try again"}, (), None
        return 200, {"ok": True, "mac": mac, "message": f"Found {mac}"}, (), None

    def check_ssh(self, params, source):
        _, settings = self.read()
        host = (settings or {}).get("pc_host")
        if not host or host in PLACEHOLDERS:
            return 400, {"ok": False, "result": "bad_host", "message": "Save the PC's address first"}, (), None
        ok, message = check_ssh(host, settings.get("ssh_user") or "deck")
        if not ok:
            message += ". To set up the key: sudo bash install.sh --setup-ssh"
        return 200, {"ok": ok, "result": "works" if ok else "fails", "message": message}, (), None

    def write(self, old, new, source, what):
        save_config(self.path, old, new)
        log("ADMIN", f"{what} from {source}")

    def save_settings(self, params, source):
        values = params.get("values")
        if not isinstance(values, dict):
            return 400, {"ok": False, "result": "bad_request", "message": "Nothing to save"}, (), None
        text, settings = self.read()
        if text is None:
            return 503, {"ok": False, "result": "no_config", "message": "CouchLink can't read its config file"}, (), None
        options = {key: opts for _, key, _, _, opts in FIELDS}
        new, changed, problems = text, [], []
        for key, raw in values.items():
            kind = KINDS.get(key)
            if kind is None:
                problems.append(f"{key} can't be changed here")
                continue
            value, error = parse_field(kind, raw, options[key])
            if error:
                problems.append(f"{key} {error}")
                continue
            if value == settings.get(key) or (value is None and not settings.get(key)):
                continue
            new = unset_config_value(new, key) if value is None else set_config_value(new, key, value)
            changed.append(key)
        if not problems:
            checked, problems = settings_from_text(new)
            if checked is not None:
                problems = problems + validate(checked)[1]
        if problems:
            return 400, {"ok": False, "result": "invalid", "message": "Nothing was saved", "problems": problems}, (), None
        if not changed:
            return 200, {"ok": True, "result": "unchanged", "message": "Nothing changed"}, (), None
        self.write(text, new, source, f"Settings changed: {', '.join(changed)}")
        return 200, {"ok": True, "result": "saved", "message": "Saved. CouchLink is restarting to use them"}, (), \
            lambda: self.events.put(("restart", f"settings were changed ({', '.join(changed)})"))

    def keys_changed(self, settings):
        """Use the new keys straight away. No restart needed."""
        self.controller.cfg.api_keys = dict(settings.get("api_keys") or {})

    def create_key(self, params, source):
        name = str(params.get("name") or "").strip()
        if not KEY_NAME_RE.fullmatch(name) or name in DEFAULTS:
            return 400, {"ok": False, "result": "bad_name",
                         "message": "Use a name of letters, digits, - and _, such as home-assistant"}, (), None
        text, settings = self.read()
        if name in (settings.get("api_keys") or {}):
            return 409, {"ok": False, "result": "exists", "message": f"There is already a key named {name}"}, (), None
        key = new_api_key()
        new = set_api_key(text, name, hash_api_key(key))
        self.write(text, new, source, f"API key {name} generated")
        self.keys_changed(settings_from_text(new)[0])
        return 200, {"ok": True, "result": "created", "name": name, "key": key,
                     "message": "Copy the key now. It can't be shown again"}, (), None

    def revoke_key(self, params, source):
        name = str(params.get("name") or "")
        text, settings = self.read()
        if name not in (settings.get("api_keys") or {}):
            return 404, {"ok": False, "result": "no_key", "message": f"There is no key named {name}"}, (), None
        new = remove_api_key(text, name)
        self.write(text, new, source, f"API key {name} revoked")
        self.keys_changed(settings_from_text(new)[0])
        return 200, {"ok": True, "result": "revoked", "message": f"Revoked {name}"}, (), None

    def change_password(self, params, source):
        current, new_password = str(params.get("current") or ""), str(params.get("new") or "")
        text, settings = self.read()
        if not check_password(current, settings.get("admin_password_hash")):
            self.guard.failed(source)
            return 401, {"ok": False, "result": "wrong_password", "message": "The current password is wrong"}, (), None
        if len(new_password) < MIN_PASSWORD:
            return 400, {"ok": False, "result": "too_short",
                         "message": f"Use at least {MIN_PASSWORD} characters"}, (), None
        hashed = hash_password(new_password)
        self.write(text, set_config_value(text, "admin_password_hash", hashed), source,
                   "Settings page password changed")
        # Every other login ends, because the cookie is signed with the password's hash
        return 200, {"ok": True, "result": "changed", "message": "Password changed"}, \
            [self.session_cookie(hashed)], None

