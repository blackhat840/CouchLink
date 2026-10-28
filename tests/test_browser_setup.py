# SPDX-License-Identifier: GPL-3.0-or-later
"""Setting CouchLink up in a browser: the setup code, setup mode, finding the HDMI
inputs through the running cec-client, the MAC address and the SSH check."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from helpers import BASE, assert_private
from couchlink import admin, auth, cec, cli, config, web
from test_admin import PASSWORD, config_copy, quick_hash

SCAN_LINES = [
    "requesting CEC bus information ...",
    "CEC bus information",
    "===================",
    "device #0: TV",
    "address:       0.0.0.0",
    "device #1: Recorder 1",
    "address:       4.0.0.0",
    "osd string:    CouchLink",
    "device #5: Audio",
    "address:       1.0.0.0",
    "osd string:    Soundbar",
    "currently active source: unknown (-1)",
]


class FakeCec:
    """Stands in for the running cec-client."""

    def __init__(self, devices=None):
        self.devices = devices
        self.heard = []

    def scan(self, timeout=40):
        return [dict(d) for d in self.devices] if self.devices is not None else None

    def heard_since(self, since):
        return [a for t, a in self.heard if t > since]

    def stop(self):
        pass


class SetupCodeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.path = os.path.join(d, "config.toml")

    def test_make_read_check_remove(self):
        self.assertIsNone(auth.read_setup_code(self.path))
        code = auth.make_setup_code(self.path)
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(auth.make_setup_code(self.path), code)          # the same one again
        assert_private(self, auth.setup_code_path(self.path))
        self.assertTrue(auth.setup_code_ok(code.lower(), self.path))     # case and dash don't matter
        self.assertTrue(auth.setup_code_ok(code.replace("-", ""), self.path))
        self.assertFalse(auth.setup_code_ok("AAAA-AAAA", self.path))
        self.assertFalse(auth.setup_code_ok("", self.path))
        auth.remove_setup_code(self.path)
        self.assertFalse(auth.setup_code_ok(code, self.path))

    def test_show_setup_code(self):
        shutil.copy(config_copy(self), self.path)
        cli.show_setup_code(self.path)
        self.assertIsNotNone(auth.read_setup_code(self.path))


class CecScanTests(unittest.TestCase):
    """The running cec-client's scan and what it hears, with a fake process behind it."""

    def client(self):
        c = object.__new__(cec.CecClient)        # no real cec-client
        c.lock, c.scan_lock, c.seen = threading.Lock(), threading.Lock(), threading.Condition()
        c.lines, c.count, c.heard = [], 0, []

        def send(command):
            if command == "scan":                 # cec-client answers a little later
                threading.Timer(0.05, lambda: [c._keep(line + "\n") for line in SCAN_LINES]).start()
        c.send = send
        return c

    def test_scan(self):
        devices = self.client().scan(timeout=5)
        self.assertEqual([(d["logical"], d["address"]) for d in devices], [(0, "00:00"), (1, "40:00"), (5, "10:00")])

    def test_scan_gives_up(self):
        c = self.client()
        c.send = lambda command: None             # never answers
        self.assertIsNone(c.scan(timeout=0.2))

    def test_heard(self):
        c = self.client()
        before = time.monotonic() - 0.001      # Windows' clock moves in steps of about 15 ms
        c._keep("TRAFFIC: [ 1]\t>> 0f:80:40:00:10:00\n")
        c._keep("TRAFFIC: [ 2]\t>> 5f:81:12:00\n")
        c._keep("TRAFFIC: [ 3]\t>> 01:90:00\n")   # not an input change
        self.assertEqual(c.heard_since(before), ["10:00", "12:00"])
        self.assertEqual(c.heard_since(time.monotonic() + 1), [])

    def test_no_tv_has_nothing_to_scan(self):
        self.assertIsNone(cec.NoCec().scan())
        self.assertEqual(cec.NoCec().heard_since(0), [])


