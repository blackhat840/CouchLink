#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Offline tests for couchlink.py. No TV, Pi or PC needed: a simulated clock and
fake actions stand in for the hardware. Run from the folder that holds both files:

    python3 test_couchlink.py
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("couchlink", os.path.join(HERE, "couchlink.py"))
couchlink = importlib.util.module_from_spec(spec)
spec.loader.exec_module(couchlink)
couchlink.SHOW_DEBUG = False

# Layout used in these tests (matches the planned setup):
#   TV HDMI 3 = PC (video)          30:00
#   TV HDMI 4 = Raspberry Pi        40:00
#   TV HDMI 2 = soundbar (ARC)      20:00
#     soundbar In 1 = streamer      21:00
#     soundbar In 2 = PC audio      22:00
BASE = dict(couchlink.DEFAULTS)
BASE.update(pc_host="192.168.1.50", pc_mac="aa:bb:cc:dd:ee:01",
            pc_addresses=["30:00"], pi_address="40:00",
            soundbar_address="20:00", soundbar_pc_audio_address="22:00",
            hid_passthrough=True, raw_debug=False)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeActions:
    dry_run = False

    def __init__(self):
        self.cec, self.wols, self.suspends, self.keys = [], 0, 0, []
        self.name_lookups = 0

    def cec_send(self, c):
        self.cec.append(c)

    def wol(self):
        self.wols += 1

    def suspend(self):
        self.suspends += 1

    def key(self, code, name):
        self.keys.append(name)

    def fetch_pc_name(self):
        self.name_lookups += 1


def msg(text):
    return ("cec", couchlink.parse_rx(f"TRAFFIC: [ 1]\t>> {text}"))


def out(text):
    """A message the Pi sent, as cec-client logs it."""
    return ("cec_out", couchlink.parse_tx(f"TRAFFIC: [ 1]\t<< {text}"))


class Sim:
    def __init__(self, **overrides):
        s = dict(BASE)
        s.update(overrides)
        self.cfg, problems = couchlink.validate(s)
        assert not problems, problems
        self.clock = Clock()
        self.act = FakeActions()
        self.ctl = couchlink.Controller(self.cfg, self.act, clock=self.clock)

    def ev(self, event):
        self.ctl.handle(event)
        self.ctl.tick()

    def run(self, seconds, step=1.0):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            self.ctl.tick()

    def pc(self, online):
        self.ev(("ping", online))


class ConfigTests(unittest.TestCase):
    def test_defaults_rejected(self):
        _, problems = couchlink.validate(dict(couchlink.DEFAULTS))
        text = " ".join(problems)
        for key in ("pc_host", "pc_mac", "pc_addresses", "pi_address"):
            self.assertIn(key, text)

    def test_address_formats(self):
        self.assertEqual(couchlink.normalize_addr("3.0.0.0"), "30:00")
        self.assertEqual(couchlink.normalize_addr("30:00"), "30:00")
        self.assertEqual(couchlink.normalize_addr("2200"), "22:00")
        self.assertIsNone(couchlink.normalize_addr("3.0"))

    def test_overlap_rejected(self):
        s = dict(BASE, soundbar_address="30:00")
        _, problems = couchlink.validate(s)
        self.assertTrue(any("also listed" in p for p in problems))

    def test_toml_loading(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[pc]\npc_host = "10.0.0.5"\npc_mac = "aa:bb:cc:dd:ee:02"\n'
                    '[cec]\npc_addresses = ["3.0.0.0"]\npi_address = "40:00"\ntypo_key = 1\n')
        settings, problems = couchlink.load_config(f.name)
        os.unlink(f.name)
        self.assertTrue(any("typo_key" in p for p in problems))
        cfg, more = couchlink.validate(settings)
        self.assertEqual(more, [])
        self.assertEqual(cfg.pc_addresses, ["30:00"])

    def test_example_config_is_valid_once_filled(self):
        path = os.path.join(HERE, "couchlink.example.toml")
        if not os.path.exists(path):
            self.skipTest("example config not present")
        settings, problems = couchlink.load_config(path)
        self.assertEqual(problems, [])
        _, problems = couchlink.validate(settings)
        self.assertTrue(problems, "example ships with placeholders and should not validate as-is")


