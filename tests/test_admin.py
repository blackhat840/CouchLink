# SPDX-License-Identifier: GPL-3.0-or-later
"""The settings page: its password and login cookie, named API keys in the config,
and the page's requests over real HTTP."""

import json
import os
import queue
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from helpers import BASE, HERE, REAL, Sim, msg
from couchlink import auth, config, setup_pc, web

PASSWORD = "sofa-console"


def quick_hash(password):
    return auth.hash_password(password, iterations=1000)      # the real count is slow on purpose


class PasswordTests(unittest.TestCase):
    def test_hash_and_check(self):
        stored = auth.hash_password(PASSWORD, iterations=1000)
        self.assertRegex(stored, auth.PASSWORD_RE)
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(auth.check_password(PASSWORD, stored))
        self.assertFalse(auth.check_password("wrong", stored))
        self.assertFalse(auth.check_password("", stored))
        self.assertFalse(auth.check_password(PASSWORD, ""))
        self.assertNotEqual(stored, auth.hash_password(PASSWORD, iterations=1000))     # salted

    def test_session(self):
        secret = quick_hash(PASSWORD)
        token = auth.make_session(secret, now=1000)
        self.assertTrue(auth.session_ok(token, secret, now=2000))
        self.assertFalse(auth.session_ok(token, secret, now=1000 + auth.SESSION_DAYS * 86400 + 1))  # expired
        self.assertFalse(auth.session_ok(token, quick_hash(PASSWORD), now=2000))    # the password changed
        self.assertFalse(auth.session_ok("9999999999.forged", secret))
        self.assertFalse(auth.session_ok("", secret))
        self.assertFalse(auth.session_ok(token, ""))

    def test_cookie(self):
        self.assertEqual(auth.cookie({"Cookie": "a=1; couchlink_admin=xyz; b=2"}, "couchlink_admin"), "xyz")
        self.assertIsNone(auth.cookie({}, "couchlink_admin"))

    def test_find_api_key(self):
        keys = {"home-assistant": auth.hash_api_key("one"), "phone": auth.hash_api_key("two")}
        self.assertEqual(auth.find_api_key("two", keys), "phone")
        self.assertIsNone(auth.find_api_key("three", keys))
        self.assertIsNone(auth.find_api_key(None, keys))

    def test_set_admin_password(self):
        path = config_copy(self)
        answers, lines = [PASSWORD, PASSWORD], []
        self.assertEqual(setup_pc.set_admin_password(path, out=lines.append, secret=lambda q: answers.pop(0)), 0)
        stored = config.load_config(path)[0]["admin_password_hash"]
        self.assertTrue(auth.check_password(PASSWORD, stored))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        answers = ["short", "short", "a b c d e f", "different", "x", "y"]
        self.assertEqual(setup_pc.set_admin_password(path, out=lines.append, secret=lambda q: answers.pop(0)), 1)
        self.assertEqual(config.load_config(path)[0]["admin_password_hash"], stored)   # unchanged


def config_copy(test, extra=""):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d)
    path = os.path.join(d, "config.toml")
    with open(os.path.join(HERE, "couchlink.example.toml")) as f:
        text = f.read()
    for key, value in (("pc_host", "192.168.1.50"), ("pc_mac", "aa:bb:cc:dd:ee:01"), ("pi_address", "40:00")):
        text = config.set_config_value(text, key, value)
    text = text.replace("pc_addresses = []", 'pc_addresses = ["12:00"]')
    with open(path, "w") as f:
        f.write(text + extra)
    return path


