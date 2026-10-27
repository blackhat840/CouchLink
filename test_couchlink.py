#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Offline tests for couchlink.py. No TV, Pi or PC needed: a simulated clock and
fake actions stand in for the hardware. Run from the folder that holds both files:

    python3 test_couchlink.py
"""
import importlib.util
import os
import sys
import tempfile
import unittest

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
        self.assertIsNone(couchlink.parse_rx("TRAFFIC: [ 1]\t>> 01"))
        self.assertIsNone(couchlink.parse_rx("NOTICE: connected"))
        self.assertEqual(couchlink.parse_key("DEBUG: key pressed: fast forward (49)"), "fast forward")

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
