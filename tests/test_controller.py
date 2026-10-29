# SPDX-License-Identifier: GPL-3.0-or-later
"""The controller: waking, suspending, the TV, sleep, power states, remote
sessions and the actions the status page and API ask for."""

import os
import shutil
import tempfile
import unittest

from helpers import BASE, Clock, HERE, REAL, Sim, assert_private, msg, out, page_text
import couchlink
from couchlink import auth, cec, config, controller, power, setup_pc, web


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
        s.pc(True)                                      # PC finishes waking from CouchLink's WoL
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
        self.assertIsNone(cec.parse_rx("TRAFFIC: [ 1]\t<< 1f:82:30:00"))
        self.assertIsNone(cec.parse_tx("TRAFFIC: [ 1]\t>> 0f:86:30:00"))
        self.assertEqual(cec.parse_tx("TRAFFIC: [ 1]\t<< 1f:82:40:00").params, ["40", "00"])
        self.assertIsNone(cec.parse_rx("TRAFFIC: [ 1]\t>> 01"))
        self.assertIsNone(cec.parse_rx("NOTICE: connected"))
        self.assertEqual(cec.parse_key("DEBUG: key pressed: fast forward (49)"), "fast forward")

    def test_version_matches_docstring(self):
        self.assertIn(f"CouchLink {couchlink.VERSION}\n", couchlink.__doc__)

    def test_status_page_urls(self):
        self.assertEqual(web.status_page_urls(8080, "192.168.1.20", "couchlink"),
                         ["http://192.168.1.20:8080", "http://couchlink.local:8080"])
        self.assertEqual(web.status_page_urls(8080, None, "couchlink"), ["http://couchlink.local:8080"])
        self.assertEqual(web.status_page_urls(8080, None, ""), ["http://<pi-address>:8080"])

    def test_own_address(self):
        self.assertEqual(web.own_address("127.0.0.1"), "127.0.0.1")
        self.assertIsNone(web.own_address("no-such-host.invalid"))

    def test_status_page_text_has_enough_contrast(self):
        """The page's gray text colors stay readable (WCAG AA, 4.5:1) on its tiles and background."""
        import re

        def luminance(hexcolor):
            rgb = [int(hexcolor[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def ratio(a, b):
            hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        html = page_text()
        page = re.search(r"--bg: #([0-9a-f]{6})", html).group(1)
        tile = "0e1320"     # --panel blended over --bg
        for name in ("muted", "dim"):
            color = re.search(rf"--{name}: #([0-9a-f]{{6}})", html).group(1)
            for ground in (page, tile):
                self.assertGreaterEqual(ratio(color, ground), 4.5, f"--{name} on #{ground}")

    def test_status_page_parts(self):
        html = page_text()
        self.assertIn('id="lost" role="status" hidden', html)       # the lost-contact banner
        self.assertIn('</svg>PC</div>', html)                        # the PC tile
        self.assertIn("min-height: 44px", html)                      # tabs big enough to tap

    def test_status_page_links_to_repo(self):
        self.assertIn('href="https://github.com/blackhat840/CouchLink"', page_text())

    def test_input_names(self):
        s = Sim()
        n = s.ctl.input_name
        self.assertEqual(n("30:00"), "HDMI 3 (PC)")
        self.assertEqual(n("22:00"), "HDMI 2.2 (soundbar, PC audio)")
        self.assertEqual(n("21:00"), "HDMI 2.1")
        self.assertEqual(n("20:00"), "HDMI 2 (soundbar)")
        self.assertEqual(n("00:00"), "TV / built-in apps")


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
        _, problems = config.validate(cfg)
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
        _, problems = config.validate(dict(BASE, tv_boot_timeout=-1, tv_boot_reclaims=1.5))
        text = " ".join(problems)
        self.assertIn("tv_boot_timeout", text)
        self.assertIn("tv_boot_reclaims", text)


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
        p = power.parse_power_line
        base = "/org/freedesktop/login1: org.freedesktop.login1.Manager."
        self.assertEqual(p(base + "PrepareForSleep (true,)"), "sleep")
        self.assertEqual(p(base + "PrepareForSleep (false,)"), "resume")
        self.assertEqual(p(base + "PrepareForShutdown (true,)"), "shutdown")
        self.assertEqual(p(base + "PrepareForShutdownWithMetadata (true, {'type': <'reboot'>})"), "reboot")
        self.assertEqual(p(base + "PrepareForShutdownWithMetadata (true, {'type': <'poweroff'>})"), "poweroff")
        self.assertIsNone(p("Monitoring signals from all objects owned by org.freedesktop.login1"))

    def test_session_end(self):
        self.assertEqual(power.power_link_end("Timeout, server 192.168.1.50 not responding."), "lost")
        self.assertEqual(power.power_link_end("Connection to 192.168.1.50 closed by remote host."), "closed")

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

    def test_up_and_down_time(self):
        s = Sim(**REAL)
        snap = s.ctl.snapshot()
        self.assertIsNone(snap["pc_state_seconds"])     # no ping yet
        s.pc(True)
        s.run(100)
        snap = s.ctl.snapshot()
        self.assertEqual(snap["pc_state_seconds"], 100)
        self.assertTrue(snap["pc_state_since_start"])   # it was already up when CouchLink started
        s.pc(False)
        s.run(30)
        snap = s.ctl.snapshot()
        self.assertEqual(snap["pc_state_seconds"], 30)
        self.assertFalse(snap["pc_state_since_start"])  # CouchLink saw it go down
        s.pc(True)
        self.assertEqual(s.ctl.snapshot()["pc_state_seconds"], 0)

    def test_status_page_describes_the_sleep_action(self):
        snap = Sim(**REAL).ctl.snapshot()
        self.assertEqual((snap["on_pc_sleep"], snap["on_pc_sleep_target"]), ("switch", "HDMI 1.1"))
        self.assertEqual(snap["power_poll_interval"], 60)

    def test_setting_validated(self):
        _, problems = config.validate(dict(BASE, watch_pc_power="yes"))
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
        _, problems = config.validate(dict(BASE, pc_name="x" * 41))
        self.assertTrue(any("pc_name" in p for p in problems))


class RemoteSessionTests(unittest.TestCase):
    """Waking the PC for streaming without the TV, and Wake PC and TV."""

    def tv_elsewhere(self, pc_up=False, **overrides):
        s = Sim(**dict(REAL, **overrides))
        s.pc(pc_up)
        s.ev(("pc_power", "link_up"))
        s.ev(msg("5f:81:11:00"))                        # TV on the streamer
        s.run(30)
        s.act.cec.clear()
        return s

    def left(self, s):
        return s.ctl.snapshot()["remote_session_seconds"]

    def tv_commands(self, s):
        return [c for c in s.act.cec if c != "pow 0"]  # power queries don't change the TV

    def test_session_wakes_pc_and_leaves_tv_alone(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        self.assertEqual(s.act.wols, 1)
        s.run(5)
        s.pc(True)
        s.run(3600)
        self.assertEqual(s.act.wols, 1)
        self.assertEqual(s.act.suspends, 0)             # kept awake without the TV
        self.assertEqual(self.tv_commands(s), [])                 # TV not touched
        self.assertEqual(s.ctl.tv_input, "11:00")
        self.assertAlmostEqual(self.left(s), 3600 + 5, delta=2)

    def test_session_retries_wol_then_gives_up(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.run(300)
        self.assertEqual(s.act.wols, s.cfg.max_wol_attempts)
        self.assertIsNone(self.left(s))                 # ended, not stuck waking

    def test_wake_again_while_running(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(1800)
        self.assertEqual(s.do("wake", tv=False)[1], "session_running")
        self.assertEqual(s.act.wols, 1)

    def test_pc_already_on_is_kept_awake(self):
        s = self.tv_elsewhere(pc_up=True, away_debounce=60)
        s.do("wake", tv=False)
        s.run(600)
        self.assertEqual(s.act.wols, 0)
        self.assertEqual(s.act.suspends, 0)

    def test_no_timer_ends_a_long_session(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(12 * 3600, step=10)                       # a very long game
        self.assertIsNotNone(self.left(s))
        self.assertEqual(s.act.suspends, 0)

    def test_pc_sleeping_by_itself_ends_it_and_it_stays_asleep(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(2 * 3600, step=10)
        s.ev(("pc_power", "sleep"))                     # the PC's own sleep timer, after you stop playing
        s.pc(False)
        s.run(600)
        self.assertIsNone(self.left(s))
        self.assertEqual(s.act.wols, 1)
        self.assertEqual(s.act.suspends, 0)

    def test_end_by_hand(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(60)
        s.do("end_session")
        self.assertIsNone(self.left(s))
        s.run(s.cfg.away_debounce + 1)
        self.assertEqual(s.act.suspends, 1)

    def test_tv_switched_to_pc_becomes_normal_use(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(60)
        s.ev(msg("5f:81:12:00"))                        # TV switched to the PC
        self.assertIsNone(self.left(s))
        s.run(60)
        s.ev(msg("5f:81:11:00"))                        # and away again
        s.run(s.cfg.away_debounce + 1)
        self.assertEqual(s.act.suspends, 1)             # usual rules again

    def test_pc_going_to_sleep_ends_session(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(60)
        s.ev(("pc_power", "sleep"))
        s.pc(False)
        self.assertIsNone(self.left(s))
        s.run(300)
        self.assertEqual(s.act.wols, 1)                 # not woken again
        self.assertEqual(self.tv_commands(s), [])

    def test_restart_keeps_session(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(60)
        s.ev(("pc_power", "reboot"))
        s.pc(False)
        s.run(60)
        self.assertIsNotNone(self.left(s))
        self.assertEqual(s.ctl.snapshot()["pc_power"], "restarting")
        s.pc(True)
        s.run(600)
        self.assertIsNotNone(self.left(s))
        self.assertEqual(s.act.wols, 1)
        self.assertEqual(s.act.suspends, 0)
        self.assertEqual(self.tv_commands(s), [])

    def test_restart_that_never_comes_back_ends_session(self):
        s = self.tv_elsewhere()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(60)
        s.ev(("pc_power", "reboot"))
        s.pc(False)
        s.run(400)
        self.assertIsNone(self.left(s))
        self.assertEqual(s.act.wols, 1)

    def test_not_needed_when_tv_is_on_pc(self):
        s = Sim(**REAL)
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.do("wake", tv=False)
        self.assertIsNone(self.left(s))

    def test_wake_pc_and_tv(self):
        s = self.tv_elsewhere(auto_switch_on_pc_wake="never")
        s.do("wake", tv=True)
        self.assertIn("on 0", s.act.cec)
        s.run(2)
        self.assertIn("tx 1F:82:12:00", s.act.cec)
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertEqual(s.act.wols, 1)
        self.assertIsNone(self.left(s))

    def test_wake_pc_and_tv_after_pc_slept_on_screen(self):
        s = Sim(**dict(REAL, on_pc_sleep="nothing", pc_sleep_switch_to=None))
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.pc(False)                                     # put to sleep on screen
        s.run(60)
        self.assertEqual(s.act.wols, 0)
        s.do("wake", tv=True)
        s.run(1)
        self.assertEqual(s.act.wols, 1)                 # a request wakes it anyway

    def test_wake_pc_and_tv_with_tv_off(self):
        s = self.tv_elsewhere()
        s.ev(msg("0f:36"))                              # TV to standby
        s.run(10)
        s.do("wake", tv=True)
        self.assertIn("on 0", s.act.cec)
        self.assertTrue(s.ctl.snapshot()["tv_starting"])

    def test_old_timer_setting_is_ignored_then_commented_out(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "config.toml")
        with open(os.path.join(HERE, "couchlink.example.toml")) as f:
            text = f.read().replace("[timing]\n", "[timing]\nremote_session_hours = 4\n")
        with open(path, "w") as f:
            f.write(text)
        _, problems = config.load_config(path)
        self.assertEqual(problems, [])                  # not reported as a typo
        lines = []
        config.migrate_config(path, out=lines.append)
        with open(path) as f:
            self.assertIn("# remote_session_hours = 4", f.read())
        self.assertTrue(any("no longer end on a timer" in line for line in lines))


class ActionTests(unittest.TestCase):
    """Wake, sleep and end session, as the status page and API ask for them."""

    def test_sleep_ends_session_and_suspends_now(self):
        s = Sim(**REAL)
        s.pc(False)
        s.ev(msg("5f:81:11:00"))
        s.run(30)
        s.do("wake", tv=False)
        s.pc(True)
        s.run(60)
        self.assertEqual(s.do("sleep"), (True, "suspending", "Putting the PC to sleep"))
        self.assertEqual(s.act.suspends, 1)             # no away_debounce wait
        self.assertIsNone(s.ctl.snapshot()["remote_session_seconds"])

    def test_sleep_on_screen_runs_on_pc_sleep(self):
        s = Sim(**REAL)                                 # on_pc_sleep = "switch" to 11:00
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.do("sleep")
        self.assertIn("tx 1F:82:11:00", s.act.cec)
        self.assertEqual(s.ctl.tv_input, "11:00")
        self.assertEqual(s.act.suspends, 1)

    def test_sleep_on_screen_with_nothing_leaves_tv_and_stays_asleep(self):
        s = Sim(**dict(REAL, on_pc_sleep="nothing", pc_sleep_switch_to=None))
        s.pc(True)
        s.ev(msg("5f:81:12:00"))
        s.run(30)
        s.do("sleep")
        self.assertEqual(s.act.suspends, 1)
        s.ev(("suspend_done", 0, ""))
        s.pc(False)
        s.run(120)
        self.assertEqual(s.ctl.tv_input, "12:00")
        self.assertEqual(s.act.wols, 0)                 # not woken again

    def test_sleep_when_already_asleep(self):
        s = Sim(**REAL)
        s.pc(False)
        s.ev(msg("5f:81:11:00"))
        self.assertEqual(s.do("sleep")[1], "already_asleep")
        self.assertEqual(s.act.suspends, 0)

    def test_rate_limit(self):
        s = Sim(**REAL)
        s.pc(False)
        s.ev(msg("5f:81:11:00"))
        self.assertTrue(s.do("wake", tv=False)[0])
        self.assertEqual(s.do("wake", tv=True)[:2], (False, "too_soon"))
        s.run(controller.ACTION_COOLDOWN)
        self.assertTrue(s.do("wake", tv=False)[0])

    def test_end_without_session(self):
        s = Sim(**REAL)
        self.assertEqual(s.do("end_session")[1], "no_session")

    def test_unknown_action(self):
        s = Sim(**REAL)
        self.assertEqual(s.do("reboot")[:2], (False, "unknown_action"))

    def test_action_event_answers_on_reply_queue(self):
        import queue
        s = Sim(**REAL)
        s.pc(False)
        s.ev(msg("5f:81:11:00"))
        q = queue.Queue()
        s.ev(("action", "wake", {"tv": False}, "192.168.1.20", q))
        self.assertEqual(q.get_nowait()[:2], (True, "session_started"))

    def test_api_keys_setting(self):
        self.assertEqual(config.DEFAULTS["api_keys"], {})          # off by default
        _, problems = config.validate(dict(BASE, api_keys={"home-assistant": "hunter2"}))
        self.assertTrue(any("home-assistant" in p for p in problems))  # a plain key is refused
        _, problems = config.validate(dict(BASE, api_keys={"bad name!": auth.hash_api_key("k")}))
        self.assertTrue(any("bad name!" in p for p in problems))
        _, problems = config.validate(dict(BASE, api_keys={"home-assistant": auth.hash_api_key("k")}))
        self.assertEqual(problems, [])

    def test_new_api_key_saves_only_the_hash(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "config.toml")
        shutil.copy(os.path.join(HERE, "couchlink.example.toml"), path)
        lines = []
        self.assertEqual(setup_pc.new_api_key(path, "home-assistant", out=lines.append), 0)
        key = lines[0].split(": ", 1)[1]
        secrets = config.secrets_path(path)
        self.assertNotIn(key, open(secrets).read())
        self.assertNotIn("home-assistant", open(path).read())     # the hash isn't in config.toml
        assert_private(self, secrets)     # only root can read the hashes
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        self.assertEqual(settings["api_keys"], {"home-assistant": auth.hash_api_key(key)})
        setup_pc.new_api_key(path, "phone", out=lines.append)       # another name keeps the first
        self.assertEqual(sorted(config.load_config(path)[0]["api_keys"]), ["home-assistant", "phone"])
        setup_pc.new_api_key(path, "home-assistant", out=lines.append)   # the same name replaces it
        self.assertEqual(open(secrets).read().count("home-assistant ="), 1)
        self.assertNotEqual(config.load_config(path)[0]["api_keys"]["home-assistant"], auth.hash_api_key(key))
        self.assertEqual(setup_pc.new_api_key(path, "bad name!", out=lines.append), 1)

    def test_bearer_key(self):
        self.assertEqual(auth.bearer_key({"Authorization": "Bearer abc"}), "abc")
        self.assertEqual(auth.bearer_key({"Authorization": "bearer  abc "}), "abc")
        self.assertIsNone(auth.bearer_key({"Authorization": "Basic abc"}))
        self.assertIsNone(auth.bearer_key({}))

    def test_key_guard_locks_out_for_a_minute(self):
        clock = Clock()
        g = auth.KeyGuard(clock)
        for _ in range(5):
            self.assertFalse(g.locked("192.168.1.20"))
            g.failed("192.168.1.20")
        self.assertTrue(g.locked("192.168.1.20"))
        self.assertFalse(g.locked("192.168.1.21"))                  # only that address
        clock.t += 61
        self.assertFalse(g.locked("192.168.1.20"))