class ApiKeysConfigTests(unittest.TestCase):
    def test_table_is_kept_whole(self):
        path = config_copy(self)
        with open(path) as f:
            text = config.set_api_key(f.read(), "home-assistant", auth.hash_api_key("k"))
        with open(path, "w") as f:
            f.write(text)
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        self.assertEqual(settings["api_keys"], {"home-assistant": auth.hash_api_key("k")})
        self.assertNotIn("home-assistant", settings)            # not flattened into the settings

    def test_set_and_remove(self):
        text = "[behavior]\nraw_debug = false\n"
        text = config.set_api_key(text, "home-assistant", auth.hash_api_key("a"))
        text = config.set_api_key(text, "phone", auth.hash_api_key("b"))
        text = config.set_api_key(text, "home-assistant", auth.hash_api_key("c"))   # replaced in place
        settings, _ = config.settings_from_text(text)
        self.assertEqual(settings["api_keys"], {"home-assistant": auth.hash_api_key("c"),
                                                "phone": auth.hash_api_key("b")})
        text = config.remove_api_key(text, "phone")
        self.assertEqual(config.settings_from_text(text)[0]["api_keys"], {"home-assistant": auth.hash_api_key("c")})
        self.assertIn("raw_debug = false", text)

    def test_old_single_key_still_works_then_is_moved(self):
        old = auth.hash_api_key("from 2.6")
        path = config_copy(self)
        with open(path) as f:
            text = f.read().replace("[timing]\n", f'[timing]\napi_key_hash = "{old}"\n')
        with open(path, "w") as f:
            f.write(text)
        settings, problems = config.load_config(path)
        self.assertEqual(problems, [])
        self.assertEqual(settings["api_keys"], {"default": old})   # works before --migrate-config
        lines = []
        config.migrate_config(path, out=lines.append)
        settings, problems = config.load_config(path)
        self.assertEqual((problems, settings["api_keys"]), ([], {"default": old}))
        with open(path) as f:
            text = f.read()
        self.assertIn(f'# api_key_hash = "{old}"', text)
        self.assertTrue(any("as the key named default" in line for line in lines))

    def test_new_settings_go_before_the_keys(self):
        text = '[diagnostics]\nraw_debug = true\n\n[api_keys]\nphone = "%s"\n' % auth.hash_api_key("k")
        text = config.set_config_value(text, "cec_client_command", ["cec-client"])
        settings, problems = config.settings_from_text(text)
        self.assertEqual(problems, [])
        self.assertEqual(settings["cec_client_command"], ["cec-client"])
        self.assertEqual(settings["api_keys"], {"phone": auth.hash_api_key("k")})

    def test_key_names_cant_be_setting_names(self):
        _, problems = config.validate(dict(BASE, api_keys={"pc_host": auth.hash_api_key("k")}))
        self.assertTrue(any("also the name of a setting" in p for p in problems))

    def test_saved_files_are_private(self):
        path = config_copy(self)
        config.save_config(path, "old\n", "new\n")
        for p in (path, path + ".bak"):
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