class FlowTests(unittest.TestCase):
    def test_switch_to_pc_wakes_and_claims(self):
        s = Sim()
        s.pc(False)
        s.ev(msg("0f:86:30:00"))
        self.assertEqual(s.act.wols, 1)
        self.assertIn("tx 1F:82:30:00", s.act.cec)       # answered for the PC's input
        s.pc(True)
        s.run(30)
        self.assertEqual(s.act.wols, 1)
        self.assertEqual(s.act.suspends, 0)

    def test_wol_retried_then_gives_up(self):
        s = Sim(max_wol_attempts=3, wol_retry_interval=10)
        s.pc(False)
        s.ev(msg("0f:86:30:00"))
        s.run(60)
        self.assertEqual(s.act.wols, 3)
        self.assertTrue(s.ctl.wake_gave_up)

    def test_routing_change_leave_then_suspend_after_debounce(self):
        s = Sim()
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.run(20)                                       # past the guard window
        s.ev(msg("0f:80:30:00:00:00"))                  # to built-in apps
        s.run(3)
        self.assertEqual(s.act.suspends, 0)             # still debouncing
        s.run(4)
        self.assertEqual(s.act.suspends, 1)

    def test_brief_pass_through_does_not_suspend(self):
        s = Sim()
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.run(20)
        s.ev(msg("0f:86:00:00"))
        s.run(2)
        s.ev(msg("0f:86:30:00"))
        s.run(30)
        self.assertEqual(s.act.suspends, 0)

    def test_suspend_retries_until_pc_drops(self):
        s = Sim(suspend_retry_interval=20, max_suspend_attempts=4)
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.run(20)
        s.ev(msg("0f:86:00:00"))
        s.run(6)
        self.assertEqual(s.act.suspends, 1)
        s.ev(("suspend_done", 255, "connection closed"))
        s.run(21)
        self.assertEqual(s.act.suspends, 2)             # still pinging -> retried
        s.ev(("suspend_done", 0, ""))
        s.pc(False)
        s.run(60)
        self.assertEqual(s.act.suspends, 2)

    def test_suspend_gives_up(self):
        s = Sim(max_suspend_attempts=2, suspend_retry_interval=5)
        s.pc(True)
        s.ev(msg("0f:86:00:00"))
        for _ in range(6):
            s.run(6)
            s.ev(("suspend_done", 1, "sudo: a password is required"))
        self.assertEqual(s.act.suspends, 2)
        self.assertTrue(s.ctl.sleep_gave_up)

    def test_no_overlapping_suspends(self):
        s = Sim(suspend_retry_interval=5)
        s.pc(True)
        s.ev(msg("0f:86:00:00"))
        s.run(40)                                       # ssh never returns
        self.assertEqual(s.act.suspends, 1)

    def test_soundbar_standby_verified(self):
        s = Sim()
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.run(20)
        s.ev(msg("5f:36"))
        s.run(10)
        self.assertEqual(s.act.cec.count("pow 0") >= 2, True)
        s.ev(msg("01:90:00"))                           # TV says: on
        s.run(10)
        self.assertEqual(s.act.suspends, 0)
        s.ev(msg("01:90:01"))                           # TV says: standby
        s.run(6)
        self.assertEqual(s.act.suspends, 1)

    def test_tv_standby(self):
        s = Sim()
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.run(20)
        s.ev(msg("0f:36"))
        s.run(6)
        self.assertEqual(s.act.suspends, 1)

    def test_tv_on_after_standby_makes_input_unknown(self):
        s = Sim()
        s.pc(False)
        s.ev(msg("0f:36"))
        s.ev(msg("01:90:00"))
        self.assertIsNone(s.ctl.tv_input)
        s.pc(True)                                      # PC woken by hand: not suspended blindly
        s.run(1)
        # PC came online by itself -> auto switch makes the PC the input
        self.assertEqual(s.ctl.tv_input, "30:00")

    def test_guard_undoes_soundbar_autoswitch_after_wake(self):
        s = Sim()
        s.pc(False)
        s.ev(msg("0f:86:30:00"))                        # user picks the PC
        s.run(5)
        s.pc(True)                                      # PC wakes, its HDMI audio output appears
        s.ev(msg("0f:80:30:00:20:00"))                  # TV follows the soundbar
        s.run(3)
        self.assertEqual(s.ctl.tv_input, "30:00")
        self.assertEqual(s.act.cec.count("tx 1F:82:30:00"), 2)  # claim + pull back
        s.run(30)
        self.assertEqual(s.act.suspends, 0)

    def test_guard_limited_then_real_switch_counts(self):
        s = Sim(max_guard_recoveries=1)
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.ev(msg("5f:82:22:00"))                        # recovered
        s.ev(msg("5f:82:22:00"))                        # limit reached: counts as real
        self.assertEqual(s.ctl.tv_input, "22:00")

    def test_switch_to_streamer_after_guard_window_suspends(self):
        s = Sim()
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.run(30)
        s.ev(msg("0f:86:20:00"))                        # TV to the soundbar input
        s.ev(msg("4f:82:21:00"))                        # streamer claims active source
        s.run(6)
        self.assertEqual(s.ctl.tv_input, "21:00")
        self.assertEqual(s.act.suspends, 1)

    def test_streamer_not_a_guard_suspect(self):
        s = Sim()
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.ev(msg("4f:82:21:00"))                        # within the window, but the streamer
        self.assertEqual(s.ctl.tv_input, "21:00")       # is not a suspect

    def test_pc_wakes_by_itself_turns_tv_on(self):
        s = Sim()
        s.pc(False)
        s.ev(msg("0f:36"))
        s.run(10)
        s.pc(True)
        s.run(2)
        self.assertIn("on 0", s.act.cec)
        self.assertNotIn("tx 1F:82:30:00", s.act.cec)   # waits for the TV to start
        s.ev(msg("01:90:00"))                           # TV says: on
        self.assertIn("tx 1F:82:30:00", s.act.cec)
        self.assertEqual(s.ctl.tv_input, "30:00")
        s.run(30)
        self.assertEqual(s.act.suspends, 0)

    def test_pc_wake_if_tv_on_mode_leaves_tv_off(self):
        s = Sim(auto_switch_on_pc_wake="if_tv_on")
        s.pc(False)
        s.ev(msg("0f:36"))
        s.pc(True)
        s.run(6)
        self.assertNotIn("on 0", s.act.cec)
        self.assertEqual(s.act.suspends, 1)

    def test_our_wol_then_user_leaves_no_autoswitch(self):
        s = Sim()
        s.pc(False)
        s.ev(msg("0f:86:30:00"))
        s.run(25)                                       # past guard
        s.ev(msg("0f:86:00:00"))                        # user leaves before PC is up
        s.run(2)
        s.pc(True)                                      # PC finishes waking from our WoL
        s.run(6)
        self.assertNotIn("on 0", s.act.cec)
        self.assertEqual(s.act.suspends, 1)

    def test_ignore_list(self):
        s = Sim(ignore_announcements_from=[5])
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        s.run(30)
        s.ev(msg("5f:82:22:00"))
        self.assertEqual(s.ctl.tv_input, "30:00")

    def test_startup_with_pc_on_asserts_pc(self):
        s = Sim()
        s.pc(True)
        s.run(7)
        self.assertEqual(s.ctl.tv_input, "30:00")
        self.assertIn("tx 1F:82:30:00", s.act.cec)

    def test_startup_with_tv_standby_suspends(self):
        s = Sim()
        s.pc(True)
        s.run(3)
        s.ev(msg("01:90:01"))
        s.run(10)
        self.assertEqual(s.ctl.tv_input, "standby")
        self.assertEqual(s.act.suspends, 1)

    def test_power_poll(self):
        s = Sim(power_poll_interval=60)
        s.run(200)
        self.assertGreaterEqual(s.act.cec.count("pow 0"), 3)

    def test_request_active_source_answered_for_pc(self):
        s = Sim()
        s.pc(True)
        s.ev(msg("0f:86:30:00"))
        before = s.act.cec.count("tx 1F:82:30:00")
        s.ev(msg("0f:85"))
        s.run(1)
        self.assertEqual(s.act.cec.count("tx 1F:82:30:00"), before + 1)

    def test_soundbar_audio_select(self):
        s = Sim(select_soundbar_audio=True)
        s.pc(False)
        s.ev(msg("0f:86:30:00"))
        s.pc(True)
        s.run(3)
        self.assertIn("tx 15:70:22:00", s.act.cec)

    def test_keys_only_on_pc_input(self):
        s = Sim()
        s.pc(True)
        s.ev(("key", "select"))
        self.assertEqual(s.act.keys, [])
        s.ev(msg("0f:86:30:00"))
        s.ev(("key", "select"))
        s.ev(("key", "nonsense"))
        self.assertEqual(s.act.keys, ["select"])

    def test_own_transmissions_and_junk_ignored(self):
        self.assertIsNone(couchlink.parse_rx("TRAFFIC: [ 1]\t<< 1f:82:30:00"))
        self.assertIsNone(couchlink.parse_tx("TRAFFIC: [ 1]\t>> 0f:86:30:00"))
        self.assertEqual(couchlink.parse_tx("TRAFFIC: [ 1]\t<< 1f:82:40:00").params, ["40", "00"])
        self.assertIsNone(couchlink.parse_rx("TRAFFIC: [ 1]\t>> 01"))
        self.assertIsNone(couchlink.parse_rx("NOTICE: connected"))
        self.assertEqual(couchlink.parse_key("DEBUG: key pressed: fast forward (49)"), "fast forward")

    def test_version_matches_docstring(self):
        self.assertIn(f"CouchLink {couchlink.VERSION}\n", couchlink.__doc__)

    def test_status_page_urls(self):
        self.assertEqual(couchlink.status_page_urls(8080, "192.168.1.20", "couchlink"),
                         ["http://192.168.1.20:8080", "http://couchlink.local:8080"])
        self.assertEqual(couchlink.status_page_urls(8080, None, "couchlink"), ["http://couchlink.local:8080"])
        self.assertEqual(couchlink.status_page_urls(8080, None, ""), ["http://<pi-address>:8080"])

    def test_own_address(self):
        self.assertEqual(couchlink.own_address("127.0.0.1"), "127.0.0.1")
        self.assertIsNone(couchlink.own_address("no-such-host.invalid"))

    def test_status_page_links_to_repo(self):
        self.assertIn('href="https://github.com/blackhat840/CouchLink"', couchlink.DASHBOARD_HTML)

    def test_input_names(self):
        s = Sim()
        n = s.ctl.input_name
        self.assertEqual(n("30:00"), "HDMI 3 (PC)")
        self.assertEqual(n("22:00"), "HDMI 2.2 (soundbar, PC audio)")
        self.assertEqual(n("21:00"), "HDMI 2.1")
        self.assertEqual(n("20:00"), "HDMI 2 (soundbar)")
        self.assertEqual(n("00:00"), "TV / built-in apps")


