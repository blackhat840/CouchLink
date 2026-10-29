# SPDX-License-Identifier: GPL-3.0-or-later
"""sudo couchlink --setup: the settings that don't need CEC addresses, the SSH
key and the settings page's password, plus --new-api-key and --admin-password."""

import getpass
import os
import re
import socket
import subprocess
import time

from .actions import send_magic_packet
from .auth import KEY_NAME_RE, MIN_PASSWORD, hash_api_key, hash_password, remove_setup_code
from .auth import new_api_key as generate_key
from .common import DOCS_URL
from .config import DEFAULTS, load_config, save_answers, set_api_key, set_config_value, update_config, validate
from .setup_cec import couchlink_service


PLACEHOLDERS = {"target-pc.local", "aa:bb:cc:dd:ee:ff"}


MAC_RE = re.compile(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", re.IGNORECASE)


def show_api_key(key, name, path, out):
    out(f"New API key {name}: {key}")
    out("")
    out(f"Copy it now. Only a hash of it is saved in {path}, so it can't be shown again.")
    out(f"Generating another key named {name} replaces it. Keys with other names keep working.")
    out("Scripts send it as:")
    out("  Authorization: Bearer <key>")


def new_api_key(path, name="default", out=print):
    """Generate an API key with a name, save its hash to the config and show the key once."""
    if not KEY_NAME_RE.fullmatch(name) or name in DEFAULTS:
        out("Use a name of letters, digits, - and _, such as home-assistant, that isn't a setting's name.")
        return 1
    if not os.path.exists(path):
        out(f"{path} not found. Run the installer first.")
        return 1
    key = generate_key()
    try:
        update_config(path, lambda text: set_api_key(text, name, hash_api_key(key)))
    except PermissionError:
        out(f"Can't write {path}. Run it with sudo: sudo couchlink --new-api-key {name}")
        return 1
    show_api_key(key, name, path, out)
    out("Restart CouchLink to use it: sudo systemctl restart couchlink")
    out("Keys generated on the settings page work straight away.")
    return 0


def ask_password(secret, out):
    """Ask for a new password twice. Returns it, or None if the user gave up."""
    for _ in range(3):
        first = secret("New password for the settings page: ")
        if len(first) < MIN_PASSWORD:
            out(f"Use at least {MIN_PASSWORD} characters.")
            continue
        if secret("The same password again: ") != first:
            out("The two didn't match.")
            continue
        return first
    return None


def set_admin_password(path, out=print, secret=getpass.getpass):
    """sudo couchlink --admin-password: set the settings page's password."""
    if not os.path.exists(path):
        out(f"{path} not found. Run the installer first.")
        return 1
    password = ask_password(secret, out)       # before taking the lock, which isn't held while you type
    if password is None:
        out("The password was not changed.")
        return 1
    hashed = hash_password(password)
    try:
        update_config(path, lambda text: set_config_value(text, "admin_password_hash", hashed))
    except PermissionError:
        out("Run it with sudo: sudo couchlink --admin-password")
        return 1
    remove_setup_code(path)
    out("Saved. Open the settings page at http://<pi>:8080/admin and log in.")
    out("It works straight away. Anyone logged in on another device has to log in again.")
    return 0


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


SSH_PROBLEMS = [
    ("no route to host", "The Pi can't reach the PC. It is probably asleep or off. On Bazzite, the firewall "
                         "also answers like this. To allow SSH, run this on the PC: "
                         "sudo firewall-cmd --permanent --add-service=ssh && sudo firewall-cmd --reload"),
    ("connection refused", "The PC is on, but its SSH server isn't running. Run this on the PC: "
                           "sudo systemctl enable --now sshd"),
    ("timed out", "The PC didn't answer. It may be asleep or off, or a firewall drops SSH."),
    ("could not resolve", "The Pi can't find the PC by that name. Use its IP address instead."),
    ("host key verification failed", "The PC's key wasn't accepted. Run this again and answer yes "
                                     "when asked whether to continue connecting."),
    ("permission denied", "The PC didn't accept the key. Check that the SSH user is your account on "
                          "the PC. On Bazzite, also run this on the PC: restorecon -Rv ~/.ssh"),
]


def ssh_problem(error):
    """Advice for an ssh error message."""
    text = error.lower()
    for needle, advice in SSH_PROBLEMS:
        if needle in text:
            return advice
    return "SSH still asks for a password or can't connect."


def pc_answers(host):
    """Whether the PC answers one ping."""
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "1", host], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wake_pc(host, send, out, answers=pc_answers, clock=time.monotonic, sleep=time.sleep, seconds=90):
    """Send Wake-on-LAN until the PC answers ping, for up to seconds. Returns whether it did."""
    out(f"Sending Wake-on-LAN. Waiting up to {seconds} seconds for the PC to answer.")
    end = clock() + seconds
    next_send = clock()
    while clock() < end:
        if clock() >= next_send:
            send()
            next_send = clock() + 15
        if answers(host):
            out("The PC is awake.")
            return True
        sleep(2)
    out("The PC didn't wake up. Turn it on by hand, and see the Wake-on-LAN steps in")
    out(f"{DOCS_URL}/preparing-the-pc.md")
    return False


