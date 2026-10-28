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
from .cec import CecClient
from . import common
from .common import DEFAULT_CONFIG_PATH, EX_CONFIG, log
from .config import load_config, migrate_config, validate
from .controller import Controller
from .power import pinger, power_watcher
from .setup_cec import run_cec_setup
from .setup_pc import new_api_key, run_setup
from .web import start_dashboard


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
    ap.add_argument("--new-api-key", action="store_true",
                    help="generate a key for scripts to use the API, replacing any old one")
    args = ap.parse_args(argv)
    if args.new_api_key:
        sys.exit(new_api_key(args.config))
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
        start_dashboard(cfg.dashboard_port, ctl, events)

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
