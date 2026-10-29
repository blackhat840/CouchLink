# SPDX-License-Identifier: GPL-3.0-or-later
"""Saving the config safely: a power cut partway through a save, two saves at once, and
a long --setup run that overlaps a save from the settings page."""

import os
import queue
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from helpers import HERE, assert_private
from couchlink import admin, auth, config, setup_pc


def example_copy(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d)
    path = os.path.join(d, "config.toml")
    shutil.copy(os.path.join(HERE, "couchlink.example.toml"), path)
    return path


def complete_copy(test):
    """A config the settings page can save to: the required settings are filled in."""
    path = example_copy(test)
    text = read(path)
    for key, value in (("pc_host", "192.168.1.50"), ("pc_mac", "aa:bb:cc:dd:ee:01"),
                       ("pc_addresses", ["12:00"]), ("pi_address", "40:00")):
        text = config.set_config_value(text, key, value)
    with open(path, "w") as f:
        f.write(text)
    return path


def read(path):
    with open(path) as f:
        return f.read()


def leftovers(path):
    """Files next to the config other than the config, its .bak and its lock file."""
    return sorted(set(os.listdir(os.path.dirname(path))) - {"config.toml", "config.toml.bak", "config.toml.lock"})


class AtomicWriteTests(unittest.TestCase):
    def test_a_failed_rename_keeps_the_old_config(self):
        path = example_copy(self)
        before = read(path)
        real_replace = os.replace

        def power_cut(src, dst):
            if dst == path:
                raise OSError("power cut")
            return real_replace(src, dst)
        with mock.patch("os.replace", power_cut):
            with self.assertRaises(OSError):
                config.save_config(path, before, before + "raw_debug = false\n")
        self.assertEqual(read(path), before)
        self.assertEqual(leftovers(path), [])

    def test_a_failed_write_keeps_the_old_config(self):
        path = example_copy(self)
        before = read(path)
        with mock.patch("os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.save_config(path, before, "")
        self.assertEqual(read(path), before)
        self.assertEqual(leftovers(path), [])

    def test_a_failed_bak_leaves_the_config_alone(self):
        path = example_copy(self)
        before = read(path)
        real_replace = os.replace

        def bak_fails(src, dst):
            if dst.endswith(".bak"):
                raise OSError("power cut")
            return real_replace(src, dst)
        with mock.patch("os.replace", bak_fails):
            with self.assertRaises(OSError):
                config.save_config(path, before, "raw_debug = false\n")
        self.assertEqual(read(path), before)
        self.assertEqual(leftovers(path), [])

    def test_saved_files_are_private(self):
        path = example_copy(self)
        os.chmod(path, 0o644)           # a new file takes its place, so old permissions don't carry over
        config.save_config(path, read(path), read(path) + "raw_debug = false\n")
        code_path = auth.setup_code_path(path)
        auth.make_setup_code(path)
        for p in (path, path + ".bak", code_path):
            assert_private(self, p)
        self.assertEqual(leftovers(path), ["setup-code"])


class LockTests(unittest.TestCase):
    def test_two_saves_at_once_both_end_up_in_the_file(self):
        path = complete_copy(self)
        page = admin.Admin(None, queue.Queue(), path, auth.KeyGuard())
        real_set = admin.set_config_value
        start = threading.Barrier(2)
        answers = []

        def slow_set(text, key, value):
            time.sleep(0.05)            # so the two saves would overlap without the lock
            return real_set(text, key, value)

        def save(values):
            start.wait()
            answers.append(page.save_settings({"values": values}, "192.168.1.20")[1]["result"])
        with mock.patch.object(admin, "set_config_value", slow_set):
            threads = [threading.Thread(target=save, args=(v,)) for v in ({"ssh_user": "gamer"}, {"pc_name": "Den"})]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(answers, ["saved", "saved"])
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        self.assertEqual((settings["ssh_user"], settings["pc_name"]), ("gamer", "Den"))

    def test_the_change_gets_the_text_as_it_is_now(self):
        path = example_copy(self)
        config.update_config(path, lambda text: config.set_config_value(text, "pc_name", "Den"))
        seen = []
        config.update_config(path, lambda text: seen.append(text))
        self.assertIn('pc_name = "Den"', seen[0])


class LongSetupTests(unittest.TestCase):
    """--setup reads the config when it starts and saves minutes later."""
    ANSWERS = ["192.168.1.50", "", "", "", "", "", "2", "", "n", "", "n"]

    def run_setup(self, meanwhile):
        self.path = example_copy(self)
        answers, self.output = list(self.ANSWERS), []

        def ask(question):
            if len(answers) == 6:       # partway through, something else saves the config
                meanwhile(self.path)
            return answers.pop(0)
        return setup_pc.run_setup(self.path, ask=ask, out=self.output.append,
                                  mac_finder=lambda host: "aa:bb:cc:dd:ee:01", ssh_setup=None,
                                  pc_check=lambda host: True, waker=None, service=lambda action: True,
                                  secret=lambda question: "")

    def test_a_settings_page_change_survives(self):
        def settings_page(path):
            config.update_config(path, lambda text: config.set_api_key(
                config.set_config_value(config.set_config_value(text, "raw_debug", False), "pc_host", "192.168.1.60"),
                "phone", auth.hash_api_key("k")))
        self.assertEqual(self.run_setup(settings_page), 0)
        settings, problems = config.load_config(self.path)
        self.assertEqual(problems, [])
        self.assertEqual(settings["pc_host"], "192.168.1.50")       # answered, so the answer wins
        self.assertEqual(settings["on_pc_sleep"], "standby")
        self.assertIs(settings["raw_debug"], False)                 # not asked, so the newer value stays
        self.assertIn("phone", settings["api_keys"])
        self.assertIn("Kept from changes saved while you answered: api_keys, raw_debug", self.output)

    def test_nothing_is_saved_if_the_answers_cant_be_added(self):
        def broken(path):
            with open(path, "w") as f:
                f.write("this isn't [ valid TOML\n")
        self.assertEqual(self.run_setup(broken), 1)
        self.assertEqual(read(self.path), "this isn't [ valid TOML\n")
        self.assertTrue(any(line.startswith("Nothing was saved: the config was changed while you answered")
                            for line in self.output), self.output)

    def test_an_unchanged_config_is_saved_as_before(self):
        self.assertEqual(self.run_setup(lambda path: None), 0)
        settings, _ = config.load_config(self.path)
        self.assertEqual(settings["pc_host"], "192.168.1.50")
        self.assertFalse(any(line.startswith("Kept") for line in self.output))


if __name__ == "__main__":
    unittest.main()