def check_ssh(host, user):
    """(works, advice) for SSH from the Pi to the PC without a password."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{user}@{host}", "echo ok"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Couldn't run ssh: {e}"
    if r.returncode == 0 and r.stdout.strip() == "ok":
        return True, "SSH to the PC works, so CouchLink can put it to sleep"
    error = (r.stderr or "").strip().splitlines()
    return False, (f"ssh said: {error[-1]}. " if error else "") + ssh_problem(r.stderr or "")


def setup_ssh_only(path, out=print, ssh_setup=None):
    """sudo couchlink --setup-ssh: only the SSH key, for the PC in the config."""
    settings, _ = load_config(path)
    host = (settings or {}).get("pc_host")
    if not host or host in PLACEHOLDERS:
        out("Set the PC's address first, on the settings page or with: sudo bash install.sh --setup")
        return 1
    user = settings.get("ssh_user") or "deck"
    out("The Pi needs an SSH key the PC trusts, so it can put the PC to sleep. The PC needs a password")
    out(f"and its SSH server turned on first (steps 1 and 2 of {DOCS_URL}/preparing-the-pc.md).")
    return 0 if (ssh_setup or setup_ssh_key)(host, user, out) else 1


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
    error = (r.stderr or "").strip().splitlines()
    if error:
        out(f"ssh said: {error[-1]}")
    out(ssh_problem(r.stderr or ""))
    out(f"See {DOCS_URL}/preparing-the-pc.md")
    return False


def tv_questions(prompt, current, out):
    """What to do when the PC wakes or sleeps by itself. Returns (wake mode, sleep mode or None)."""
    out("")
    out("When the PC wakes by itself, for example from a controller or its power button:")
    out("  1) turn the TV on and switch it to the PC")
    out("  2) switch the TV to the PC only if the TV is already on")
    out("  3) leave the TV alone. The PC goes back to sleep unless you switch the TV to it")
    out("This is not for remote play. To stream to another device, use Wake PC on the status")
    out("page, which keeps the PC awake without the TV.")
    wake_modes = {"1": "always", "2": "if_tv_on", "3": "never"}
    default = {v: k for k, v in wake_modes.items()}.get(current.get("auto_switch_on_pc_wake"), "1")
    wake_mode = wake_modes[prompt("Choose 1, 2 or 3", default, lambda a: a in wake_modes, "Enter 1, 2 or 3.")]

    out("")
    sleep_mode = None
    if current.get("on_pc_sleep") == "switch":
        out("When the PC goes to sleep by itself, the TV switches to another input. Keeping that.")
        out("Change it with: sudo couchlink --setup-cec")
    else:
        out("When the PC goes to sleep by itself, for example from the Steam menu:")
        out("  1) leave the TV alone")
        out("  2) turn the TV off")
        out("Switching the TV to another input, such as a streaming box, is set up with the CEC")
        out("addresses: sudo couchlink --setup-cec")
        default = "2" if current.get("on_pc_sleep") == "standby" else "1"
        choice = prompt("Choose 1 or 2", default, lambda a: a in ("1", "2"), "Enter 1 or 2.")
        sleep_mode = "standby" if choice == "2" else "nothing"
    return wake_mode, sleep_mode


def run_setup(path, ask=input, out=print, mac_finder=find_mac, ssh_setup=setup_ssh_key,
              pc_check=pc_answers, waker=None, service=None, secret=getpass.getpass):
    """Ask for the settings that don't need CEC addresses and write them to the config."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        out(f"{path} not found. Run the installer first.")
        return 1
    current, _ = load_config(path)
    current = current or dict(DEFAULTS)

    def prompt(question, default=None, check=None, error="", blank_ok=False):
        while True:
            hint = f" [{default}]" if default else ""
            answer = ask(f"{question}{hint}: ").strip() or (default or "")
            if not answer and blank_ok:
                return ""
            if answer and (check is None or check(answer)):
                return answer
            out(error or "Please enter a value.")

    def yes(question, default):
        return prompt(question, default, lambda a: a.lower() in ("y", "n"), "Enter y or n.").lower() == "y"

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
    out("A name for the PC on the status page. Press Enter to use the PC's own name.")
    name = prompt("PC name", current.get("pc_name") or None, lambda a: len(a) <= 40,
                  "Use 40 characters or fewer.", blank_ok=True)

    out("")
    out("Is the Pi plugged into a TV?")
    out("  1) Yes. CouchLink follows the TV: the PC wakes when you switch to it and sleeps when")
    out("     you switch away")
    out("  2) No. CouchLink only wakes the PC for remote play, such as streaming to a Steam Deck,")
    out("     and puts it to sleep when asked. The PC's own sleep timer does the rest")
    use_tv = prompt("Choose 1 or 2", "1" if current.get("use_tv", True) else "2",
                    lambda a: a in ("1", "2"), "Enter 1 or 2.") == "1"
    wake_mode = sleep_mode = None
    if use_tv:
        wake_mode, sleep_mode = tv_questions(prompt, current, out)

    out("")
    out("The status page can show buttons to wake the PC and put it to sleep. They need no key,")
    out("so anyone who can open the page can use them.")
    buttons = prompt("Show the buttons? (y/n)", "y" if current.get("status_page_buttons", True) else "n",
                     lambda a: a.lower() in ("y", "n"), "Enter y or n.").lower() == "y"

    out("")
    out("The settings page, at /admin, lets you change settings and API keys from a browser.")
    out("It needs a password.")
    has_password = bool(current.get("admin_password_hash"))
    password = None
    if yes("Change the settings page's password? (y/n)" if has_password
           else "Set a password for the settings page now? (y/n)", "n" if has_password else "y"):
        password = ask_password(secret, out)
        if password is None:
            out("Skipped. Set it later with: sudo couchlink --admin-password")

    out("")
    out("Scripts, phone shortcuts and Home Assistant can wake the PC and put it to sleep through")
    out("CouchLink's API, with an API key. Each key has a name, so you can revoke one without")
    out("touching the others. The status page's buttons don't need a key.")
    existing = sorted(current.get("api_keys") or {})
    if existing:
        out(f"Keys you have: {', '.join(existing)}")
    api_key = key_name = None
    if yes("Generate a new API key now? (y/n)", "n"):
        key_name = prompt("Name for the key", "home-assistant",
                          lambda a: KEY_NAME_RE.fullmatch(a) and a not in DEFAULTS,
                          "Use letters, digits, - and _, such as home-assistant.")
        if key_name in existing:
            out(f"This replaces the key named {key_name}, and the old one stops working.")
        api_key = generate_key()

    answers = [(key, value) for key, value in (
        ("pc_host", host), ("pc_mac", mac.lower().replace("-", ":")), ("ssh_user", user),
        ("pc_name", name or None), ("auto_switch_on_pc_wake", wake_mode), ("on_pc_sleep", sleep_mode),
        ("use_tv", use_tv), ("status_page_buttons", buttons),
        ("admin_password_hash", hash_password(password) if password else None)) if value is not None]

    def answer(config):
        for key, value in answers:
            config = set_config_value(config, key, value)
        return set_api_key(config, key_name, hash_api_key(api_key)) if api_key else config
    answered = {key for key, _ in answers} | ({"api_keys"} if api_key else set())
    try:
        saved, kept, problem = save_answers(path, text, answer, answered)
    except PermissionError:
        out(f"Can't write {path}. Run it with sudo: sudo couchlink --setup")
        return 1
    if problem:
        out(f"Nothing was saved: {problem}. Run sudo bash install.sh --setup again.")
        return 1
    out("")
    out(f"Saved to {path}. The previous version is in {path}.bak." if saved else f"Nothing changed in {path}.")
    if kept:
        out(f"Kept from changes saved while you answered: {', '.join(kept)}")
    if api_key:
        out("")
        show_api_key(api_key, key_name, path, out)

    out("")
    out("The Pi needs an SSH key the PC trusts, so it can put the PC to sleep.")
    out("The PC needs a password and its SSH server turned on first (steps 1 and 2 of")
    out(f"{DOCS_URL}/preparing-the-pc.md).")
    ssh_ok = None
    if prompt("Set up the SSH key now? (y/n)", "y", lambda a: a.lower() in ("y", "n")).lower() == "y":
        service = service or couchlink_service
        if waker is None:
            mac_addr = mac.lower().replace("-", ":")
            broadcast, port = current.get("wol_broadcast", "255.255.255.255"), current.get("wol_port", 9)
            waker = lambda h, o: wake_pc(h, lambda: send_magic_packet(mac_addr, broadcast, port), o)
        paused = False
        awake = pc_check(host)
        if not awake:
            out(f"The PC at {host} doesn't answer. It may be asleep or off.")
            if prompt("Wake it with Wake-on-LAN? (y/n)", "y", lambda a: a.lower() in ("y", "n")).lower() == "y":
                paused = service("stop")
                if paused:
                    out("Paused couchlink.service, so it doesn't put the PC back to sleep.")
                awake = waker(host, out)
        if awake:
            ssh_ok = ssh_setup(host, user, out)
        else:
            out("The SSH key needs the PC to be on. Turn it on, then run: sudo bash install.sh --setup")
            ssh_ok = False
        if paused:
            service("start")
            out("Started couchlink.service again.")
    else:
        out("Skipped. Run sudo bash install.sh --setup again later to do it.")

    settings, _ = load_config(path)
    _, problems = validate(settings)
    out("")
    if problems:
        out("Still to do:")
        for p in problems:
            out(f"  {p}")
        out("Find the CEC addresses with: sudo couchlink --setup-cec")
    if ssh_ok is False:
        out("SSH to the PC doesn't work yet, so CouchLink can't put the PC to sleep.")
        out("Fix that, then run: sudo bash install.sh --setup")
    elif not problems:
        out("The config is complete. Check it with: sudo couchlink --check-config")
    return 0