# The tested layout (from the scan and trace):
#   TV HDMI 1 (ARC) = soundbar 10:00; soundbar In 1 = streamer 11:00; In 2 = PC 12:00
#   TV HDMI 4 = Pi 40:00
REAL = dict(pc_addresses=["12:00"], pi_address="40:00", soundbar_address="10:00",
            soundbar_pc_audio_address=None, on_pc_sleep="switch", pc_sleep_switch_to="11:00")


class PcSleepTests(unittest.TestCase):
    def test_pc_sleeps_on_screen_switches_to_streamer(self):
        s = Sim(**REAL)
        s.pc(True)
        s.ev(msg("5f:81:12:00"))                        # soundbar remote -> HDMI 2 (PC)
        s.run(30)
        s.pc(False)                                     # PC put to sleep from the Steam menu
        s.run(5)
        self.assertEqual(s.act.wols, 0)                 # not woken again
        self.assertNotIn("tx 1F:82:11:00", s.act.cec)   # still confirming
        s.run(10)
        self.assertIn("tx 1F:82:11:00", s.act.cec)      # switched to the streamer
        self.assertEqual(s.ctl.tv_input, "11:00")
        s.ev(msg("5f:81:11:00"))                        # soundbar confirms
        s.ev(msg("4f:82:11:00"))                        # streamer claims active source
        s.run(30)
        self.assertEqual(s.act.wols, 0)
        self.assertEqual(s.act.suspends, 0)             # PC already asleep

    def test_network_blip_does_not_switch(self):
        s = Sim(**REAL)
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.pc(False)
        s.run(6)
        s.pc(True)                                      # back before the confirm time
        s.run(30)
        self.assertNotIn("tx 1F:82:11:00", s.act.cec)
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertEqual(s.act.wols, 0)

    def test_nothing_mode_leaves_tv_and_does_not_rewake(self):
        s = Sim(**dict(REAL, on_pc_sleep="nothing", pc_sleep_switch_to=None))
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.pc(False)
        s.run(120)
        self.assertEqual(s.act.wols, 0)
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertFalse(any(c.startswith("tx 1F:82:11") for c in s.act.cec))

    def test_standby_mode(self):
        s = Sim(**dict(REAL, on_pc_sleep="standby", pc_sleep_switch_to=None))
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.pc(False)
        s.run(15)
        self.assertIn("standby 0", s.act.cec)
        self.assertEqual(s.ctl.tv_input, "standby")

    def test_controller_wake_after_sleep_switches_back_to_pc(self):
        s = Sim(**REAL)
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.pc(False)
        s.run(15)                                       # -> streamer
        s.run(60)
        s.pc(True)                                      # Xbox controller wakes the PC
        s.run(2)
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertEqual(s.act.cec.count("tx 1F:82:12:00") >= 2, True)
        s.run(30)
        self.assertEqual(s.act.suspends, 0)

    def test_still_wakes_a_pc_that_was_never_up(self):
        s = Sim(**REAL)
        s.pc(False)
        s.ev(msg("5f:81:12:00"))                        # pick the PC while it sleeps
        s.run(25)
        self.assertGreaterEqual(s.act.wols, 2)          # WoL retried as before

    def test_reselecting_pc_after_sleep_wakes_it(self):
        s = Sim(**dict(REAL, on_pc_sleep="nothing", pc_sleep_switch_to=None))
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.pc(False)
        s.run(20)
        s.ev(msg("5f:81:11:00"))                        # user goes to the streamer
        s.ev(msg("5f:81:12:00"))                        # and back to the PC
        s.run(1)
        self.assertEqual(s.act.wols, 1)

    def test_switch_needs_target(self):
        cfg = dict(BASE, **REAL)
        cfg["pc_sleep_switch_to"] = None
        _, problems = couchlink.validate(cfg)
        self.assertTrue(any("pc_sleep_switch_to" in p for p in problems))

    def test_trace_replay_streamer_and_tv_off(self):
        """Messages taken from the real trace."""
        s = Sim(**REAL)
        s.pc(True)
        s.ev(msg("0f:80:10:00:11:00"))                  # TV on HDMI 1 -> streamer
        self.assertEqual(s.ctl.tv_input, "11:00")
        s.ev(msg("5f:81:12:00"))                        # soundbar -> HDMI 2
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertIn("tx 1F:82:12:00", s.act.cec)      # claimed for the PC
        s.run(30)
        s.ev(msg("0f:80:11:00:20:00"))                  # TV to HDMI 2
        s.run(6)
        self.assertEqual(s.act.suspends, 1)
        s.ev(("suspend_done", 0, ""))
        s.pc(False)
        s.ev(msg("0f:36"))                              # TV off
        self.assertEqual(s.ctl.tv_input, "standby")


