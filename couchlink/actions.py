# SPDX-License-Identifier: GPL-3.0-or-later
"""What the controller does to the world: Wake-on-LAN, suspend over SSH, CEC
commands and keys. DryRunActions only logs them."""

import os
import re
import socket
import subprocess
import threading
import time

from .common import log, ssh_command


def send_magic_packet(mac, broadcast, port):
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))


class RealActions:
    dry_run = False

    def __init__(self, cfg, cec, events):
        self.cfg = cfg
        self.cec = cec
        self.events = events

    def cec_send(self, command):
        try:
            self.cec.send(command)
        except (OSError, ValueError) as e:
            log("CEC-TX", f"Could not send '{command}': {e}", level="ERROR")

    def wol(self):
        try:
            send_magic_packet(self.cfg.pc_mac, self.cfg.wol_broadcast, self.cfg.wol_port)
        except OSError as e:
            log("WOL", f"Could not send magic packet: {e}", level="ERROR")

    def suspend(self):
        def run():
            try:
                r = subprocess.run(
                    ssh_command(f"{self.cfg.ssh_user}@{self.cfg.pc_host}", self.cfg.suspend_command),
                    capture_output=True, text=True, timeout=20)
                self.events.put(("suspend_done", r.returncode, r.stderr.strip()))
            except subprocess.TimeoutExpired:
                self.events.put(("suspend_done", None, "no answer within 20s"))
            except OSError as e:
                self.events.put(("suspend_done", -1, str(e)))
        threading.Thread(target=run, daemon=True).start()

    def fetch_pc_name(self):
        """Find the PC's name: ask it over SSH, then fall back to reverse DNS.
        Posts ("pc_name", name or None). Read-only, so dry runs do it too."""
        def clean(name):
            name = (name or "").strip().split(".")[0]
            return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name) else None

        def run():
            name = None
            try:
                r = subprocess.run(
                    ssh_command(f"{self.cfg.ssh_user}@{self.cfg.pc_host}", "hostname"),
                    capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    name = clean(r.stdout.splitlines()[0] if r.stdout else "")
            except (subprocess.TimeoutExpired, OSError):
                pass
            if not name:
                try:
                    name = clean(socket.gethostbyaddr(self.cfg.pc_host)[0])
                except OSError:
                    pass
            self.events.put(("pc_name", name))
        threading.Thread(target=run, daemon=True).start()

    def key(self, code, name):
        report = bytearray(8)
        report[2] = code
        try:
            fd = os.open(self.cfg.hidg_device, os.O_WRONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            log("KEY", "HID gadget not available, keypress skipped", level="WARN")
            return
        except OSError as e:
            log("KEY", f"Could not open {self.cfg.hidg_device}: {e}", level="WARN")
            return
        try:
            os.write(fd, bytes(report))
            time.sleep(0.05)
            for _ in range(10):
                try:
                    os.write(fd, bytes(8))
                    break
                except BlockingIOError:
                    time.sleep(0.01)
            log("KEY", f"CEC key '{name}' -> HID 0x{code:02x}")
        except BlockingIOError:
            log("KEY", "PC isn't reading keyboard input (asleep or USB unplugged), keypress dropped",
                level="WARN")
        except OSError as e:
            log("KEY", f"Keypress failed: {e}", level="WARN")
        finally:
            os.close(fd)


class DryRunActions(RealActions):
    """Reads the bus and asks the TV for its power status, but changes nothing."""
    dry_run = True

    def cec_send(self, command):
        if command == "pow 0":
            super().cec_send(command)       # read-only query
        else:
            log("DRY-RUN", f"Would send CEC: {command}")

    def wol(self):
        log("DRY-RUN", "Would send Wake-on-LAN")

    def suspend(self):
        log("DRY-RUN", f"Would run: ssh {self.cfg.ssh_user}@{self.cfg.pc_host} {self.cfg.suspend_command}")
        self.events.put(("suspend_done", 0, "dry run"))

    def key(self, code, name):
        log("DRY-RUN", f"Would send key '{name}' -> HID 0x{code:02x}")