class AdminPageTests(unittest.TestCase):
    """The settings page over real HTTP."""

    def setUp(self):
        self.path = config_copy(self, "")
        with open(self.path) as f:
            text = config.set_config_value(f.read(), "admin_password_hash", quick_hash(PASSWORD))
        with open(self.path, "w") as f:
            f.write(text)
        settings, _ = config.load_config(self.path)
        self.sim = Sim(**dict(REAL, api_keys={}))
        self.events = queue.Queue()       # what the page asks of the main loop, such as a restart
        actions = queue.Queue()

        def worker():                     # the main loop: wake and sleep go to the controller
            while True:
                event = actions.get()
                if event is None:
                    return
                if event[0] == "action":
                    self.sim.ctl.handle(event)
                else:
                    self.events.put(event)
        threading.Thread(target=worker, daemon=True).start()
        self.server = web.start_dashboard(0, self.sim.ctl, actions, self.path)
        self.port = self.server.server_address[1]
        self.addCleanup(lambda: threading.Thread(target=self.server.shutdown, daemon=True).start())
        self.addCleanup(actions.put, None)
        self.cookie = None

    def request(self, path, body=None, page=True, cookie=True, key=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        req.add_header("Content-Type", "application/json")
        if page:
            req.add_header("Origin", f"http://127.0.0.1:{self.port}")
            req.add_header("Sec-Fetch-Site", "same-origin")
        if cookie and self.cookie:
            req.add_header("Cookie", self.cookie)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, headers, raw = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, e.headers, e.read()
        new = headers.get("Set-Cookie")
        if new:
            self.cookie = new.split(";")[0]
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode()

    def login(self, password=PASSWORD):
        return self.request("/admin/login", {"password": password})

    def text(self):
        with open(self.path) as f:
            return f.read()

    def test_page_and_script_load(self):
        status, page = self.request("/admin")
        self.assertEqual(status, 200)
        self.assertIn('<script src="/admin.js"></script>', page)
        self.assertEqual(self.request("/admin.js")[0], 200)

    def test_needs_a_login(self):
        status, body = self.request("/admin/api/state")
        self.assertEqual((status, body["result"]), (401, "login_needed"))
        status, body = self.request("/admin/settings", {"values": {"raw_debug": False}})
        self.assertEqual((status, body["result"]), (401, "login_needed"))

    def test_no_password_yet(self):
        with open(self.path) as f:
            text = config.unset_config_value(f.read(), "admin_password_hash")
        with open(self.path, "w") as f:
            f.write(text)
        status, body = self.request("/admin/api/state")
        self.assertEqual((status, body["result"]), (403, "no_password"))
        self.assertIn("sudo couchlink --admin-password", body["message"])

    def test_login_and_state(self):
        status, body = self.login("wrong")
        self.assertEqual((status, body["result"]), (401, "wrong_password"))
        self.assertIsNone(self.cookie)
        status, body = self.login()
        self.assertEqual((status, body["result"]), (200, "logged_in"))
        self.assertIsNotNone(self.cookie)
        status, body = self.request("/admin/api/state")
        self.assertEqual(status, 200)
        fields = {f["key"]: f for f in body["fields"]}
        self.assertEqual(fields["pc_host"]["value"], "192.168.1.50")
        self.assertEqual(fields["pc_addresses"]["value"], "12:00")
        self.assertNotIn("admin_password_hash", fields)
        self.assertNotIn("sha256", json.dumps(body))

    def test_wrong_passwords_lock_out(self):
        for _ in range(5):
            self.login("wrong")
        status, body = self.login()
        self.assertEqual((status, body["result"]), (429, "locked_out"))

    def test_other_sites_cant_use_the_login(self):
        self.login()
        status, body = self.request("/admin/settings", {"values": {"raw_debug": False}}, page=False)
        self.assertEqual((status, body["result"]), (403, "refused"))
        self.assertIn("raw_debug = true", self.text())          # unchanged

    def test_save_settings_restarts(self):
        self.login()
        status, body = self.request("/admin/settings", {"values": {
            "pc_name": "Living Room PC", "raw_debug": False, "away_debounce": 8, "pc_addresses": "12:00, 13:00",
            "on_pc_sleep": "switch", "pc_sleep_switch_to": "11:00"}})
        self.assertEqual((status, body["result"]), (200, "saved"))
        settings, problems = config.load_config(self.path)
        self.assertEqual(problems, [])
        self.assertEqual((settings["pc_name"], settings["raw_debug"], settings["away_debounce"]),
                         ("Living Room PC", False, 8))
        self.assertEqual(settings["pc_addresses"], ["12:00", "13:00"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertIn("# REQUIRED", self.text())                # comments kept
        kind, why = self.events.get(timeout=2)
        self.assertEqual(kind, "restart")
        self.assertIn("raw_debug", why)

    def test_bad_settings_are_not_saved(self):
        self.login()
        before = self.text()
        status, body = self.request("/admin/settings", {"values": {"pi_address": "zz", "pc_host": ""}})
        self.assertEqual((status, body["result"]), (400, "invalid"))
        self.assertTrue(any("pc_host" in p for p in body["problems"]))
        status, body = self.request("/admin/settings", {"values": {"on_pc_sleep": "switch", "pc_sleep_switch_to": ""}})
        self.assertEqual(status, 400)                           # switch needs an input to switch to
        status, body = self.request("/admin/settings", {"values": {"ssh_user": "deck", "dashboard_port": 1}})
        self.assertTrue(any("dashboard_port" in p for p in body["problems"]))   # not on the page
        self.assertEqual(self.text(), before)
        self.assertTrue(self.events.empty())

    def test_unchanged_settings_dont_restart(self):
        self.login()
        status, body = self.request("/admin/settings", {"values": {"pc_host": "192.168.1.50"}})
        self.assertEqual((status, body["result"]), (200, "unchanged"))
        self.assertTrue(self.events.empty())

    def test_keys_work_straight_away_and_can_be_revoked(self):
        self.login()
        status, body = self.request("/admin/keys/new", {"name": "home-assistant"})
        self.assertEqual((status, body["result"]), (200, "created"))
        key = body["key"]
        self.assertNotIn(key, self.text())
        self.assertEqual(self.request("/admin/keys/new", {"name": "home-assistant"})[0], 409)
        self.assertEqual(self.request("/admin/keys/new", {"name": "bad name!"})[0], 400)
        self.request("/admin/keys/new", {"name": "phone"})
        status, state = self.request("/admin/api/state")
        self.assertEqual(state["keys"], ["home-assistant", "phone"])
        # the API takes it with no restart, and the log says which key
        status, body = self.request("/api/session/end", {}, page=False, cookie=False, key=key)
        self.assertEqual((status, body["result"]), (200, "no_session"))
        status, body = self.request("/admin/keys/revoke", {"name": "home-assistant"})
        self.assertEqual(status, 200)
        status, body = self.request("/api/session/end", {}, page=False, cookie=False, key=key)
        self.assertEqual(status, 401)
        self.assertEqual(sorted(config.load_config(self.path)[0]["api_keys"]), ["phone"])
        self.assertTrue(self.events.empty())                    # keys need no restart

    def test_change_password(self):
        self.login()
        old_cookie = self.cookie
        status, body = self.request("/admin/password", {"current": "wrong", "new": "a new password"})
        self.assertEqual(status, 401)
        status, body = self.request("/admin/password", {"current": PASSWORD, "new": "short"})
        self.assertEqual(status, 400)
        status, body = self.request("/admin/password", {"current": PASSWORD, "new": "a new password"})
        self.assertEqual((status, body["result"]), (200, "changed"))
        self.assertEqual(self.request("/admin/api/state")[0], 200)      # this device stays logged in
        self.cookie = old_cookie
        self.assertEqual(self.request("/admin/api/state")[0], 401)      # other logins end
        self.assertEqual(self.login("a new password")[0], 200)

    def test_logout(self):
        self.login()
        self.request("/admin/logout", {})
        self.assertEqual(self.request("/admin/api/state")[0], 401)