class TvBootTests(unittest.TestCase):
    """The PC wakes by itself while the TV is off (issue 1)."""

    def off_then_controller_wake(self, **overrides):
        s = Sim(**dict(REAL, **overrides))
        s.pc(False)
        s.ev(msg("0f:36"))                              # TV off
        s.run(30)
        s.pc(True)                                      # controller wakes the PC
        return s

    def test_trace_replay_controller_wake_with_tv_off(self):
        """The sequence from issue 1. No wake.log was attached, so the TV's boot
        traffic below is assumed from the issue: a standby report while it starts,
        the route to the streamer on HDMI 1, then a switch to the Pi's own input
        caused by Image View On, with cec-client answering for the Pi."""
        s = self.off_then_controller_wake()
        self.assertIn("on 0", s.act.cec)
        self.assertEqual(s.ctl.tv_input, "12:00")
        s.run(2)
        self.assertNotIn("tx 1F:82:12:00", s.act.cec)   # not claimed while the TV starts
        self.assertGreaterEqual(s.act.cec.count("pow 0"), 1)
        s.ev(msg("01:90:01"))                           # still in standby while starting
        s.ev(msg("0f:80:10:00:11:00"))                  # TV boots on HDMI 1 -> streamer
        s.run(2)
        s.ev(msg("01:90:02"))                           # turning on
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertNotIn("tx 1F:82:12:00", s.act.cec)
        s.ev(msg("01:90:00"))                           # TV is on
        self.assertEqual(s.act.cec.count("tx 1F:82:12:00"), 1)
        s.ev(msg("0f:86:40:00"))                        # TV jumps to the Pi's input (HDMI 4)
        s.ev(out("1f:82:40:00"))                        # cec-client answers for the Pi
        self.assertEqual(s.ctl.tv_input, "12:00")
        s.run(10)
        # first claim + 3 re-claims + one pull-back for the switch to the Pi
        self.assertEqual(s.act.cec.count("tx 1F:82:12:00"), 5)
        s.ev(msg("0f:80:40:00:10:00"))                  # TV back on HDMI 1
        s.ev(msg("5f:81:12:00"))                        # soundbar wakes on the PC's input
        s.run(60)
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertEqual(s.act.suspends, 0)
        self.assertEqual(s.act.wols, 0)
        claims = s.act.cec.count("tx 1F:82:12:00")
        s.ev(msg("0f:80:10:00:40:00"))                  # late switch to the Pi, guard long over
        s.run(2)
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertEqual(s.act.cec.count("tx 1F:82:12:00"), claims + 1)

    def test_tv_never_reports_on_claims_after_timeout(self):
        s = self.off_then_controller_wake(tv_boot_timeout=10)
        s.run(9)
        self.assertNotIn("tx 1F:82:12:00", s.act.cec)
        self.assertGreaterEqual(s.act.cec.count("pow 0"), 4)   # kept asking
        s.run(2)
        self.assertIn("tx 1F:82:12:00", s.act.cec)
        s.run(10)
        s.ev(msg("01:90:01"))                           # TV really is off
        s.run(6)
        self.assertEqual(s.ctl.tv_input, "standby")
        self.assertEqual(s.act.suspends, 1)

    def test_tv_turned_off_while_starting_cancels(self):
        s = self.off_then_controller_wake()
        s.run(2)
        s.ev(msg("0f:36"))                              # user turns the TV off again
        s.run(30)
        self.assertNotIn("tx 1F:82:12:00", s.act.cec)
        self.assertEqual(s.act.suspends, 1)

    def test_switch_while_settling_pulled_back(self):
        s = self.off_then_controller_wake(tv_boot_reclaims=2, tv_boot_reclaim_interval=3)
        s.ev(msg("01:90:00"))
        s.run(1)
        s.ev(msg("0f:80:10:00:11:00"))                  # streamer route right after power on
        s.ev(msg("4f:82:11:00"))                        # and the streamer claims the TV
        self.assertEqual(s.ctl.tv_input, "12:00")
        s.run(10)
        # first claim + 2 re-claims + one pull-back
        self.assertEqual(s.act.cec.count("tx 1F:82:12:00"), 4)
        s.ev(msg("0f:80:10:00:11:00"))                  # later: a real switch to the streamer
        s.run(30)
        self.assertEqual(s.ctl.tv_input, "11:00")
        self.assertEqual(s.act.suspends, 1)

    def test_tv_already_on_switches_without_waiting(self):
        s = Sim(**REAL)
        s.pc(False)
        s.ev(msg("5f:81:11:00"))                        # TV on, showing the streamer
        s.ev(msg("01:90:00"))
        s.run(30)
        s.pc(True)
        s.run(2)
        self.assertIn("tx 1F:82:12:00", s.act.cec)
        self.assertFalse(s.ctl.snapshot()["tv_starting"])

    def test_pi_input_never_kept_while_pc_awake(self):
        s = Sim(**REAL)
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(60)                                       # guard window long over
        s.ev(msg("0f:80:10:00:40:00"))
        s.ev(out("1f:82:40:00"))
        s.ev(msg("0f:86:40:00"))
        self.assertEqual(s.ctl.tv_input, "12:00")
        s.run(3)
        self.assertEqual(s.act.cec.count("tx 1F:82:12:00"), 2)  # claim + one pull-back
        s.run(30)
        self.assertEqual(s.act.suspends, 0)

    def test_pi_input_accepted_while_pc_asleep(self):
        s = Sim(**REAL)
        s.pc(False)
        s.ev(msg("5f:81:11:00"))
        s.run(30)
        s.ev(msg("0f:80:10:00:40:00"))
        s.ev(out("1f:82:40:00"))
        self.assertEqual(s.ctl.tv_input, "40:00")
        self.assertFalse(any(c.startswith("tx 1F:82") for c in s.act.cec))

    def test_boot_settings_validated(self):
        _, problems = couchlink.validate(dict(BASE, tv_boot_timeout=-1, tv_boot_reclaims=1.5))
        text = " ".join(problems)
        self.assertIn("tv_boot_timeout", text)
        self.assertIn("tv_boot_reclaims", text)


