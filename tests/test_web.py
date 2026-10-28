# SPDX-License-Identifier: GPL-3.0-or-later
"""The API over real HTTP."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import REAL, Sim, msg
from couchlink import auth, common, web


class ApiTests(unittest.TestCase):
    """POST /api/wake, /api/sleep and /api/session/end over real HTTP."""

    KEY = "test-key-0123456789abcdef"

    def start(self, keyed=True, **overrides):
        import queue
        if keyed:
            overrides["api_key_hash"] = auth.hash_api_key(self.KEY)
        self.sim = Sim(**dict(REAL, **overrides))
        self.sim.pc(False)
        self.sim.ev(msg("5f:81:11:00"))
        events = queue.Queue()

        def worker():
            while True:
                event = events.get()
                if event is None:
                    return
                self.sim.ctl.handle(event)
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.server = web.start_dashboard(0, self.sim.ctl, events)
        self.port = self.server.server_address[1]
        # shutdown() waits up to half a second for the server to notice, so don't wait
        self.addCleanup(lambda: threading.Thread(target=self.server.shutdown, daemon=True).start())
        self.addCleanup(events.put, None)

    def post(self, path, body=b"{}", ctype="application/json", key=KEY, **headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method="POST")
        if ctype:
            req.add_header("Content-Type", ctype)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        for k, v in headers.items():
            req.add_header(k.replace("_", "-"), v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_off_without_a_key(self):
        self.start(keyed=False)
        status, body = self.post("/api/wake", b'{"tv": false}')
        self.assertEqual((status, body["result"]), (403, "actions_off"))
        self.assertEqual(self.sim.act.wols, 0)

    def test_wake_starts_session(self):
        self.start()
        status, body = self.post("/api/wake", b'{"tv": false}')
        self.assertEqual((status, body["ok"], body["result"]), (200, True, "session_started"))
        self.assertEqual(self.sim.act.wols, 1)
        self.assertTrue(any("Wake PC requested from 127.0.0.1" in line for line in common.LOG_RING))

    def test_wake_pc_and_tv(self):
        self.start()
        status, body = self.post("/api/wake", b'{"tv": true}')
        self.assertEqual(body["result"], "waking_pc_and_tv")
        self.assertIn("on 0", self.sim.act.cec)

    def test_end_session(self):
        self.start()
        self.post("/api/wake", b'{"tv": false}')
        status, body = self.post("/api/session/end")
        self.assertEqual((status, body["result"]), (200, "session_ended"))

    def test_sleep(self):
        self.start()
        self.sim.pc(True)
        status, body = self.post("/api/sleep")
        self.assertEqual(body["result"], "suspending")
        self.assertEqual(self.sim.act.suspends, 1)

    def test_rate_limited(self):
        self.start()
        self.post("/api/wake", b'{"tv": false}')
        status, body = self.post("/api/wake", b'{"tv": false}')
        self.assertEqual((status, body["result"]), (429, "too_soon"))

    def test_needs_the_key(self):
        self.start()
        for key in (None, "wrong"):
            status, body = self.post("/api/wake", b'{"tv": false}', key=key)
            self.assertEqual((status, body["result"]), (401, "bad_key"))
        self.assertEqual(self.sim.act.wols, 0)
        self.assertTrue(any("Wrong API key from 127.0.0.1" in line for line in common.LOG_RING))

    def test_locked_out_after_wrong_keys(self):
        self.start()
        for _ in range(5):
            self.post("/api/session/end", key="wrong")
        status, body = self.post("/api/wake", b'{"tv": false}')      # even with the right key
        self.assertEqual((status, body["result"]), (429, "locked_out"))
        self.assertEqual(self.sim.act.wols, 0)

    def test_no_body_or_content_type_needed(self):
        self.start()
        status, body = self.post("/api/session/end", body=b"", ctype=None)
        self.assertEqual((status, body["result"]), (200, "no_session"))

    def test_body_must_be_json_object(self):
        self.start()
        status, body = self.post("/api/wake", b"tv=false", ctype="application/x-www-form-urlencoded")
        self.assertEqual((status, body["result"]), (400, "bad_request"))
        status, body = self.post("/api/wake", b"not json")
        self.assertEqual((status, body["result"]), (400, "bad_request"))
        status, body = self.post("/api/wake", b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(self.sim.act.wols, 0)

    def test_other_sites_refused(self):
        self.start()
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                        {"Sec_Fetch_Site": "cross-site"}):
            status, body = self.post("/api/wake", b'{"tv": false}', **headers)
            self.assertEqual((status, body["result"]), (403, "refused"), headers)
        self.assertEqual(self.sim.act.wols, 0)

    def test_same_site_allowed(self):
        self.start()
        status, body = self.post("/api/wake", b'{"tv": false}', Origin=f"http://127.0.0.1:{self.port}",
                                 Sec_Fetch_Site="same-origin")
        self.assertEqual(status, 200)

    def page_headers(self):
        return {"Origin": f"http://127.0.0.1:{self.port}", "Sec_Fetch_Site": "same-origin"}

    def test_status_page_needs_no_key(self):
        self.start(keyed=False)                                     # works even with no key at all
        status, body = self.post("/api/wake", b'{"tv": false}', key=None, **self.page_headers())
        self.assertEqual((status, body["result"]), (200, "session_started"))
        self.assertTrue(any("Wake PC requested from 127.0.0.1 (status page)" in line
                            for line in common.LOG_RING))

    def test_status_page_buttons_can_be_turned_off(self):
        self.start(status_page_buttons=False)
        status, body = self.post("/api/wake", b'{"tv": false}', key=None, **self.page_headers())
        self.assertEqual((status, body["result"]), (403, "buttons_off"))
        status, body = self.post("/api/wake", b'{"tv": false}')    # scripts with the key still work
        self.assertEqual(status, 200)

    def test_scripts_still_need_the_key(self):
        self.start(keyed=False)
        status, body = self.post("/api/wake", b'{"tv": false}', key=None)
        self.assertEqual((status, body["result"]), (403, "actions_off"))
        self.assertEqual(self.sim.act.wols, 0)

    def test_from_status_page(self):
        f = web.from_status_page
        self.assertTrue(f({"Sec-Fetch-Site": "same-origin"}))
        self.assertTrue(f({"Origin": "http://couchlink.local:8080", "Host": "couchlink.local:8080"}))
        self.assertFalse(f({"Origin": "http://evil.example", "Host": "couchlink.local:8080"}))
        self.assertFalse(f({"Origin": "null", "Host": "couchlink.local:8080"}))
        self.assertFalse(f({"Host": "couchlink.local:8080"}))       # curl and most scripts

    def test_unknown_path_and_get_stays_read_only(self):
        self.start()
        self.assertEqual(self.post("/api/reboot")[0], 404)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/wake", timeout=5) as r:
            snap = json.loads(r.read())                 # GET only reads
        self.assertTrue(snap["actions_enabled"])
        self.assertNotIn("api_key_hash", json.dumps(snap))              # never shown
        self.assertEqual(self.sim.act.wols, 0)
