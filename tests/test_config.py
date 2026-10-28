# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: checking, writing and bringing old configs up to date."""

import os
import tempfile
import unittest

from helpers import BASE, HERE, OLD_CONFIG, out
from couchlink import config


class ConfigTests(unittest.TestCase):
    def test_defaults_rejected(self):
        _, problems = config.validate(dict(config.DEFAULTS))
        text = " ".join(problems)
        for key in ("pc_host", "pc_mac", "pc_addresses", "pi_address"):
            self.assertIn(key, text)

    def test_address_formats(self):
        self.assertEqual(config.normalize_addr("3.0.0.0"), "30:00")
        self.assertEqual(config.normalize_addr("30:00"), "30:00")
        self.assertEqual(config.normalize_addr("2200"), "22:00")
        self.assertIsNone(config.normalize_addr("3.0"))

    def test_overlap_rejected(self):
        s = dict(BASE, soundbar_address="30:00")
        _, problems = config.validate(s)
        self.assertTrue(any("also listed" in p for p in problems))

    def test_toml_loading(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[pc]\npc_host = "10.0.0.5"\npc_mac = "aa:bb:cc:dd:ee:02"\n'
                    '[cec]\npc_addresses = ["3.0.0.0"]\npi_address = "40:00"\ntypo_key = 1\n')
        settings, problems = config.load_config(f.name)
        os.unlink(f.name)
        self.assertTrue(any("typo_key" in p for p in problems))
        cfg, more = config.validate(settings)
        self.assertEqual(more, [])
        self.assertEqual(cfg.pc_addresses, ["30:00"])

    def test_example_config_is_valid_once_filled(self):
        path = os.path.join(HERE, "couchlink.example.toml")
        if not os.path.exists(path):
            self.skipTest("example config not present")
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        _, problems = config.validate(settings)
        self.assertTrue(problems, "example ships with placeholders and should not validate as-is")


class MigrateTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "couchlink.example.toml")) as f:
            self.example = f.read()

    def migrate(self, text):
        new, changes, problems = config.migrate_config_text(text, self.example)
        return new, " | ".join(changes), problems

    def test_old_config_is_brought_up_to_date(self):
        new, changes, problems = self.migrate(OLD_CONFIG)
        self.assertEqual(problems, [])
        self.assertIn("Renamed pc_adresses to pc_addresses (it looks like a typo)", changes)
        self.assertIn('Changed select_soundbar_audio from "false" to false', changes)
        self.assertIn('Changed away_debounce from "5" to 5', changes)
        self.assertIn("Commented out old_thing", changes)
        self.assertIn("Added watch_pc_power to [behavior]", changes)
        self.assertIn("Added tv_boot_timeout to [timing]", changes)
        self.assertIn('pc_addresses = ["12:00"]               # REQUIRED', new)    # comment kept
        self.assertIn("# old_thing = 1", new)
        settings = config.tomllib.loads(new)
        self.assertIs(settings["behavior"]["watch_pc_power"], True)
        self.assertIs(settings["behavior"]["select_soundbar_audio"], False)
        self.assertEqual(settings["timing"]["away_debounce"], 5)

    def test_second_run_changes_nothing(self):
        new, _, _ = self.migrate(OLD_CONFIG)
        again, changes, _ = self.migrate(new)
        self.assertEqual(changes, "")
        self.assertEqual(again, new)

    def test_missing_section_is_added(self):
        new, changes, _ = self.migrate('pc_host = "192.168.1.50"\n')
        self.assertIn("\n[timing]\n", new)
        self.assertIn("Added tv_boot_timeout to [timing]", changes)

    def test_required_settings_are_reported_not_added(self):
        new, changes, problems = self.migrate('pc_host = "192.168.1.50"\n')
        self.assertNotIn("pc_mac", changes)
        self.assertTrue(any("pc_mac" in p for p in problems))

    def test_renamed_and_removed_settings(self):
        renamed, removed = config.RENAMED_SETTINGS, config.REMOVED_SETTINGS
        config.RENAMED_SETTINGS = {"sleep_action": "on_pc_sleep"}
        config.REMOVED_SETTINGS = {"legacy_mode": "Removed in 3.0"}
        self.addCleanup(setattr, config, "RENAMED_SETTINGS", renamed)
        self.addCleanup(setattr, config, "REMOVED_SETTINGS", removed)
        new, changes, _ = self.migrate('sleep_action = "standby"\nlegacy_mode = true\n')
        self.assertIn("Renamed sleep_action to on_pc_sleep", changes)
        self.assertIn("Commented out legacy_mode: Removed in 3.0", changes)
        self.assertIn('on_pc_sleep = "standby"', new)

    def test_broken_toml_is_left_alone(self):
        text = "[pc]\npc_host = \n"
        new, changes, problems = self.migrate(text)
        self.assertEqual(new, text)
        self.assertTrue(problems and "not valid TOML" in problems[0])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w") as f:
                f.write(OLD_CONFIG)
            out = []
            self.assertEqual(config.migrate_config(path, dry_run=True, out=out.append), 0)
            with open(path) as f:
                self.assertEqual(f.read(), OLD_CONFIG)
            self.assertFalse(os.path.exists(path + ".bak"))
            self.assertTrue(out[0].startswith("Would make these changes"))
            self.assertEqual(config.migrate_config(path, out=out.append), 0)
            with open(path + ".bak") as f:
                self.assertEqual(f.read(), OLD_CONFIG)