def release_archive(tag, files):
    """A .tar.gz shaped like GitHub's source archives: one top folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name if name.startswith("..") else f"CouchLink-{tag.lstrip('v')}/{name}")
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


STUB_INSTALLER = '#!/bin/bash\necho "ran $COUCHLINK_RELEASE_TAG $*" > "$ROOT/stub-ran"\n'


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the installer tests")
class InstallerTests(unittest.TestCase):
    """install.sh against a local stand-in for GitHub. The downloaded installers
    are stubs that record how they were run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.src = os.path.join(self.tmp, "src")           # no test file here, so no recursion
        os.makedirs(self.src)
        for name in ("install.sh", "couchlink.py", "couchlink.example.toml"):
            shutil.copy(os.path.join(HERE, name), self.src)
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(self.root, "usr/local/bin"))
        os.makedirs(os.path.join(self.root, "etc/couchlink"))
        with open(os.path.join(self.root, "usr/local/bin/couchlink"), "w") as f:
            f.write('VERSION = "2.1"\n')
        with open(os.path.join(self.root, "etc/couchlink/config.toml"), "w") as f:
            f.write('pc_host = "192.168.1.50"\npc_mac = "aa:bb:cc:dd:ee:01"\n'
                    'pc_addresses = ["12:00"]\npi_address = "40:00"\n')
        self.releases = [
            {"tag_name": "v2.3", "name": "CouchLink 2.3", "published_at": "2026-11-01T00:00:00Z"},
            {"tag_name": "v2.2", "name": "CouchLink 2.2", "published_at": "2026-10-27T00:00:00Z"},
            {"tag_name": "v2.4", "name": "Unfinished", "draft": True},
            {"tag_name": "v2.0", "name": "CouchLink 2.0", "published_at": "2026-09-01T00:00:00Z"},
        ]
        self.latest = "v2.3"
        self.archives = {r["tag_name"]: release_archive(r["tag_name"], {"install.sh": STUB_INSTALLER})
                         for r in self.releases}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/releases/latest"):
                    body = json.dumps({"tag_name": test.latest}).encode()
                elif self.path.startswith("/api/releases"):
                    body = json.dumps(test.releases).encode()
                elif self.path.startswith("/archive/") and self.path[9:-7] in test.archives:
                    body = test.archives[self.path[9:-7]]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def install(self, *args, api=None, **extra_env):
        # These tests run inside a real update too, so drop anything the
        # installer that started them may have set.
        env = {k: v for k, v in os.environ.items()
               if "proxy" not in k.lower() and not k.startswith("COUCHLINK_")}
        env.update(ROOT=self.root, INSTALL_TEST="1", COUCHLINK_API=api or f"{self.base}/api",
                   COUCHLINK_ARCHIVE=f"{self.base}/archive/{{tag}}.tar.gz", **extra_env)
        r = subprocess.run(["bash", os.path.join(self.src, "install.sh"), *args], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=120, start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def stub_ran(self):
        path = os.path.join(self.root, "stub-ran")
        return open(path).read().strip() if os.path.exists(path) else None

    def test_update_installs_latest_release(self):
        code, out = self.install("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.3 --update")

    def test_update_passes_with_hid_on(self):
        code, out = self.install("--update", "--with-hid")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.3 --update --with-hid")

    def test_already_on_latest_does_nothing(self):
        self.latest = "v2.1"
        code, out = self.install("--update")
        self.assertEqual(code, 0, out)
        self.assertIn("already the latest release", out)
        self.assertIsNone(self.stub_ran())

    def test_chosen_release(self):
        code, out = self.install("--update", "--release", "v2.2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.2 --update")

    def test_older_release_needs_confirmation(self):
        code, out = self.install("--release=v2.0")
        self.assertNotEqual(code, 0)
        self.assertIn("older than the installed 2.1", out)
        self.assertIsNone(self.stub_ran())

    def test_picking_needs_a_terminal(self):
        code, out = self.install("--update", "--release")
        self.assertNotEqual(code, 0)
        self.assertIn("No terminal", out)

    def test_list_releases_skips_drafts(self):
        code, out = self.install("--list-releases")
        self.assertEqual(code, 0, out)
        self.assertIn("v2.3", out)
        self.assertIn("v2.0", out)
        self.assertNotIn("v2.4", out)

    def test_unsafe_archive_rejected(self):
        self.archives["v2.3"] = release_archive("v2.3", {"install.sh": STUB_INSTALLER,
                                                         "../../evil.sh": "boom"})
        code, out = self.install("--update")
        self.assertNotEqual(code, 0)
        self.assertIn("Unsafe entry", out)
        self.assertIsNone(self.stub_ran())

    def test_github_unreachable(self):
        code, out = self.install("--update", api="http://127.0.0.1:9/api")
        self.assertNotEqual(code, 0)
        self.assertIn("Could not reach GitHub", out)

    def lone_script(self):
        """Only install.sh, as if it was downloaded on its own."""
        for name in ("couchlink.py", "couchlink.example.toml"):
            os.remove(os.path.join(self.src, name))

    def not_installed(self):
        os.remove(os.path.join(self.root, "usr/local/bin/couchlink"))
        os.remove(os.path.join(self.root, "etc/couchlink/config.toml"))

    def test_lone_script_installs_latest_release(self):
        self.lone_script()
        self.not_installed()
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.3")         # a first install, not --update

    def test_lone_script_installs_chosen_release(self):
        self.lone_script()
        self.not_installed()
        code, out = self.install("--release", "v2.2", "--with-hid")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.2 --with-hid")

    def test_lone_script_updates_an_existing_install(self):
        self.lone_script()
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn("already installed, so this updates it", out)
        self.assertEqual(self.stub_ran(), "ran v2.3 --update")

    def test_update_without_an_install_explains(self):
        self.not_installed()
        code, out = self.install("--update")
        self.assertNotEqual(code, 0)
        self.assertIn("Run without --update to install CouchLink", out)
        self.assertIsNone(self.stub_ran())

    def test_local_without_the_files_explains(self):
        self.lone_script()
        code, out = self.install("--local")
        self.assertNotEqual(code, 0)
        self.assertIn("Run without --local to download it", out)
        self.assertIsNone(self.stub_ran())

    def test_release_marker_not_passed_on_to_the_tests(self):
        probe = os.path.join(self.root, "probe")
        with open(os.path.join(self.src, "test_couchlink.py"), "w") as f:
            f.write("import os\n"
                    f"open({probe!r}, 'w').write(os.environ.get('COUCHLINK_RELEASE_TAG', 'unset'))\n"
                    "print('Ran 1 test')\nprint('OK')\n")
        code, out = self.install("--update", COUCHLINK_RELEASE_TAG="v9.9")
        self.assertEqual(code, 0, out)
        self.assertIn("(release v9.9)", out)
        self.assertIsNone(self.stub_ran())               # installed locally, nothing downloaded
        with open(probe) as f:
            self.assertEqual(f.read(), "unset")

    def test_local_update_installs_files_next_to_the_script(self):
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.stub_ran())
        with open(os.path.join(self.root, "usr/local/bin/couchlink")) as f:
            self.assertIn(f'VERSION = "{couchlink.VERSION}"', f.read())
        # Logs stay inside the test root, so they never clash with a real install's.
        self.assertTrue(os.path.exists(os.path.join(self.root, "tmp/couchlink-check.log")))


