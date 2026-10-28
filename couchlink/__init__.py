# SPDX-License-Identifier: GPL-3.0-or-later
"""
CouchLink 2.7

Makes a SteamOS or Bazzite gaming PC behave like a console on the TV. Runs on a
Raspberry Pi on the TV's HDMI-CEC bus and keeps the PC's power state in line
with the TV:

    TV on the PC's input              ->  PC awake   (Wake-on-LAN, retried)
    TV on any other input, or standby ->  PC asleep  (suspend over SSH, retried)

and the other way round: if the PC wakes by itself (controller, power button)
the TV turns on and switches to it, and if the PC goes to sleep by itself the
TV can switch to another input.

A remote session keeps the PC awake without the TV, for streaming. The status
page and its API (POST /api/..., with an API key) can wake the PC, with or
without the TV, and put it to sleep. --setup and --setup-cec fill in the config.

Design notes:
  * One controller thread owns all state. The CEC reader, the pinger and the
    SSH worker only post events to a queue, so there are no shared-state locks
    and the logic can be tested with a simulated clock.
  * Settings live in /etc/couchlink/config.toml, so updating never touches them.
  * --dry-run watches the bus and logs what it would do without doing it.

Talks to the CEC bus through cec-client from libcec (Pulse-Eight,
GPL-2.0-or-later), which it runs as a separate program. See docs/credits.md.

Run with --help for options. See README.md and docs/ for setup.
"""

VERSION = "2.7"
