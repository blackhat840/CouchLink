# SPDX-License-Identifier: GPL-3.0-or-later
"""sudo couchlink --setup: the settings that don't need CEC addresses, the SSH
key, and --new-api-key."""

import os
import re
import secrets
import socket
import subprocess
import time

from .actions import send_magic_packet
from .auth import hash_api_key
from .common import DOCS_URL
from .config import DEFAULTS, load_config, set_config_value, validate
from .setup_cec import couchlink_service


PLACEHOLDERS = {"target-pc.local", "aa:bb:cc:dd:ee:ff"}


MAC_RE = re.compile(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", re.IGNORECASE)


def show_api_key(key, path, out):
    out(f"New API key: {key}")
    out("")
    out(f"Copy it now. Only a hash of it is saved in {path}, so it can't be shown again.")
    out("Generating another key replaces it, and the old one stops working.")
    out("Scripts send it as:")
    out("  Authorization: Bearer <key>")


def new_api_key(path, out=print):
    """Generate a new API key, save its hash to the config and show the key once."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        out(f"{path} not found. Run the installer first.")
        return 1
    key = secrets.token_urlsafe(24)
    try:
        with open(path, "w") as f:
            f.write(set_config_value(text, "api_key_hash", hash_api_key(key)))
    except PermissionError:
        out(f"Can't write {path}. Run it with sudo: sudo couchlink --new-api-key")
        return 1
    show_api_key(key, path, out)
    out("Restart CouchLink to use it: sudo systemctl restart couchlink")
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


def run_setup(path, ask=input, out=print, mac_finder=find_mac, ssh_setup=setup_ssh_key,
              pc_check=pc_answers, waker=None, service=None):
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

    out("")
    out("The status page can show buttons to wake the PC and put it to sleep. They need no key,")
    out("so anyone who can open the page can use them.")
    buttons = prompt("Show the buttons? (y/n)", "y" if current.get("status_page_buttons", True) else "n",
                     lambda a: a.lower() in ("y", "n"), "Enter y or n.").lower() == "y"

    out("")
    out("Scripts, phone shortcuts and Home Assistant can wake the PC and put it to sleep through")
    out("CouchLink's API. They need an API key. The status page's buttons don't.")
    question = ("An API key already exists. Generate a new one? The old one stops working. (y/n)"
                if current.get("api_key_hash") else "Generate an API key now? (y/n)")
    api_key = None
    if prompt(question, "n", lambda a: a.lower() in ("y", "n"), "Enter y or n.").lower() == "y":
        api_key = secrets.token_urlsafe(24)

    original = text
    for key, value in (("pc_host", host), ("pc_mac", mac.lower().replace("-", ":")),
                       ("ssh_user", user), ("pc_name", name or None),
                       ("auto_switch_on_pc_wake", wake_mode), ("on_pc_sleep", sleep_mode),
                       ("status_page_buttons", buttons),
                       ("api_key_hash", hash_api_key(api_key) if api_key else None)):
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
    if api_key:
        out("")
        show_api_key(api_key, path, out)

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