class SetupTests(unittest.TestCase):
    """couchlink --setup, with scripted answers instead of a person."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "config.toml")
        shutil.copy(os.path.join(HERE, "couchlink.example.toml"), self.path)
        self.output = []
        self.ssh_calls = []

    def setup(self, answers, found_mac=None):
        answers = list(answers)

        def ask(question):
            self.assertTrue(answers, f"unexpected question: {question}")
            return answers.pop(0)
        code = couchlink.run_setup(self.path, ask=ask, out=self.output.append,
                                   mac_finder=lambda host: found_mac,
                                   ssh_setup=lambda host, user, out: self.ssh_calls.append((host, user)))
        self.assertEqual(answers, [], "not every answer was used")
        settings, problems = couchlink.load_config(self.path)
        self.assertEqual(problems, [])                   # no typos or broken TOML
        return code, settings

    def test_set_value_keeps_the_comment_in_its_column(self):
        text = 'pc_host = "target-pc.local"          # REQUIRED: the address\n'
        new = couchlink.set_config_value(text, "pc_host", "192.168.1.50")
        self.assertTrue(new.startswith('pc_host = "192.168.1.50" '))
        self.assertEqual(new.index("# REQUIRED: the address\n"), text.index("# REQUIRED"))

    def test_set_value_switches_on_a_commented_key(self):
        text = '# pi_address = "40:00"               # REQUIRED\n'
        new = couchlink.set_config_value(text, "pi_address", "40:00")
        self.assertTrue(new.startswith('pi_address = "40:00" '))
        self.assertEqual(new.index("# REQUIRED\n"), text.index("# REQUIRED"))

    def test_set_value_adds_a_missing_key(self):
        self.assertEqual(couchlink.set_config_value("a = 1\n", "ssh_user", "deck"), 'a = 1\nssh_user = "deck"\n')

    def test_setup_fills_in_the_pc_settings(self):
        code, s = self.setup(["192.168.1.50", "", "", "2", "n"], found_mac="aa:bb:cc:dd:ee:01")
        self.assertEqual(code, 0)
        self.assertEqual((s["pc_host"], s["pc_mac"], s["ssh_user"], s["on_pc_sleep"]),
                         ("192.168.1.50", "aa:bb:cc:dd:ee:01", "deck", "standby"))
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertEqual(self.ssh_calls, [])
        text = "\n".join(self.output)
        self.assertIn("Found aa:bb:cc:dd:ee:01", text)
        self.assertIn("Still to do", text)               # the CEC addresses
        self.assertIn("pi_address", text)
        with open(self.path) as f:
            self.assertIn("# REQUIRED: the PC's IP address", f.read())

    def test_setup_asks_again_after_a_bad_answer(self):
        code, s = self.setup(["", "target-pc.local", "192.168.1.50", "zz", "AA-BB-CC-DD-EE-02",
                              "bazzite user", "gamer", "3", "1", "y"])
        self.assertEqual(code, 0)
        self.assertEqual((s["pc_mac"], s["ssh_user"], s["on_pc_sleep"]),
                         ("aa:bb:cc:dd:ee:02", "gamer", "nothing"))
        self.assertEqual(self.ssh_calls, [("192.168.1.50", "gamer")])

    def test_setup_offers_the_saved_values(self):
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "gamer", "2", "n"])
        code, s = self.setup(["", "", "", "", "n"])
        self.assertEqual((s["pc_host"], s["pc_mac"], s["ssh_user"], s["on_pc_sleep"]),
                         ("192.168.1.50", "aa:bb:cc:dd:ee:01", "gamer", "standby"))

    def test_setup_keeps_a_switch_setting(self):
        with open(self.path) as f:
            text = f.read()
        text = couchlink.set_config_value(text, "on_pc_sleep", "switch")
        text = couchlink.set_config_value(text, "pc_sleep_switch_to", "11:00")
        with open(self.path, "w") as f:
            f.write(text)
        code, s = self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "n"])   # no sleep question
        self.assertEqual((s["on_pc_sleep"], s["pc_sleep_switch_to"]), ("switch", "11:00"))

    def test_setup_completes_a_config(self):
        with open(self.path) as f:
            text = f.read()
        text = text.replace("pc_addresses = []", 'pc_addresses = ["12:00"]')
        text = couchlink.set_config_value(text, "pi_address", "40:00")
        with open(self.path, "w") as f:
            f.write(text)
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "1", "n"])
        self.assertIn("The config is complete", "\n".join(self.output))


class DocsTests(unittest.TestCase):
    def test_name_is_spelled_couchlink_with_a_capital_l(self):
        wrong = "Couch" + "link"
        names = ["README.md", "CONTRIBUTING.md", "couchlink.py", "install.sh", "couchlink.example.toml"]
        docs = os.path.join(HERE, "docs")
        if os.path.isdir(docs):
            names += [os.path.join("docs", n) for n in os.listdir(docs) if n.endswith(".md")]
        for name in names:
            path = os.path.join(HERE, name)
            if os.path.exists(path):
                with open(path) as f:
                    self.assertNotIn(wrong, f.read(), f"{name} spells it {wrong}, use CouchLink")

    def test_links_between_pages_resolve(self):
        """Every relative link in the README and docs/ points to a file that
        exists, and every #anchor to a heading in it."""
        import re

        def anchors(path):
            with open(path) as f:
                heads = re.findall(r"^#+ (.+)$", f.read(), re.M)
            return {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in heads}

        pages = [os.path.join(HERE, "README.md")]
        docs = os.path.join(HERE, "docs")
        if os.path.isdir(docs):
            pages += [os.path.join(docs, n) for n in sorted(os.listdir(docs)) if n.endswith(".md")]
        for page in pages:
            with open(page) as f:
                text = re.sub(r"```.*?```", "", f.read(), flags=re.S)
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"[a-z]+:", target):
                    continue
                path, _, anchor = target.partition("#")
                full = os.path.normpath(os.path.join(os.path.dirname(page), path)) if path else page
                self.assertTrue(os.path.exists(full), f"{page}: broken link {target}")
                if anchor:
                    self.assertIn(anchor, anchors(full), f"{page}: no heading for {target}")


