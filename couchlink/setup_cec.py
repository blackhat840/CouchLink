# SPDX-License-Identifier: GPL-3.0-or-later
"""sudo couchlink --setup-cec: finding the CEC addresses with the TV on."""

import subprocess

from .cec import cec_scan, cec_watch, dotted
from .config import DEFAULTS, load_config, save_answers, set_config_value, unset_config_value, validate


def couchlink_service(action):
    """Stop or start couchlink.service. stop returns whether it was running."""
    try:
        if action == "stop":
            active = subprocess.run(["systemctl", "is-active", "--quiet", "couchlink.service"]).returncode == 0
            if active:
                subprocess.run(["systemctl", "stop", "couchlink.service"], check=False)
            return active
        subprocess.run(["systemctl", "start", "couchlink.service"], check=False)
    except OSError:
        pass
    return False


def run_cec_setup(path, ask=input, out=print, scan=cec_scan, watch=cec_watch, service=couchlink_service):
    """Find the CEC addresses with the user and write them to the config."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        out(f"{path} not found. Run the installer first.")
        return 1
    current, _ = load_config(path)
    current = current or dict(DEFAULTS)
    command = current["cec_client_command"]
    pi_la = current["pi_logical_address"]

    def prompt(question, default, check, error):
        while True:
            hint = f" [{default}]" if default else ""
            answer = ask(f"{question}{hint}: ").strip() or (default or "")
            if answer and check(answer):
                return answer
            out(error)

    def yes(question, default="y"):
        return prompt(question, default, lambda a: a.lower() in ("y", "n"), "Enter y or n.").lower() == "y"

    def number(question):
        return int(prompt(question, None, lambda a: a.isdigit() and 1 <= int(a) <= 9, "Enter a number from 1 to 9."))

    out("CouchLink CEC setup. This finds the HDMI addresses CouchLink needs.")
    out("Before you start, turn the TV on, and plug in and turn on the PC.")
    out("")
    out("How is the PC connected?")
    out("  1) To the TV, with no soundbar or AV receiver")
    out("  2) To the TV, with a soundbar or AV receiver on the TV's ARC or eARC port")
    out("  3) To a soundbar or AV receiver, which is connected to the TV")
    layout = prompt("Choose 1, 2 or 3", None, lambda a: a in ("1", "2", "3"), "Enter 1, 2 or 3.")
    has_bar, pc_behind_bar = layout in ("2", "3"), layout == "3"

    was_running = service("stop")
    if was_running:
        out("")
        out("Stopped couchlink.service while this uses the CEC adapter. It starts again at the end.")
    try:
        out("")
        out("Asking every HDMI device who it is. This takes up to a minute.")
        devices = scan(command)
        if not devices:
            out("No CEC devices answered. Check that the TV is on, that the Pi is plugged into one of")
            out("its HDMI inputs, and that CEC is turned on in the TV's settings. TVs call it Anynet+,")
            out("Bravia Sync, SimpLink, CEC or HDMI control.")
            return 1
        me = next((d for d in devices if d["logical"] == pi_la), None)
        out("Found:")
        for d in devices:
            label = "this Pi" if d is me else d["osd"] or d["vendor"]
            extra = f", {label}" if label and label.lower() != d["name"].lower() else ""
            out(f"  {dotted(d['address']):<9} {d['name']}{extra}")

        out("")
        if me and me["address"] != "00:00":
            pi = me["address"]
            out(f"The Pi is on TV input {pi[0]} ({dotted(pi)}).")
        else:
            out("The Pi did not show up in the scan.")
            pi = f"{number('Which TV input is the Pi plugged into? (1-9)')}0:00"

        bar = None
        if has_bar:
            audio = next((d for d in devices if d["logical"] == 5), None)
            if audio and yes(f"Is {audio['osd'] or audio['vendor'] or 'the audio device'} on "
                             f"{dotted(audio['address'])} your soundbar or receiver? (y/n)"):
                bar = audio["address"]
            else:
                bar = f"{number('Which TV input is the soundbar or receiver plugged into? (1-9)')}0:00"

        known = {d["address"] for d in devices} | {pi, "00:00"} | ({bar} if bar else set())
        out("")
        out("Now find the PC's input. CouchLink listens while you switch to it.")
        out("Use the TV's remote" + (", or the soundbar's or receiver's remote," if has_bar else "") +
            " to switch to the PC so its picture is on screen.")
        out("If the PC is already on screen, switch to another input first, then back to the PC.")
        pc = watch(command, 60, lambda a: a not in known, out)
        if pc:
            out(f"Heard a switch to {dotted(pc)}.")
            if not yes("Is that the PC's input? (y/n)"):
                pc = None
        else:
            out("Did not hear a switch to a new input.")
        if not pc:
            if pc_behind_bar:
                pc = f"{bar[0]}{number('Which input on the soundbar or receiver is the PC plugged into? (1-9)')}:00"
            else:
                pc = f"{number('Which TV input is the PC plugged into? (1-9)')}0:00"
        if pc in (pi, bar):
            out(f"{dotted(pc)} is the {'Pi' if pc == pi else 'soundbar or receiver'}, not the PC. "
                "Run this again to try once more.")
            return 1

        out("")
        out("When the PC goes to sleep by itself, CouchLink can switch the TV to another input,")
        out("such as a streaming box.")
        switch_to = None
        declined = not yes("Do that? (y/n)", "y" if current.get("on_pc_sleep") == "switch" else "n")
        if not declined:
            others = [d for d in devices if d["address"] not in (pi, bar, pc, "00:00")]
            for i, d in enumerate(others, 1):
                out(f"  {i}) {d['osd'] or d['name']} on {dotted(d['address'])}")
            listen = len(others) + 1
            out(f"  {listen}) another input: switch to it now and CouchLink listens")
            pick = int(prompt(f"Choose 1-{listen}", None, lambda a: a.isdigit() and 1 <= int(a) <= listen,
                              f"Enter a number from 1 to {listen}."))
            if pick < listen:
                switch_to = others[pick - 1]["address"]
            else:
                out("Switch to the input to use when the PC sleeps.")
                switch_to = watch(command, 60, lambda a: a not in (pi, pc, "00:00"), out)
                if switch_to:
                    out(f"Heard a switch to {dotted(switch_to)}.")
                else:
                    out("Did not hear a switch. Keeping the current setting for when the PC sleeps.")
    finally:
        if was_running:
            service("start")
            out("Started couchlink.service again.")

    answered = {"pc_addresses", "pi_address", "soundbar_address"}
    if current.get("use_tv") is False:
        answered.add("use_tv")
    if switch_to:
        answered |= {"on_pc_sleep", "pc_sleep_switch_to"}
    elif declined and current.get("on_pc_sleep") == "switch":
        answered.add("on_pc_sleep")

    def answer(config):
        config = set_config_value(config, "pc_addresses", [pc])
        if current.get("use_tv") is False:
            config = set_config_value(config, "use_tv", True)       # finding the addresses means there is a TV
        config = set_config_value(config, "pi_address", pi)
        config = (set_config_value(config, "soundbar_address", bar) if bar
                  else unset_config_value(config, "soundbar_address"))
        if switch_to:
            config = set_config_value(config, "on_pc_sleep", "switch")
            config = set_config_value(config, "pc_sleep_switch_to", switch_to)
        elif declined and current.get("on_pc_sleep") == "switch":
            config = set_config_value(config, "on_pc_sleep", "nothing")
        return config

    try:
        saved, kept, problem = save_answers(path, text, answer, answered)
    except PermissionError:
        out(f"Can't write {path}. Run it with sudo: sudo couchlink --setup-cec")
        return 1
    if problem:
        out(f"Nothing was saved: {problem}. Run sudo bash install.sh --setup-cec again.")
        return 1

    out("")
    out(f"Saved to {path}. The previous version is in {path}.bak." if saved else f"Nothing changed in {path}.")
    if kept:
        out(f"Kept from changes saved while you answered: {', '.join(kept)}")
    out(f"  pc_addresses = [\"{pc}\"]      the PC ({dotted(pc)})")
    out(f"  pi_address = \"{pi}\"          the Pi ({dotted(pi)})")
    if bar:
        out(f"  soundbar_address = \"{bar}\"    the soundbar or receiver ({dotted(bar)})")
    if switch_to:
        out(f"  pc_sleep_switch_to = \"{switch_to}\"  when the PC sleeps ({dotted(switch_to)})")
    settings, _ = load_config(path)
    _, problems = validate(settings)
    out("")
    if problems:
        out("Still to do:")
        for p in problems:
            out(f"  {p}")
        return 0
    out("The config is complete. Try it without changing anything (Ctrl+C stops it):")
    out("  sudo couchlink --dry-run")
    return 0
