# SPDX-License-Identifier: GPL-3.0-or-later
"""Without a TV (use_tv = false): CouchLink only wakes the PC for remote play and
puts it to sleep when asked."""

import os
import shutil
import tempfile
import unittest

from helpers import BASE, HERE, Sim
from couchlink import cec, config, setup_cec, setup_pc

NO_TV = dict(use_tv=False, pc_addresses=None, pi_address=None, soundbar_address=None,
             soundbar_pc_audio_address=None)


class NoTvConfigTests(unittest.TestCase):
    def test_no_cec_addresses_needed(self):
        _, problems = config.validate(dict(BASE, **NO_TV))
        self.assertEqual(problems, [])

    def test_with_a_tv_they_still_are(self):
        _, problems = config.validate(dict(BASE, use_tv=True, pc_addresses=None, pi_address=None))
        self.assertTrue(any("pc_addresses" in p for p in problems))
        self.assertTrue(any("pi_address" in p for p in problems))

    def test_must_be_true_or_false(self):
        _, problems = config.validate(dict(BASE, use_tv="no"))
        self.assertTrue(any("use_tv" in p for p in problems))

    def test_stand_in_for_cec_client(self):
        stand_in = cec.NoCec()
        stand_in.send("on 0")       # sends nothing, and doesn't fail
        stand_in.stop()


class NoTvControllerTests(unittest.TestCase):
    def sim(self, pc_up=False):
        s = Sim(**NO_TV)
        s.pc(pc_up)
        s.run(30)
        return s

    def test_never_talks_to_the_tv(self):
        s = self.sim()
        s.run(600)
        self.assertEqual(s.act.cec, [])                 # not even power queries

    def test_awake_pc_is_left_alone(self):
        s = self.sim(pc_up=True)
        s.run(3600, step=10)
        self.assertEqual((s.act.suspends, s.act.wols), (0, 0))

    def test_pc_waking_by_itself_is_left_alone(self):
        s = self.sim()
        s.pc(True)                                      # Moonlight's own Wake-on-LAN, say
        s.run(600)
        self.assertEqual((s.act.cec, s.act.suspends), ([], 0))

    def test_wake_pc_starts_a_session(self):
        s = self.sim()
        ok, result, _ = s.do("wake", tv=False)
        self.assertEqual((ok, result), (True, "session_started"))
        self.assertEqual(s.act.wols, 1)
        s.pc(True)
        s.run(3600, step=10)
        self.assertEqual(s.act.suspends, 0)
        self.assertIsNotNone(s.ctl.snapshot()["remote_session_seconds"])

    def test_wake_pc_and_tv_is_refused(self):
        s = self.sim()
        self.assertEqual(s.do("wake", tv=True)[:2], (False, "no_tv"))
        self.assertEqual((s.act.cec, s.act.wols), ([], 0))
        self.assertEqual(s.do("wake", tv=False)[:2], (True, "session_started"))   # not held up

    def test_pc_sleeping_by_itself_ends_the_session(self):
        s = self.sim()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(600)
        s.ev(("pc_power", "sleep"))                     # its own sleep timer
        s.pc(False)
        s.run(600)
        self.assertIsNone(s.ctl.snapshot()["remote_session_seconds"])
        self.assertEqual(s.act.wols, 1)                 # not woken again

    def test_ending_a_session_leaves_the_pc_awake(self):
        s = self.sim()
        s.do("wake", tv=False)
        s.pc(True)
        s.run(60)
        s.do("end_session")
        s.run(600)
        self.assertEqual(s.act.suspends, 0)             # the PC's own sleep timer does that

    def test_put_pc_to_sleep(self):
        s = self.sim(pc_up=True)
        self.assertEqual(s.do("sleep")[:2], (True, "suspending"))
        self.assertEqual((s.act.suspends, s.act.cec), (1, []))

    def test_snapshot_says_no_tv(self):
        self.assertFalse(self.sim().ctl.snapshot()["tv"])
        self.assertTrue(Sim().ctl.snapshot()["tv"])


class NoTvSetupTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.path = os.path.join(d, "config.toml")
        shutil.copy(os.path.join(HERE, "couchlink.example.toml"), self.path)

    def test_setup_without_a_tv_skips_the_tv_questions_and_is_complete(self):
        answers = ["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "2", "", "n", "n", "n"]
        lines = []

        def ask(question):
            self.assertTrue(answers, f"unexpected question: {question}")
            return answers.pop(0)
        setup_pc.run_setup(self.path, ask=ask, out=lines.append, mac_finder=lambda host: None,
                           ssh_setup=lambda *a: True, pc_check=lambda host: True,
                           service=lambda action: False, secret=lambda q: "")
        self.assertEqual(answers, [])
        text = "\n".join(lines)
        self.assertNotIn("When the PC wakes by itself", text)
        self.assertNotIn("setup-cec", text.split("Saved to")[-1])
        settings, problems = config.load_config(self.path)
        self.assertEqual(problems, [])
        self.assertFalse(settings["use_tv"])
        self.assertEqual(config.validate(settings)[1], [])          # nothing left to do
        self.assertIn("The config is complete", text)

    def test_setup_cec_turns_tv_mode_back_on(self):
        with open(self.path) as f:
            text = f.read()
        for key, value in (("pc_host", "192.168.1.50"), ("pc_mac", "aa:bb:cc:dd:ee:01"), ("use_tv", False)):
            text = config.set_config_value(text, key, value)
        with open(self.path, "w") as f:
            f.write(text)
        scan = [{"logical": 0, "name": "TV", "address": "00:00", "vendor": "", "osd": ""},
                {"logical": 1, "name": "Recorder 1", "address": "40:00", "vendor": "", "osd": ""}]
        answers = ["1", "y", "n"]
        setup_cec.run_cec_setup(self.path, ask=lambda q: answers.pop(0), out=lambda line: None,
                                scan=lambda command: scan, watch=lambda c, s, wanted, out: "20:00",
                                service=lambda action: False)
        settings, problems = config.load_config(self.path)
        self.assertEqual(problems, [])
        self.assertTrue(settings["use_tv"])
        self.assertEqual(settings["pc_addresses"], ["20:00"])
