# SPDX-License-Identifier: GPL-3.0-or-later
"""--setup and --setup-cec, with scripted answers instead of a person."""

import os
import shutil
import tempfile
import unittest

from helpers import Clock, HERE, assert_private, msg
from couchlink import auth, cec, config, setup_cec, setup_pc


class SetupTests(unittest.TestCase):
    """couchlink --setup, with scripted answers instead of a person."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "config.toml")
        shutil.copy(os.path.join(HERE, "couchlink.example.toml"), self.path)
        self.output = []
        self.ssh_calls = []

    def setup(self, answers, found_mac=None, pc_awake=True, wakes=True, ssh_works=True, running=True,
              passwords=()):
        answers, passwords = list(answers), list(passwords)
        self.service_calls, self.wake_calls = [], []

        def ask(question):
            self.assertTrue(answers, f"unexpected question: {question}")
            return answers.pop(0)

        def ssh_setup(host, user, out):
            self.ssh_calls.append((host, user))
            return ssh_works

        def waker(host, out):
            self.wake_calls.append(host)
            return wakes

        def service(action):
            self.service_calls.append(action)
            return running

        def secret(question):
            self.assertTrue(passwords, f"unexpected password question: {question}")
            return passwords.pop(0)
        code = setup_pc.run_setup(self.path, ask=ask, out=self.output.append,
                                   mac_finder=lambda host: found_mac, ssh_setup=ssh_setup,
                                   pc_check=lambda host: pc_awake, waker=waker, service=service,
                                   secret=secret)
        self.assertEqual(passwords, [], "not every password was used")
        self.assertEqual(answers, [], "not every answer was used")
        settings, problems = config.load_config(self.path)
        self.assertEqual(problems, [])                   # no typos or broken TOML
        return code, settings

    def test_set_value_keeps_the_comment_in_its_column(self):
        text = 'pc_host = "target-pc.local"          # REQUIRED: the address\n'
        new = config.set_config_value(text, "pc_host", "192.168.1.50")
        self.assertTrue(new.startswith('pc_host = "192.168.1.50" '))
        self.assertEqual(new.index("# REQUIRED: the address\n"), text.index("# REQUIRED"))

    def test_set_value_switches_on_a_commented_key(self):
        text = '# pi_address = "40:00"               # REQUIRED\n'
        new = config.set_config_value(text, "pi_address", "40:00")
        self.assertTrue(new.startswith('pi_address = "40:00" '))
        self.assertEqual(new.index("# REQUIRED\n"), text.index("# REQUIRED"))

    def test_set_value_keeps_the_next_line(self):
        text = '# soundbar_address = "10:00"\n# soundbar_pc_audio_address = "12:00"  # only if\n'
        new = config.set_config_value(text, "soundbar_address", "10:00")
        self.assertEqual(new, 'soundbar_address = "10:00"\n# soundbar_pc_audio_address = "12:00"  # only if\n')

    def test_set_value_adds_a_missing_key(self):
        self.assertEqual(config.set_config_value("a = 1\n", "ssh_user", "deck"), 'a = 1\nssh_user = "deck"\n')

    def test_setup_fills_in_the_pc_settings(self):
        code, s = self.setup(["192.168.1.50", "", "", "", "", "", "2", "", "n", "", "n"], found_mac="aa:bb:cc:dd:ee:01")
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
        code, s = self.setup(["", "target-pc.local", "192.168.1.50", "zz", "AA-BB-CC-DD-EE-02", "bazzite user", "gamer", "", "", "", "3", "1", "", "n", "", "y"])
        self.assertEqual(code, 0)
        self.assertEqual((s["pc_mac"], s["ssh_user"], s["on_pc_sleep"]),
                         ("aa:bb:cc:dd:ee:02", "gamer", "nothing"))
        self.assertEqual(self.ssh_calls, [("192.168.1.50", "gamer")])

    def test_setup_offers_the_saved_values(self):
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "gamer", "", "", "", "2", "", "n", "", "n"])
        code, s = self.setup(["", "", "", "", "", "", "", "", "n", "", "n"])
        self.assertEqual((s["pc_host"], s["pc_mac"], s["ssh_user"], s["on_pc_sleep"]),
                         ("192.168.1.50", "aa:bb:cc:dd:ee:01", "gamer", "standby"))

    def test_setup_keeps_a_switch_setting(self):
        with open(self.path) as f:
            text = f.read()
        text = config.set_config_value(text, "on_pc_sleep", "switch")
        text = config.set_config_value(text, "pc_sleep_switch_to", "11:00")
        with open(self.path, "w") as f:
            f.write(text)
        code, s = self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "", "n", "", "n"])   # no sleep question
        self.assertEqual((s["on_pc_sleep"], s["pc_sleep_switch_to"]), ("switch", "11:00"))

    def test_setup_completes_a_config(self):
        with open(self.path) as f:
            text = f.read()
        text = text.replace("pc_addresses = []", 'pc_addresses = ["12:00"]')
        text = config.set_config_value(text, "pi_address", "40:00")
        with open(self.path, "w") as f:
            f.write(text)
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "1", "", "n", "", "n"])
        self.assertIn("The config is complete", "\n".join(self.output))

    def test_setup_asks_for_name_wake_and_api_key(self):
        code, s = self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "Living Room PC", "", "2", "1", "n", "y", "y", "", "n"], passwords=["sofa-console", "sofa-console"])
        self.assertEqual((s["pc_name"], s["auto_switch_on_pc_wake"]), ("Living Room PC", "if_tv_on"))
        self.assertFalse(s["status_page_buttons"])
        self.assertTrue(auth.check_password("sofa-console", s["admin_password_hash"]))
        key = next(line for line in self.output
                   if line.startswith("New API key home-assistant: ")).split(": ", 1)[1]
        self.assertEqual(s["api_keys"], {"home-assistant": auth.hash_api_key(key)})
        with open(self.path) as f:
            text = f.read()
        self.assertNotIn(key, text)
        self.assertNotIn("sofa-console", text)
        assert_private(self, self.path)    # only root can read the hashes
        code, s = self.setup(["", "", "", "", "", "", "", "", "", "", "n"])  # Enter keeps them all
        self.assertEqual((s["pc_name"], s["auto_switch_on_pc_wake"]), ("Living Room PC", "if_tv_on"))
        self.assertFalse(s["status_page_buttons"])                    # Enter keeps "n" too
        self.assertEqual(s["api_keys"], {"home-assistant": auth.hash_api_key(key)})
        self.assertTrue(auth.check_password("sofa-console", s["admin_password_hash"]))

    def test_setup_second_key_keeps_the_first(self):
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "1", "", "n", "y", "home-assistant", "n"])
        self.setup(["", "", "", "", "", "", "", "", "n", "y", "phone", "n"])
        s = config.load_config(self.path)[0]
        self.assertEqual(sorted(s["api_keys"]), ["home-assistant", "phone"])

    def test_setup_password_must_be_long_enough_and_match(self):
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "1", "", "y", "", "n"],
                   passwords=["short", "long enough 1", "long enough 2", "long enough", "long enough"])
        text = "\n".join(self.output)
        self.assertIn("at least 8 characters", text)
        self.assertIn("didn't match", text)
        self.assertTrue(auth.check_password("long enough", config.load_config(self.path)[0]["admin_password_hash"]))

    def test_ssh_waits_for_a_sleeping_pc_to_wake(self):
        code, s = self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "1", "", "n", "", "y", "y"], pc_awake=False)
        self.assertEqual(self.wake_calls, ["192.168.1.50"])
        self.assertEqual(self.service_calls, ["stop", "start"])     # paused so it isn't suspended again
        self.assertEqual(self.ssh_calls, [("192.168.1.50", "deck")])
        self.assertIn("doesn't answer", "\n".join(self.output))

    def test_ssh_skipped_when_the_pc_stays_asleep(self):
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "1", "", "n", "", "y", "y"], pc_awake=False, wakes=False)
        self.assertEqual(self.ssh_calls, [])
        text = "\n".join(self.output)
        self.assertIn("needs the PC to be on", text)
        self.assertIn("SSH to the PC doesn't work yet", text)
        self.assertNotIn("The config is complete", text)

    def test_ssh_not_woken_if_declined(self):
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "1", "", "n", "", "y", "n"], pc_awake=False)
        self.assertEqual((self.wake_calls, self.ssh_calls, self.service_calls), ([], [], []))

    def test_failed_ssh_is_not_called_complete(self):
        with open(self.path) as f:
            text = f.read().replace("pc_addresses = []", 'pc_addresses = ["12:00"]')
        with open(self.path, "w") as f:
            f.write(config.set_config_value(text, "pi_address", "40:00"))
        self.setup(["192.168.1.50", "aa:bb:cc:dd:ee:01", "", "", "", "", "1", "", "n", "", "y"], ssh_works=False)
        text = "\n".join(self.output)
        self.assertIn("SSH to the PC doesn't work yet", text)
        self.assertNotIn("The config is complete", text)

    def test_ssh_problem_advice(self):
        p = setup_pc.ssh_problem
        self.assertIn("asleep or off", p("ssh: connect to host 192.168.1.50 port 22: No route to host"))
        self.assertIn("firewall-cmd", p("ssh: connect to host 192.168.1.50 port 22: No route to host"))
        self.assertIn("enable --now sshd", p("ssh: connect to host 192.168.1.50 port 22: Connection refused"))
        self.assertIn("restorecon", p("deck@192.168.1.50: Permission denied (publickey,password)."))
        self.assertIn("IP address", p("ssh: Could not resolve hostname gaming-pc: Name or service not known"))
        self.assertIn("password", p("something else"))

    def test_wake_pc_resends_until_the_pc_answers(self):
        clock, sends, polls = Clock(), [], []

        def answers(host):
            polls.append(clock.t)
            return len(polls) > 10                      # awake after about 20 seconds

        def sleep(seconds):
            clock.t += seconds
        self.assertTrue(setup_pc.wake_pc("192.168.1.50", lambda: sends.append(clock.t), lambda line: None,
                                          answers=answers, clock=clock, sleep=sleep))
        self.assertEqual(len(sends), 2)                 # once, then again after 15 seconds

    def test_wake_pc_gives_up(self):
        clock, lines = Clock(), []
        ok = setup_pc.wake_pc("192.168.1.50", lambda: None, lines.append, answers=lambda host: False,
                               clock=clock, sleep=lambda seconds: setattr(clock, "t", clock.t + seconds))
        self.assertFalse(ok)
        self.assertIn("didn't wake up", "\n".join(lines))

    def test_set_value_writes_a_list(self):
        new = config.set_config_value('pc_addresses = []                    # REQUIRED\n', "pc_addresses", ["12:00"])
        self.assertTrue(new.startswith('pc_addresses = ["12:00"]'))

    def test_unset_value_comments_out(self):
        text = 'soundbar_address = "10:00"\nb = 1\n'
        self.assertEqual(config.unset_config_value(text, "soundbar_address"), '# soundbar_address = "10:00"\nb = 1\n')


SCAN_OUTPUT = """opening a connection to the CEC adapter...
requesting CEC bus information ...
CEC bus information
===================
device #0: TV
address:       0.0.0.0
active source: no
vendor:        Samsung
osd string:    TV
CEC version:   1.4
power status:  on


