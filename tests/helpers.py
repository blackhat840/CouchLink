# SPDX-License-Identifier: GPL-3.0-or-later
"""What the tests share: a simulated clock, fake actions and a controller to drive."""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the repository
sys.path.insert(0, HERE)

from couchlink import cec, common, config, controller  # noqa: E402

common.SHOW_DEBUG = False
# --quick (set by test_couchlink.py) skips the tests of install.sh itself.
QUICK = os.environ.get("COUCHLINK_TESTS_QUICK") == "1"


def page_text():
    """The status page: its HTML, CSS and JavaScript."""
    text = []
    for name in ("status.html", "style.css", "status.js"):
        with open(os.path.join(HERE, "couchlink", "static", name)) as f:
            text.append(f.read())
    return "\n".join(text)


# Layout used in these tests (matches the planned setup):
#   TV HDMI 3 = PC (video)          30:00
#   TV HDMI 4 = Raspberry Pi        40:00
#   TV HDMI 2 = soundbar (ARC)      20:00
#     soundbar In 1 = streamer      21:00
#     soundbar In 2 = PC audio      22:00
BASE = dict(config.DEFAULTS)
BASE.update(pc_host="192.168.1.50", pc_mac="aa:bb:cc:dd:ee:01",
            pc_addresses=["30:00"], pi_address="40:00",
            soundbar_address="20:00", soundbar_pc_audio_address="22:00",
            hid_passthrough=True, raw_debug=False)


def assert_private(test, path):
    """The file is readable by its owner only. Windows has no such permissions, so it is",
    checked on Linux, where CouchLink runs."""
    if os.name == "posix":
        test.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)

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
    return ("cec", cec.parse_rx(f"TRAFFIC: [ 1]\t>> {text}"))


def out(text):
    """A message the Pi sent, as cec-client logs it."""
    return ("cec_out", cec.parse_tx(f"TRAFFIC: [ 1]\t<< {text}"))


class Sim:
    def __init__(self, **overrides):
        s = dict(BASE)
        s.update(overrides)
        self.cfg, problems = config.validate(s)
        assert not problems, problems
        self.clock = Clock()
        self.act = FakeActions()
        self.ctl = controller.Controller(self.cfg, self.act, clock=self.clock)

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

    def do(self, name, **params):
        """An action from the status page or API. Returns (ok, result, message)."""
        answer = self.ctl.action(name, params, "192.168.1.20")
        self.ctl.tick()
        return answer


# The tested layout (from the scan and trace):
#   TV HDMI 1 (ARC) = soundbar 10:00; soundbar In 1 = streamer 11:00; In 2 = PC 12:00
#   TV HDMI 4 = Pi 40:00
REAL = dict(pc_addresses=["12:00"], pi_address="40:00", soundbar_address="10:00",
            soundbar_pc_audio_address=None, on_pc_sleep="switch", pc_sleep_switch_to="11:00")


OLD_CONFIG = """# A config from before 2.4
[pc]
pc_host = "192.168.1.50"
pc_mac = "aa:bb:cc:dd:ee:01"

[cec]
pc_adresses = ["12:00"]               # REQUIRED
pi_address = "40:00"

[behavior]
on_pc_sleep = "nothing"
select_soundbar_audio = "false"       # experimental

[timing]
away_debounce = "5"

[extras]
old_thing = 1
"""
