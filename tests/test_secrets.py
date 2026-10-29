# SPDX-License-Identifier: GPL-3.0-or-later
"""secrets.toml: the settings page's password hash and the API keys, kept apart from
config.toml, moving them there from older installs, and --share-config."""

import contextlib
import io
import os
import queue
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from helpers import HERE, assert_mode
from couchlink import VERSION, admin, auth, cli, config

PASSWORD = "sofa-console"
OLD_PASSWORD = auth.hash_password("old-password", iterations=1000)
PHONE = auth.hash_api_key("phone key")


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def complete_config(test, password=None, keys=None):
    """A complete config, as an older install has it: the hashes, if any, in config.toml."""
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d)
    path = os.path.join(d, "config.toml")
    text = read(os.path.join(HERE, "couchlink.example.toml"))
    for key, value in (("pc_host", "192.168.1.50"), ("pc_mac", "aa:bb:cc:dd:ee:01"),
                       ("pc_addresses", ["12:00"]), ("pi_address", "40:00")):
        text = config.set_config_value(text, key, value)
    if password:
        text = config.set_config_value(text, "admin_password_hash", password)
    for name, hashed in (keys or {}).items():
        text = config.set_api_key(text, name, hashed)
    write(path, text)
    return path


class ReadingTests(unittest.TestCase):
    def test_secrets_come_from_secrets_toml(self):
        path = complete_config(self)
        write(config.secrets_path(path), config.SECRETS_HEADER + f'admin_password_hash = "{OLD_PASSWORD}"\n\n'
              f'[api_keys]\nphone = "{PHONE}"\n')
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        self.assertEqual(settings["admin_password_hash"], OLD_PASSWORD)
        self.assertEqual(settings["api_keys"], {"phone": PHONE})
        self.assertIsNone(config.secrets_warning(path))

    def test_no_secrets_file_is_fine(self):
        settings, problems = config.load_config(complete_config(self))
        self.assertEqual((problems, settings["admin_password_hash"], settings["api_keys"]), ([], "", {}))

    def test_unknown_settings_in_secrets_toml_are_reported(self):
        path = complete_config(self)
        write(config.secrets_path(path), 'pc_host = "192.168.1.50"\n')
        self.assertTrue(any("Unknown setting 'pc_host'" in p for p in config.load_config(path)[1]))

    def test_check_config_shows_set_or_not_set_never_the_hash(self):
        path = complete_config(self)
        write(config.secrets_path(path), f'admin_password_hash = "{OLD_PASSWORD}"\n\n[api_keys]\nphone = "{PHONE}"\n')
        lines = check_config(path)
        self.assertIn("admin_password_hash = set", lines)
        self.assertIn("api_keys = phone (set)", lines)
        self.assertFalse(any(OLD_PASSWORD in line or PHONE in line for line in lines))
        lines = check_config(complete_config(self))
        self.assertIn("admin_password_hash = not set", lines)
        self.assertIn("api_keys = not set", lines)