class PcPowerTests(unittest.TestCase):
    """Telling sleep, power-off and restarts apart, from what the PC announces."""

    def on_screen(self, **overrides):
        s = Sim(**dict(REAL, **overrides))
        s.pc(True)
        s.ev(("pc_power", "link_up"))
        s.ev(msg("5f:81:12:00"))                        # PC on screen
        s.run(30)
        return s

    def power(self, s):
        return s.ctl.snapshot()["pc_power"]

    def test_parse_gdbus_lines(self):
        p = couchlink.parse_power_line
        base = "/org/freedesktop/login1: org.freedesktop.login1.Manager."
        self.assertEqual(p(base + "PrepareForSleep (true,)"), "sleep")
        self.assertEqual(p(base + "PrepareForSleep (false,)"), "resume")
        self.assertEqual(p(base + "PrepareForShutdown (true,)"), "shutdown")
        self.assertEqual(p(base + "PrepareForShutdownWithMetadata (true, {'type': <'reboot'>})"), "reboot")
        self.assertEqual(p(base + "PrepareForShutdownWithMetadata (true, {'type': <'poweroff'>})"), "poweroff")
        self.assertIsNone(p("Monitoring signals from all objects owned by org.freedesktop.login1"))

    def test_session_end(self):
        self.assertEqual(couchlink.power_link_end("Timeout, server 192.168.1.50 not responding."), "lost")
        self.assertEqual(couchlink.power_link_end("Connection to 192.168.1.50 closed by remote host."), "closed")

    def test_sleep_shows_asleep(self):
        s = self.on_screen()
        s.ev(("pc_power", "sleep"))
        s.pc(False)
        self.assertEqual(self.power(s), "asleep")

    def test_poweroff_shows_off_and_runs_on_pc_sleep(self):
        s = self.on_screen()
        s.ev(("pc_power", "poweroff"))
        s.ev(("pc_power", "shutdown"))                  # the plain signal that follows
        s.ev(("pc_power", "link_closed"))
        s.pc(False)
        self.assertEqual(self.power(s), "off")
        s.run(15)
        self.assertIn("tx 1F:82:11:00", s.act.cec)      # on_pc_sleep = "switch" still runs

    def test_session_closing_means_off(self):
        s = self.on_screen()
        s.ev(("pc_power", "link_closed"))               # no announcement, gdbus missing
        s.pc(False)
        self.assertEqual(self.power(s), "off")

    def test_session_freezing_means_asleep(self):
        s = self.on_screen()
        s.pc(False)
        self.assertEqual(self.power(s), "asleep")
        s.run(15)
        s.ev(("pc_power", "link_lost"))
        self.assertEqual(self.power(s), "asleep")

    def test_restart_is_not_treated_as_sleep(self):
        s = self.on_screen()
        s.ev(("pc_power", "reboot"))
        s.ev(("pc_power", "shutdown"))
        s.ev(("pc_power", "link_closed"))
        s.pc(False)
        self.assertEqual(self.power(s), "restarting")
        s.run(60)
        self.assertNotIn("tx 1F:82:11:00", s.act.cec)   # TV left alone
        self.assertEqual(s.ctl.tv_input, "12:00")
        s.pc(True)
        s.run(30)
        self.assertEqual(self.power(s), "on")
        self.assertEqual(s.act.suspends, 0)

    def test_restart_that_never_comes_back_shows_off(self):
        s = self.on_screen()
        s.ev(("pc_power", "reboot"))
        s.pc(False)
        s.run(301)
        self.assertEqual(self.power(s), "off")

    def test_no_information_shows_unknown(self):
        s = Sim(**REAL)                                 # no watcher session, e.g. no SSH key yet
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.pc(False)
        self.assertEqual(self.power(s), "unknown")

    def test_our_suspend_shows_asleep(self):
        s = Sim(**REAL)
        s.pc(True)
        s.ev(msg("5f:81:11:00"))                        # TV elsewhere
        s.run(10)
        self.assertEqual(s.act.suspends, 1)
        s.pc(False)
        self.assertEqual(self.power(s), "asleep")

    def test_setting_validated(self):
        _, problems = couchlink.validate(dict(BASE, watch_pc_power="yes"))
        self.assertTrue(any("watch_pc_power" in p for p in problems))


