# SPDX-License-Identifier: GPL-3.0-or-later
"""The couchlink command."""

import argparse
import os
import queue
import sys
import threading
import time

from . import VERSION
from .actions import DryRunActions, RealActions
from .cec import CecClient, NoCec
from . import common
from .auth import make_setup_code, read_setup_code
from .common import DEFAULT_CONFIG_PATH, EX_CONFIG, LOG_RING, log
from .config import load_config, migrate_config, validate
from .controller import Controller
from .power import pinger, power_watcher
from .setup_cec import run_cec_setup
from .setup_pc import new_api_key, run_setup, set_admin_password, setup_ssh_only
from .web import start_dashboard


def restart(why, cec):
    """Start CouchLink again in this process, with the same command line, to use new settings.
    Works the same under systemd and in a terminal."""
    log("STARTUP", f"Restarting, because {why}")
    cec.stop()      # free the CEC adapter for the new CouchLink
    sys.stdout.flush()
    os.execv(sys.executable, sys.orig_argv)


class SetupMode:
    """Stands in for the controller while the config isn't complete. Only the settings page
    runs, so the setup can be finished in a browser."""
    setup_mode = True

    def __init__(self, cfg, problems):
        self.cfg = cfg
        self.problems = problems

    def snapshot(self):
        return {"version": VERSION, "setup_needed": True, "problems": self.problems,
                "recent_log": list(LOG_RING)[-40:]}


def show_setup_code(path):
    """sudo couchlink --setup-code"""
    settings, _ = load_config(path)
    if settings is None:
        print(f"{path} not found. Run the installer first.")
        return 1
    if settings.get("admin_password_hash"):
        print("The settings page already has a password. To change it: sudo couchlink --admin-password")
        return 0
    try:
        code = make_setup_code(path)
    except PermissionError:
        print("Run it with sudo: sudo couchlink --setup-code")
        return 1
    print(f"Setup code: {code}")
    print("Open http://<pi>:8080/admin, enter the code and choose a password.")
    return 0


def run_setup_mode(cfg, problems, args):
    """Run only the settings page (and cec-client, to find the inputs) until the setup is done."""
    log("STARTUP", f"CouchLink {VERSION} is waiting to be set up. Open the settings page to finish it", level="WARN")
    for p in problems:
        log("CONFIG", p, level="WARN")
    if not cfg.admin_password_hash and not read_setup_code(args.config):
        try:
            make_setup_code(args.config)
        except OSError as e:
            log("STARTUP", f"Can't write the setup code: {e}", level="ERROR")
    if not cfg.admin_password_hash:
        log("STARTUP", "To set the settings page's password, see the setup code with: sudo couchlink --setup-code")
    events = queue.Queue()
    cec = NoCec()
    if cfg.use_tv:
        try:
            cec = CecClient(cfg.cec_client_command, events, False)
        except FileNotFoundError:
            log("STARTUP", f"{cfg.cec_client_command[0]} is not installed, so the HDMI inputs can't be found "
                           "(sudo apt install cec-utils)", level="WARN")
    if not start_dashboard(cfg.dashboard_port, SetupMode(cfg, problems), events, args.config, cec):
        sys.exit(EX_CONFIG)
    while True:
        event = events.get()
        if event[0] == "restart":
            restart(event[1], cec)
        if event[0] == "cec_exit":
            log("STARTUP", f"cec-client exited with code {event[1]}. Exiting so systemd restarts CouchLink",
                level="ERROR")
            sys.exit(1)


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
    ap.add_argument("--setup-cec", action="store_true",
                    help="find the CEC addresses with the TV on and save them")
    ap.add_argument("--new-api-key", nargs="?", const="default", metavar="NAME",
                    help="generate a key for scripts to use the API, such as --new-api-key home-assistant. "
                         "A key with the same name is replaced")
    ap.add_argument("--admin-password", action="store_true", help="set the settings page's password")
    ap.add_argument("--setup-code", action="store_true",
                    help="show the one-time code that sets the settings page's first password")
    ap.add_argument("--setup-ssh", action="store_true",
                    help="give the Pi an SSH key the PC trusts, so it can put the PC to sleep")
    args = ap.parse_args(argv)
    if args.setup_code:
        sys.exit(show_setup_code(args.config))
    if args.setup_ssh:
        if os.geteuid() != 0:
            print("Run it with sudo: sudo couchlink --setup-ssh")
            sys.exit(1)
        sys.exit(setup_ssh_only(args.config))
    if args.new_api_key:
        sys.exit(new_api_key(args.config, args.new_api_key))
    if args.admin_password:
        sys.exit(set_admin_password(args.config))
    if args.setup_cec:
        if os.geteuid() != 0:
            print("Run it with sudo: sudo couchlink --setup-cec")
            sys.exit(1)
        sys.exit(run_cec_setup(args.config))
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
        # A config that is only incomplete can be finished on the settings page. Anything
        # else, or a check from the command line, stops here as before.
        unknown = any(p.startswith("Unknown setting") for p in problems)
        if cfg is not None and cfg.dashboard_port and not args.check_config and not unknown:
            run_setup_mode(cfg, problems, args)
        for p in problems:
            log("CONFIG", p, level="ERROR")
        sys.exit(EX_CONFIG)
    common.SHOW_DEBUG = cfg.raw_debug
    if args.check_config:
        for k, v in sorted(vars(cfg).items()):
            print(f"{k} = {v!r}")
        print("Config OK")
        return

    events = queue.Queue()
    try:
        cec = CecClient(cfg.cec_client_command, events, cfg.raw_debug) if cfg.use_tv else NoCec()
    except FileNotFoundError:
        log("STARTUP", f"{cfg.cec_client_command[0]} is not installed (sudo apt install cec-utils)",
            level="ERROR")
        sys.exit(EX_CONFIG)
    actions = (DryRunActions if args.dry_run else RealActions)(cfg, cec, events)
    ctl = Controller(cfg, actions)
    if cfg.use_tv:
        log("STARTUP", f"CouchLink {VERSION}{' DRY RUN' if args.dry_run else ''} starting. "
                       f"PC input: {', '.join(ctl.input_name(a) for a in cfg.pc_addresses)}, "
                       f"Pi: {ctl.input_name(cfg.pi_address)}, PC address: {cfg.pc_host}")
    else:
        log("STARTUP", f"CouchLink {VERSION}{' DRY RUN' if args.dry_run else ''} starting without a TV "
                       f"(use_tv = false). PC address: {cfg.pc_host}")
    threading.Thread(target=pinger, args=(cfg, events), daemon=True).start()
    if cfg.watch_pc_power:
        threading.Thread(target=power_watcher, args=(cfg, events), daemon=True).start()
    if cfg.dashboard_port:
        start_dashboard(cfg.dashboard_port, ctl, events, args.config, cec)

    last_tick = 0.0
    while True:
        try:
            event = events.get(timeout=cfg.tick_interval)
        except queue.Empty:
            event = None
        if event is not None:
            if event[0] == "restart":
                restart(event[1], cec)
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