def check_config(path):
    """couchlink --check-config's output lines."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["--config", path, "--check-config"])
    return out.getvalue().splitlines()


class FallbackTests(unittest.TestCase):
    def test_hashes_still_in_config_toml_keep_working_with_one_warning(self):
        path = complete_config(self, OLD_PASSWORD, {"phone": PHONE})
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        self.assertEqual((settings["admin_password_hash"], settings["api_keys"]), (OLD_PASSWORD, {"phone": PHONE}))
        lines = check_config(path)
        warnings = [line for line in lines if line.startswith("[WARN]")]
        self.assertEqual(len(warnings), 1, lines)
        self.assertIn("They still work", warnings[0])
        self.assertIn("sudo couchlink --migrate-config", warnings[0])

    def test_secrets_toml_wins_and_the_warning_says_so(self):
        newer = auth.hash_password("newer-password", iterations=1000)
        path = complete_config(self, OLD_PASSWORD, {"phone": PHONE})
        write(config.secrets_path(path), f'admin_password_hash = "{newer}"\n')
        settings, _ = config.load_config(path)
        self.assertEqual(settings["admin_password_hash"], newer)
        self.assertEqual(settings["api_keys"], {"phone": PHONE})       # only in config.toml, so still used
        warning = config.secrets_warning(path)
        self.assertIn("secrets.toml has the settings page's password hash too, and those are the ones used", warning)

    def test_an_empty_secrets_toml_doesnt_hide_the_old_values(self):
        path = complete_config(self, OLD_PASSWORD, {"phone": PHONE})
        write(config.secrets_path(path), config.SECRETS_HEADER)          # as the installer creates it
        settings, _ = config.load_config(path)
        self.assertEqual((settings["admin_password_hash"], settings["api_keys"]), (OLD_PASSWORD, {"phone": PHONE}))


class MoveTests(unittest.TestCase):
    def test_migrate_moves_the_hashes(self):
        old_key = auth.hash_api_key("from 2.6")
        path = complete_config(self, OLD_PASSWORD, {"phone": PHONE})
        write(path, read(path).replace("[timing]\n", f'[timing]\n# api_key_hash = "{old_key}"\n'))   # an old comment
        before = read(path)
        lines = []
        self.assertEqual(config.migrate_config(path, out=lines.append), 0)
        text, secrets = read(path), read(config.secrets_path(path))
        for hashed in (OLD_PASSWORD, PHONE, old_key):
            self.assertNotIn(hashed, text)
        self.assertIn(OLD_PASSWORD, secrets)
        self.assertIn(PHONE, secrets)
        self.assertTrue(secrets.startswith("# CouchLink's secrets"))
        self.assertIn("# admin_password_hash is in secrets.toml now", text)       # commented out, not deleted
        self.assertIn("# phone is in secrets.toml now", text)
        self.assertEqual(read(path + ".bak"), before)
        self.assertIn("  - Moved admin_password_hash to secrets.toml", lines)
        self.assertIn("  - Moved the API key phone to secrets.toml", lines)
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        self.assertEqual((settings["admin_password_hash"], settings["api_keys"]), (OLD_PASSWORD, {"phone": PHONE}))
        self.assertIsNone(config.secrets_warning(path))
        assert_mode(self, path, 0o644)
        assert_mode(self, path + ".bak", 0o600)     # it still has the hashes
        assert_mode(self, config.secrets_path(path), 0o600)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        path = complete_config(self, OLD_PASSWORD, {"phone": PHONE})
        config.migrate_config(path, out=lambda line: None)
        files = (read(path), read(path + ".bak"), read(config.secrets_path(path)))
        lines = []
        self.assertEqual(config.migrate_config(path, out=lines.append), 0)
        self.assertEqual((read(path), read(path + ".bak"), read(config.secrets_path(path))), files)
        self.assertTrue(any("Nothing to change" in line for line in lines), lines)

    def test_dry_run_only_shows_the_move(self):
        path = complete_config(self, OLD_PASSWORD, {"phone": PHONE})
        before = read(path)
        lines = []
        self.assertEqual(config.migrate_config(path, dry_run=True, out=lines.append), 0)
        self.assertEqual(read(path), before)
        self.assertFalse(os.path.exists(config.secrets_path(path)))
        self.assertFalse(os.path.exists(path + ".bak"))
        self.assertIn("  - Moved admin_password_hash to secrets.toml", lines)

    def test_the_2_6_single_key_moves_as_default(self):
        old_key = auth.hash_api_key("from 2.6")
        path = complete_config(self)
        write(path, read(path).replace("[timing]\n", f'[timing]\napi_key_hash = "{old_key}"\n'))
        config.migrate_config(path, out=lambda line: None)
        self.assertNotIn(old_key, read(path))
        self.assertEqual(config.load_config(path)[0]["api_keys"], {"default": old_key})

    def test_a_key_in_both_files_keeps_secrets_tomls(self):
        newer = auth.hash_api_key("newer")
        path = complete_config(self, keys={"phone": PHONE})
        write(config.secrets_path(path), f'[api_keys]\nphone = "{newer}"\n')
        lines = []
        config.migrate_config(path, out=lines.append)
        self.assertEqual(config.load_config(path)[0]["api_keys"], {"phone": newer})
        self.assertNotIn(PHONE, read(path))
        self.assertTrue(any("secrets.toml already has a key with that name" in line for line in lines), lines)

    def test_a_config_without_secrets_is_readable_by_everyone(self):
        path = complete_config(self)
        config.save_config(path, read(path), read(path) + "\n")
        assert_mode(self, path, 0o644)
        config.save_config(path, read(path), config.set_api_key(read(path), "phone", PHONE))
        assert_mode(self, path, 0o600)          # it holds a hash, so it stays root only


class SettingsPageTests(unittest.TestCase):
    """The settings page and the API keys after the move."""

    def setUp(self):
        self.path = complete_config(self, auth.hash_password(PASSWORD), {"phone": PHONE})
        config.migrate_config(self.path, out=lambda line: None)
        self.controller = SimpleNamespace(cfg=SimpleNamespace(api_keys={}))
        self.page = admin.Admin(self.controller, queue.Queue(), self.path, auth.KeyGuard())

    def login(self, password=PASSWORD):
        status, payload, headers, _ = self.page.post("/admin/login", {"password": password}, {}, "192.168.1.20")
        cookies = {"Cookie": headers[0][1].split(";")[0]} if headers else {}
        return status, cookies

    def test_logins_work_and_end_when_the_password_changes(self):
        status, cookies = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(self.page.logged_in(cookies))
        status, payload, headers, _ = self.page.post("/admin/password", {"current": PASSWORD, "new": "new-password"},
                                                     cookies, "192.168.1.20")
        self.assertEqual((status, payload["result"]), (200, "changed"))
        self.assertFalse(self.page.logged_in(cookies))          # the old cookie no longer works
        self.assertEqual(self.login()[0], 401)
        self.assertEqual(self.login("new-password")[0], 200)
        self.assertNotIn("pbkdf2", read(self.path))
        self.assertIn("pbkdf2", read(config.secrets_path(self.path)))

    def test_api_keys_are_made_and_revoked_in_secrets_toml(self):
        _, cookies = self.login()
        status, payload, _, _ = self.page.post("/admin/keys/new", {"name": "home-assistant"}, cookies, "x")
        self.assertEqual(status, 200)
        key = payload["key"]
        cfg, problems = config.validate(config.load_config(self.path)[0])
        self.assertEqual(problems, [])
        self.assertEqual(auth.find_api_key(key, cfg.api_keys), "home-assistant")      # what the API checks
        self.assertEqual(auth.find_api_key(key, self.controller.cfg.api_keys), "home-assistant")
        self.assertNotIn(auth.hash_api_key(key), read(self.path))
        self.page.post("/admin/keys/revoke", {"name": "phone"}, cookies, "x")
        self.assertEqual(sorted(config.load_config(self.path)[0]["api_keys"]), ["home-assistant"])

    def test_a_key_made_before_the_move_is_moved_first(self):
        path = complete_config(self, auth.hash_password(PASSWORD), {"phone": PHONE})   # not moved yet
        page = admin.Admin(self.controller, queue.Queue(), path, auth.KeyGuard())
        status, payload, headers, _ = page.post("/admin/login", {"password": PASSWORD}, {}, "x")
        cookies = {"Cookie": headers[0][1].split(";")[0]}
        page.post("/admin/keys/revoke", {"name": "phone"}, cookies, "x")
        settings = config.load_config(path)[0]
        self.assertEqual(settings["api_keys"], {})             # it can't come back from config.toml
        self.assertNotIn(PHONE, read(path))
        self.assertIsNone(config.secrets_warning(path))
        self.assertTrue(page.logged_in(cookies))               # the password moved, so the login still works


class ShareConfigTests(unittest.TestCase):
    def test_the_addresses_are_hidden(self):
        path = complete_config(self, OLD_PASSWORD, {"phone": PHONE})
        write(path, config.set_config_value(read(path), "wol_broadcast", "192.168.1.255"))
        lines = []
        self.assertEqual(cli.share_config(path, out=lines.append), 0)
        shared = "\n".join(lines)
        self.assertIn(f"CouchLink {VERSION}", lines[0])
        for secret in ("192.168.1.50", "aa:bb:cc:dd:ee:01", "192.168.1.255", OLD_PASSWORD, PHONE):
            self.assertNotIn(secret, shared)
        self.assertIn('pc_host = "<hidden>"', shared)
        self.assertIn('pi_address = "40:00"', shared)            # the rest is shown as it is


if __name__ == "__main__":
    unittest.main()