device #1: Recorder 1
address:       4.0.0.0
vendor:        Pulse Eight
osd string:    CECTester


device #4: Playback 1
address:       1.1.0.0
vendor:        Google
osd string:    Chromecast


device #5: Audio
address:       1.0.0.0
vendor:        Samsung
osd string:    Soundbar

currently active source: unknown (-1)
"""


class CecSetupTests(unittest.TestCase):
    """couchlink --setup-cec, with a scripted bus and answers instead of a TV and a person."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "config.toml")
        shutil.copy(os.path.join(HERE, "couchlink.example.toml"), self.path)
        with open(self.path) as f:
            text = f.read()
        text = config.set_config_value(text, "pc_host", "192.168.1.50")
        text = config.set_config_value(text, "pc_mac", "aa:bb:cc:dd:ee:01")
        with open(self.path, "w") as f:
            f.write(text)
        self.output, self.service_calls = [], []

    def run_setup(self, answers, heard=(), scan=SCAN_OUTPUT, running=True):
        answers, heard = list(answers), list(heard)

        def ask(question):
            self.assertTrue(answers, f"unexpected question: {question}")
            return answers.pop(0)

        def watch(command, seconds, wanted, out):
            while heard:
                addr = heard.pop(0)
                if wanted(addr):
                    return addr
            return None

        def service(action):
            self.service_calls.append(action)
            return running
        code = setup_cec.run_cec_setup(self.path, ask=ask, out=self.output.append,
                                       scan=lambda command: cec.parse_scan(scan), watch=watch, service=service)
        self.assertEqual(answers, [], "not every answer was used")
        settings, problems = config.load_config(self.path)
        self.assertEqual(problems, [])
        return code, settings

    def test_parse_scan(self):
        d = cec.parse_scan(SCAN_OUTPUT)
        self.assertEqual([(x["logical"], x["address"]) for x in d], [(0, "00:00"), (1, "40:00"), (4, "11:00"), (5, "10:00")])
        self.assertEqual(d[3]["osd"], "Soundbar")

    def test_announced_address(self):
        self.assertEqual(cec.announced_address(msg("5f:81:12:00")[1]), "12:00")
        self.assertEqual(cec.announced_address(msg("0f:80:10:00:20:00")[1]), "20:00")
        self.assertIsNone(cec.announced_address(msg("05:90:00")[1]))
        self.assertIsNone(cec.announced_address(None))

    def test_pc_behind_soundbar_heard(self):
        # The TV announces the soundbar's input first, then the soundbar the PC's.
        code, s = self.run_setup(["3", "y", "y", "y", "1"], heard=["10:00", "12:00"])
        self.assertEqual(code, 0)
        self.assertEqual((s["pc_addresses"], s["pi_address"], s["soundbar_address"]), (["12:00"], "40:00", "10:00"))
        self.assertEqual((s["on_pc_sleep"], s["pc_sleep_switch_to"]), ("switch", "11:00"))
        self.assertEqual(self.service_calls, ["stop", "start"])
        self.assertIn("The config is complete", "\n".join(self.output))
        self.assertTrue(os.path.exists(self.path + ".bak"))

    def test_tv_only_typed_input(self):
        code, s = self.run_setup(["1", "2", "n"], heard=[])          # nothing heard: type the input
        self.assertEqual((s["pc_addresses"], s["pi_address"], s["soundbar_address"]), (["20:00"], "40:00", None))
        self.assertEqual(s["on_pc_sleep"], "nothing")

    def test_soundbar_on_arc_pc_on_tv(self):
        code, s = self.run_setup(["2", "y", "y", "n"], heard=["30:00"])
        self.assertEqual((s["pc_addresses"], s["soundbar_address"]), (["30:00"], "10:00"))

    def test_typed_input_behind_soundbar(self):
        code, s = self.run_setup(["3", "y", "n", "2", "n"], heard=["13:00"])   # wrong one heard
        self.assertEqual(s["pc_addresses"], ["12:00"])

    def test_tv_only_comments_out_an_old_soundbar(self):
        with open(self.path) as f:
            text = config.set_config_value(f.read(), "soundbar_address", "10:00")
        with open(self.path, "w") as f:
            f.write(text)
        code, s = self.run_setup(["1", "y", "n"], heard=["20:00"])
        self.assertIsNone(s["soundbar_address"])

    def test_sleep_target_heard(self):
        code, s = self.run_setup(["1", "y", "y", "3"], heard=["20:00", "30:00"])   # 3: listen
        self.assertEqual((s["on_pc_sleep"], s["pc_sleep_switch_to"]), ("switch", "30:00"))

    def test_declining_switch_turns_it_off(self):
        with open(self.path) as f:
            text = config.set_config_value(f.read(), "on_pc_sleep", "switch")
        text = config.set_config_value(text, "pc_sleep_switch_to", "11:00")
        with open(self.path, "w") as f:
            f.write(text)
        code, s = self.run_setup(["1", "y", "n"], heard=["20:00"])
        self.assertEqual(s["on_pc_sleep"], "nothing")

    def test_pi_missing_from_scan_is_asked(self):
        scan = SCAN_OUTPUT.replace("device #1: Recorder 1\naddress:       4.0.0.0", "")
        code, s = self.run_setup(["1", "4", "y", "n"], heard=["20:00"], scan=scan)
        self.assertEqual(s["pi_address"], "40:00")

    def fake_cec_client(self, body):
        path = os.path.join(self.tmp, "cec-client")
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys, time\n" + body)
        os.chmod(path, 0o755)
        return [path]

    @unittest.skipIf(os.name != "posix", "runs a fake cec-client script")
    def test_cec_scan_runs_cec_client(self):
        command = self.fake_cec_client(
            "assert sys.argv[1:] == ['-s', '-d', '1'], sys.argv\n"
            "assert sys.stdin.read().strip() == 'scan'\n"
            f"print({SCAN_OUTPUT!r})\n")
        self.assertEqual([d["address"] for d in cec.cec_scan(command)], ["00:00", "40:00", "11:00", "10:00"])

    @unittest.skipIf(os.name != "posix", "runs a fake cec-client script")
    def test_cec_watch_returns_the_first_wanted_input(self):
        command = self.fake_cec_client(
            "print('opening a connection to the CEC adapter...', flush=True)\n"
            "print('waiting for input', flush=True)\n"
            "print('TRAFFIC: [ 1]\\t>> 0f:80:40:00:10:00', flush=True)\n"   # the soundbar: not wanted
            "print('TRAFFIC: [ 2]\\t>> 5f:81:12:00', flush=True)\n"
            "time.sleep(30)\n")
        lines = []
        addr = cec.cec_watch(command, 10, lambda a: a != "10:00", out=lines.append)
        self.assertEqual(addr, "12:00")
        self.assertIn("Listening now", lines[0])

    @unittest.skipIf(os.name != "posix", "runs a fake cec-client script")
    def test_cec_watch_gives_up(self):
        command = self.fake_cec_client("print('waiting for input', flush=True)\ntime.sleep(30)\n")
        self.assertIsNone(cec.cec_watch(command, 1, lambda a: True, out=lambda line: None))

    def test_no_devices(self):
        code, _ = self.run_setup(["1"], scan="")
        self.assertEqual(code, 1)
        self.assertIn("No CEC devices answered", "\n".join(self.output))
        self.assertEqual(self.service_calls, ["stop", "start"])        # started again anyway
        with open(self.path) as f:
            self.assertIn("pc_addresses = []", f.read())            # nothing written

    def test_service_left_stopped_if_it_was(self):
        self.run_setup(["1", "y", "n"], heard=["20:00"], running=False)
        self.assertEqual(self.service_calls, ["stop"])