class BrowserSetupTests(unittest.TestCase):
    """The settings page on a fresh install: no password yet, and settings to fill in."""

    def setUp(self):
        self.path = config_copy(self)
        with open(self.path) as f:          # a fresh install: the HDMI inputs aren't known yet
            text = config.unset_config_value(f.read(), "pi_address").replace('pc_addresses = ["12:00"]',
                                                                              "pc_addresses = []")
        with open(self.path, "w") as f:
            f.write(text)
        self.cec = FakeCec(devices=[{"logical": 0, "name": "TV", "address": "00:00", "vendor": "", "osd": "TV"},
                                    {"logical": 1, "name": "Recorder 1", "address": "40:00", "vendor": "", "osd": ""},
                                    {"logical": 5, "name": "Audio", "address": "10:00", "vendor": "", "osd": "Bar"}])
        settings, problems = config.load_config(self.path)
        cfg, more = config.validate(settings)
        self.setup = cli.SetupMode(cfg, problems + more)
        self.server = web.start_dashboard(0, self.setup, None, self.path, self.cec)
        self.port = self.server.server_address[1]
        self.addCleanup(lambda: threading.Thread(target=self.server.shutdown, daemon=True).start())
        self.cookie = None
        self.code = auth.make_setup_code(self.path)

    def request(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", f"http://127.0.0.1:{self.port}")
        req.add_header("Sec-Fetch-Site", "same-origin")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as r:
                status, headers, raw = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, e.headers, e.read()
        if headers.get("Set-Cookie"):
            self.cookie = headers.get("Set-Cookie").split(";")[0]
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, raw.decode()

    def set_password(self):
        return self.request("/admin/setup", {"code": self.code, "password": PASSWORD})

    def test_status_page_goes_to_the_settings_page(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        try:
            urllib.request.build_opener(NoRedirect).open(req, timeout=10)
            self.fail("no redirect")
        except urllib.error.HTTPError as e:
            self.assertEqual((e.code, e.headers["Location"]), (302, "/admin"))

    def test_first_visit_asks_for_the_code(self):
        status, body = self.request("/admin/api/state")
        self.assertEqual((status, body["result"]), (403, "setup"))
        auth.remove_setup_code(self.path)
        status, body = self.request("/admin/api/state")
        self.assertEqual((status, body["result"]), (403, "no_password"))
        self.assertIn("--setup-code", body["message"])

    def test_wrong_code_short_password_then_right(self):
        status, body = self.request("/admin/setup", {"code": "AAAA-AAAA", "password": PASSWORD})
        self.assertEqual((status, body["result"]), (401, "wrong_code"))
        status, body = self.request("/admin/setup", {"code": self.code, "password": "short"})
        self.assertEqual((status, body["result"]), (400, "too_short"))
        status, body = self.set_password()
        self.assertEqual((status, body["result"]), (200, "password_set"))
        self.assertIsNone(auth.read_setup_code(self.path))                 # used up
        self.assertTrue(auth.check_password(PASSWORD, config.load_config(self.path)[0]["admin_password_hash"]))
        status, body = self.request("/admin/api/state")                    # and logged in
        self.assertEqual(status, 200)
        self.assertIn("The PC's HDMI input. \"Find the HDMI inputs\" can fill it in", body["todo"])
        self.assertTrue(body["cec"])
        status, body = self.request("/admin/setup", {"code": self.code, "password": "another one"})
        self.assertEqual(status, 409)                                      # only once

    def test_wrong_codes_lock_out(self):
        for _ in range(5):
            self.request("/admin/setup", {"code": "AAAA-AAAA", "password": PASSWORD})
        status, body = self.set_password()
        self.assertEqual((status, body["result"]), (429, "locked_out"))

    def test_finding_needs_a_login(self):
        self.assertEqual(self.request("/admin/cec/scan", {})[0], 401)

    def test_scan_and_listen(self):
        self.set_password()
        status, body = self.request("/admin/cec/scan", {})
        self.assertEqual(status, 200)
        self.assertEqual([d["dotted"] for d in body["devices"]], ["0.0.0.0", "4.0.0.0", "1.0.0.0"])
        status, start = self.request("/admin/cec/heard", {})
        self.assertEqual(start["heard"], [])
        self.cec.heard.append((time.monotonic() + 1, "12:00"))
        status, body = self.request("/admin/cec/heard", {"since": start["since"]})
        self.assertEqual(body["heard"], [{"address": "12:00", "dotted": "1.2.0.0"}])

    def test_scan_with_no_answer(self):
        self.set_password()
        self.cec.devices = []
        status, body = self.request("/admin/cec/scan", {})
        self.assertEqual((status, body["result"]), (504, "no_devices"))

    def test_no_cec_adapter(self):
        self.server.shutdown()
        self.server = web.start_dashboard(0, self.setup, None, self.path, cec.NoCec())
        self.port = self.server.server_address[1]
        self.set_password()
        self.assertEqual(self.request("/admin/cec/scan", {})[1]["result"], "no_cec")
        self.assertFalse(self.request("/admin/api/state")[1]["cec"])

    def test_find_mac(self):
        self.set_password()
        real = admin.find_mac
        admin.find_mac = lambda host: "aa:bb:cc:dd:ee:01" if host == "192.168.1.50" else None
        self.addCleanup(setattr, admin, "find_mac", real)
        status, body = self.request("/admin/find-mac", {"host": "192.168.1.50"})
        self.assertEqual((status, body["mac"]), (200, "aa:bb:cc:dd:ee:01"))
        self.assertEqual(self.request("/admin/find-mac", {"host": "192.168.1.51"})[0], 404)
        self.assertEqual(self.request("/admin/find-mac", {"host": "bad host!"})[0], 400)

    def test_check_ssh(self):
        self.set_password()
        real = admin.check_ssh
        admin.check_ssh = lambda host, user: (False, "ssh said: No route to host. The Pi can't reach the PC")
        self.addCleanup(setattr, admin, "check_ssh", real)
        status, body = self.request("/admin/check-ssh", {})
        self.assertEqual(body["result"], "fails")
        self.assertIn("--setup-ssh", body["message"])

    def test_saving_a_complete_config_restarts(self):
        events = []
        self.server.shutdown()
        q = type("Q", (), {"put": lambda _, e: events.append(e)})()
        self.server = web.start_dashboard(0, self.setup, q, self.path, self.cec)
        self.port = self.server.server_address[1]
        self.set_password()
        status, body = self.request("/admin/settings", {"values": {"pc_addresses": "12:00", "pi_address": "40:00",
                                                                   "soundbar_address": "10:00"}})
        self.assertEqual((status, body["result"]), (200, "saved"))
        time.sleep(0.2)
        self.assertEqual([e[0] for e in events], ["restart"])
        self.assertEqual(config.validate(config.load_config(self.path)[0])[1], [])

    def test_setup_mode_snapshot(self):
        snap = self.setup.snapshot()
        self.assertTrue(snap["setup_needed"])
        self.assertIn("recent_log", snap)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
