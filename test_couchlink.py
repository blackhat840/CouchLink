#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Offline tests for CouchLink. No TV, Pi or PC needed: a simulated clock and fake
actions stand in for the hardware. The tests themselves are in tests/. Run from
the repository folder:

    python3 test_couchlink.py
    python3 test_couchlink.py --quick   # skip the installer's own tests, as install.sh --test does
"""
import os
import sys
import unittest

if __name__ == "__main__":
    if "--quick" in sys.argv:
        sys.argv.remove("--quick")
        # The tests of install.sh start many processes, which is slow on a Pi Zero,
        # and say nothing about whether CouchLink works on it.
        os.environ["COUCHLINK_TESTS_QUICK"] = "1"
    here = os.path.dirname(os.path.abspath(__file__))
    tests = unittest.defaultTestLoader.discover(os.path.join(here, "tests"), top_level_dir=os.path.join(here, "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    sys.exit(0 if result.wasSuccessful() else 1)