class PcNameTests(unittest.TestCase):
    def test_name_looked_up_once_pc_is_reachable(self):
        s = Sim()
        s.pc(False)
        self.assertEqual(s.act.name_lookups, 0)
        s.pc(True)
        self.assertEqual(s.act.name_lookups, 1)
        s.pc(True)
        self.assertEqual(s.act.name_lookups, 1)          # still in flight
        s.ev(("pc_name", "steamdeck"))
        self.assertEqual(s.ctl.snapshot()["pc_name"], "steamdeck")
        s.pc(False)
        s.pc(True)
        self.assertEqual(s.act.name_lookups, 1)          # known now, no more lookups

    def test_failed_lookup_retried_a_few_times(self):
        s = Sim()
        for _ in range(5):
            s.pc(True)
            s.ev(("pc_name", None))
            s.pc(False)
        self.assertEqual(s.act.name_lookups, 3)
        self.assertIsNone(s.ctl.snapshot()["pc_name"])

    def test_config_name_wins(self):
        s = Sim(pc_name="Living Room PC")
        s.pc(True)
        self.assertEqual(s.act.name_lookups, 0)
        self.assertEqual(s.ctl.snapshot()["pc_name"], "Living Room PC")

    def test_bad_name_rejected(self):
        _, problems = couchlink.validate(dict(BASE, pc_name="x" * 41))
        self.assertTrue(any("pc_name" in p for p in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
