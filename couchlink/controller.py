# SPDX-License-Identifier: GPL-3.0-or-later
"""The controller. It owns all state and runs on one thread. Other threads only
post events to it, so the logic can be tested with a simulated clock."""

import time

from . import VERSION
from .cec import ANNOUNCE_OPCODES, KEYCODES, POWER_STATUS, device_name
from .common import LOG_RING, TRAFFIC_RING, log


PC_ANNOUNCED = {"sleep": "asleep", "reboot": "restarting", "poweroff": "off", "shutdown": "off"}


TV_BOOT_POLL = 2            # seconds between "is the TV on yet?" queries while it starts
ACTION_COOLDOWN = 10        # seconds between two wakes, or two sleeps, asked for from the status page or API


class Controller:
    def __init__(self, cfg, actions, clock=time.monotonic):
        self.cfg = cfg
        self.actions = actions
        self.now = clock
        self.pc_set = set(cfg.pc_addresses)
        self.pc_primary = cfg.pc_addresses[0] if cfg.pc_addresses else None   # None without a TV
        self.la = cfg.pi_logical_address
        self.suspects = ({cfg.pi_address, cfg.soundbar_address, cfg.soundbar_pc_audio_address}
                         | set(cfg.guard_addresses)) - {None} - self.pc_set
        t = self.now()
        self.tv_power = None            # "on" | "standby" | None (unknown)
        self.tv_input = None            # address, "standby", or None (unknown)
        self.input_since = t
        self.pc_online = None
        self.pc_online_since = None
        self.last_wol = None
        self.wol_attempts = 0
        self.wake_gave_up = False
        self.last_suspend = None
        self.suspend_attempts = 0
        self.sleep_gave_up = False
        self.suspend_inflight = False
        self.guard_until = 0
        self.guard_count = 0
        self.last_auto_switch = None
        self.session_since = None       # remote session: keep the PC awake without the TV, from then
        self.session_ended_at = None    # so the PC gets the usual away_debounce after a session
        self.session_seen_up = False    # the PC came up during this session, so stop sending WoL
        self.last_action = {}           # action name -> when it was last asked for, for ACTION_COOLDOWN
        self.tv_boot_started = None     # we turned the TV on and are waiting for it to report on
        self.next_boot_poll = 0
        self.tv_settle_until = 0        # the TV reported on, re-claims run until then
        self.pullback_pending = False
        self.pc_name = cfg.pc_name       # from the config, or looked up from the PC
        self.name_lookup_inflight = False
        self.name_lookup_tries = 0
        self.pc_seen_up = False         # PC answered ping during this stay on its input
        self.pc_offline_since = None    # PC dropped off while on screen, waiting to confirm sleep
        self.pc_down_since = None       # PC stopped answering ping
        self.first_ping_at = None       # first ping result, so "since" times before it are unknown
        self.power_note = None          # (what, time): the PC announced sleep, reboot or poweroff
        self.power_link = None          # (state, time): the power watcher's SSH session, up | closed | lost
        self.timers = []                # (due, description, function)
        self.next_power_poll = t + 3    # first query shortly after start
        self.startup_check_at = t + 6
        self.startup_pending = True
        self.started_at = t

    # ---- naming --------------------------------------------------------
    def input_name(self, addr):
        if addr is None:
            return "unknown"
        if addr == "standby":
            return "TV standby"
        if addr == "00:00":
            return "TV / built-in apps"
        digits = addr.replace(":", "").rstrip("0")
        label = "HDMI " + ".".join(str(int(c, 16)) for c in digits)
        if addr in self.pc_set:
            label += " (PC)"
        elif addr == self.cfg.pi_address:
            label += " (Raspberry Pi)"
        elif addr == self.cfg.soundbar_pc_audio_address:
            label += " (soundbar, PC audio)"
        elif addr == self.cfg.soundbar_address:
            label += " (soundbar)"
        return label

    # ---- helpers -------------------------------------------------------
    def desired(self):
        if self.tv_power == "standby" or self.tv_input == "standby":
            return "asleep"
        if self.tv_input is None:
            return None
        return "awake" if self.tv_input in self.pc_set else "asleep"

    def after(self, delay, description, fn):
        self.timers.append((self.now() + delay, description, fn))

    def cec(self, command, why):
        log("CEC-TX", f"{command}  ({why})")
        self.actions.cec_send(command)

    def assert_pc_input(self, why):
        self.cec(f"tx {self.la:x}F:82:{self.pc_primary}", why)

    def pull_back(self, why):
        """Claim the PC's input after guard_delay. Repeated calls before it fires add nothing."""
        if self.pullback_pending:
            return
        self.pullback_pending = True

        def fire():
            self.pullback_pending = False
            if self.tv_input in self.pc_set:
                self.assert_pc_input(why)
        self.after(self.cfg.guard_delay, "pull back", fire)

    def request_soundbar_audio(self):
        if not (self.cfg.select_soundbar_audio and self.cfg.soundbar_pc_audio_address):
            return
        self.cec(f"tx {self.la:x}5:70:{self.cfg.soundbar_pc_audio_address}",
                 "asking the soundbar to play the PC's audio input")

    def set_input(self, addr, source, detail=""):
        """Record the TV's input. source: tv | self | startup | standby | power."""
        prev = self.tv_input
        if addr == prev:
            log("INPUT", f"TV input unchanged: {self.input_name(addr)}{detail}")
            return False
        self.tv_input = addr
        self.input_since = self.now()
        self.suspend_attempts = 0
        self.sleep_gave_up = False
        self.wol_attempts = 0
        self.wake_gave_up = False
        self.pc_seen_up = bool(self.pc_online) and addr in self.pc_set
        self.pc_offline_since = None
        if addr not in ("standby", None):
            self.tv_power = "on"
        log("INPUT", f"TV input: {self.input_name(prev)} -> {self.input_name(addr)}{detail}")
        if addr in self.pc_set:
            if self.session_active():
                self.end_session("the TV was switched to the PC")
            self.on_pc_input_selected(source)
        return True

    def on_pc_input_selected(self, source):
        t = self.now()
        self.guard_until = t + self.cfg.guard_window
        self.guard_count = 0
        if source == "self":
            self.after(1.0, "switch TV to PC", lambda: self.assert_pc_input("switching the TV to the PC"))
        elif source == "startup":
            self.assert_pc_input("PC already on at startup")
        elif source == "tv" and self.cfg.claim_pc_input:
            self.assert_pc_input("answering for the PC's input")
        if self.pc_online:
            self.after(1.0, "soundbar audio", self.request_soundbar_audio)

    # ---- events --------------------------------------------------------
    def handle(self, event):
        kind = event[0]
        if kind == "cec":
            self.on_cec(event[1])
        elif kind == "key":
            self.on_key(event[1])
        elif kind == "ping":
            self.on_ping(event[1])
        elif kind == "suspend_done":
            self.on_suspend_done(event[1], event[2])
        elif kind == "pc_name":
            self.on_pc_name(event[1])
        elif kind == "cec_out":
            self.on_cec_out(event[1])
        elif kind == "action":      # ("action", name, params, source, reply queue or None)
            answer = self.action(event[1], event[2], event[3])
            if len(event) > 4 and event[4] is not None:
                event[4].put(answer)
        elif kind == "pc_power":
            self.on_pc_power(event[1])

    def lookup_pc_name(self):
        """Ask the PC for its name once it is reachable, unless the config sets one."""
        if self.pc_name or self.name_lookup_inflight or self.name_lookup_tries >= 3:
            return
        self.name_lookup_inflight = True
        self.name_lookup_tries += 1
        self.actions.fetch_pc_name()

    def on_pc_name(self, name):
        self.name_lookup_inflight = False
        if name and not self.pc_name:
            self.pc_name = name
            log("WATCHER", f"PC name: {name}")

    def on_cec(self, msg):
        op, p = msg.opcode, msg.params
        if op in ("82", "86") and msg.broadcast and len(p) >= 2:
            self.on_announcement(msg.initiator, op, f"{p[0]}:{p[1]}")
        elif op == "81" and msg.broadcast and len(p) >= 2 and self.cfg.watch_routing_messages:
            self.on_announcement(msg.initiator, op, f"{p[0]}:{p[1]}")
        elif op == "80" and msg.broadcast and len(p) >= 4 and self.cfg.watch_routing_messages:
            self.on_announcement(msg.initiator, op, f"{p[2]}:{p[3]}")
        elif op == "36" and (msg.broadcast or msg.destination == 0):
            self.on_standby(msg.initiator, msg.destination)
        elif op == "90" and msg.initiator == 0 and p:
            self.on_power_status(p[0])
        elif op == "85" and msg.broadcast and self.cfg.claim_pc_input and self.tv_input in self.pc_set:
            # Someone asked who the active source is. Answer for the PC.
            self.after(0.3, "answer active source", lambda: self.assert_pc_input(
                "answering Request Active Source for the PC"))

    def on_cec_out(self, msg):
        """A message the Pi sent. cec-client answers some requests by itself, for
        example with Active Source for the Pi's own input, which pulls the TV there."""
        p = msg.params
        if not (msg.opcode == "82" and msg.broadcast and len(p) >= 2
                and f"{p[0]}:{p[1]}" == self.cfg.pi_address):
            return
        if self.tv_boot_started is None and self.tv_input in self.pc_set and self.pc_online:
            log("RECOVER", f"cec-client made the Pi the active source ({self.input_name(self.cfg.pi_address)}) "
                           "while the PC is on. Switching the TV back to the PC")
            self.pull_back("undoing a switch to the Pi's own input")

    def on_announcement(self, initiator, opcode, addr):
        sender = device_name(initiator)
        detail = f" ({ANNOUNCE_OPCODES[opcode]} from {sender}, {addr})"
        if initiator in self.cfg.ignore_announcements_from:
            log("INPUT", f"Ignored {self.input_name(addr)}{detail}: sender is in ignore_announcements_from")
            return
        t = self.now()
        if self.tv_input in self.pc_set and addr not in self.pc_set:
            if self.tv_boot_started is not None:
                log("RECOVER", f"Ignoring {self.input_name(addr)}{detail}: the TV is still starting. "
                               "It is switched to the PC once it reports that it is on")
                return
            if t < self.tv_settle_until:
                log("RECOVER", f"TV switched to {self.input_name(addr)} while it was still starting{detail}. "
                               "Switching it back to the PC")
                self.pull_back("undoing a switch made while the TV was starting")
                return
            if addr == self.cfg.pi_address and self.pc_online:
                log("RECOVER", f"TV switched to {self.input_name(addr)} while the PC is on{detail}. "
                               "Switching it back to the PC")
                self.pull_back("undoing a switch to the Pi's own input")
                return
        if (self.tv_input in self.pc_set and addr in self.suspects and t < self.guard_until
                and self.guard_count < self.cfg.max_guard_recoveries):
            self.guard_count += 1
            log("RECOVER", f"TV switched to {self.input_name(addr)} right after switching to the PC{detail}. "
                           f"Treating it as automatic and switching back "
                           f"({self.guard_count}/{self.cfg.max_guard_recoveries}). The PC stays awake")
            self.after(self.cfg.guard_delay, "guard",
                       lambda: self.assert_pc_input("undoing an automatic switch after wake"))
            return
        self.set_input(addr, "tv", detail)

    def on_standby(self, initiator, destination):
        if initiator == 0:
            log("TV", "Standby broadcast from the TV")
            self.tv_boot_started = None
            self.tv_settle_until = 0
            self.tv_power = "standby"
            self.set_input("standby", "standby")
            return
        who = device_name(initiator)
        to = "everyone" if destination == 15 else "the TV"
        if self.cfg.verify_non_tv_standby:
            log("TV", f"Standby from {who} to {to}, asking the TV whether it is really off")
            for d in self.cfg.power_query_delays:
                self.after(d, "power query", lambda: self.cec("pow 0", "TV power check"))
        else:
            log("TV", f"Standby from {who} to {to}, treating it as TV standby")
            self.tv_power = "standby"
            self.set_input("standby", "standby")

    def on_power_status(self, code):
        status = POWER_STATUS.get(code, f"unknown ({code})")
        if self.tv_boot_started is not None:
            if code == "00":
                log("TV", "TV reports that it is on")
                self.tv_power = "on"
                self.tv_ready()
            else:
                log("TV", f"TV power status: {status}, still waiting for it to start", level="DEBUG")
            return
        changed = (self.tv_power != ("standby" if code in ("01", "03") else "on"))
        log("TV", f"TV power status: {status}", level="INFO" if changed else "DEBUG")
        if code in ("01", "03"):
            self.tv_power = "standby"
            self.set_input("standby", "power")
        elif code in ("00", "02"):
            self.tv_power = "on"
            if self.tv_input == "standby":
                # The TV came on without announcing an input. Don't guess.
                log("TV", "TV is on but hasn't announced an input yet, waiting for one")
                self.tv_input = None
                self.input_since = self.now()

    def on_key(self, name):
        if not self.cfg.hid_passthrough:
            return
        if self.tv_input not in self.pc_set:
            log("KEY", f"CEC key '{name}' ignored: the TV isn't on the PC's input", level="DEBUG")
            return
        code = KEYCODES.get(name)
        if code is None:
            log("KEY", f"Unmapped CEC key '{name}'", level="WARN")
            return
        self.actions.key(code, name)

    def on_pc_power(self, what):
        """What the power watcher heard from the PC: sleep, resume, reboot, poweroff,
        shutdown, or link_up / link_closed / link_lost for its own SSH session."""
        t = self.now()
        if what.startswith("link_"):
            self.power_link = (what[5:], t)
            log("WATCHER", f"Power watcher session {what[5:]}", level="DEBUG")
            return
        if what == "resume":
            self.power_note = None
            log("WATCHER", "PC says it woke up")
            return
        if what not in PC_ANNOUNCED:
            return
        note = self.power_note
        if what == "shutdown" and note and note[0] in ("reboot", "poweroff") and t - note[1] < 10:
            return      # the plain signal that follows the one saying which kind of shutdown
        self.power_note = (what, t)
        log("WATCHER", {"sleep": "PC says it is going to sleep", "reboot": "PC says it is restarting",
                        "poweroff": "PC says it is shutting down",
                        "shutdown": "PC says it is shutting down or restarting"}[what])

    def power_state(self):
        """on | asleep | off | restarting | unknown, or None before the first ping."""
        if self.pc_online is None:
            return None
        if self.pc_online:
            return "on"
        t = self.now()
        since = self.pc_down_since if self.pc_down_since is not None else self.started_at

        def recent(event):
            return event is not None and event[1] >= since - 120

        if recent(self.power_note):
            state = PC_ANNOUNCED[self.power_note[0]]
            if state == "restarting" and t - since > 300:
                return "off"        # it never came back
            return state
        if recent(self.power_link) and self.power_link[0] == "closed":
            return "off"            # shutting down stops the SSH server, sleep doesn't
        if self.last_suspend is not None and self.last_suspend >= since - 120:
            return "asleep"
        if self.power_link and (self.power_link[0] == "up" or
                                (self.power_link[0] == "lost" and recent(self.power_link))):
            return "asleep"         # the session froze rather than closed
        return "unknown"

    def on_ping(self, online):
        prev = self.pc_online
        self.pc_online = online
        t = self.now()
        if online:
            self.lookup_pc_name()
        if prev is None:
            self.first_ping_at = t
            if online:
                self.pc_online_since = t
            else:
                self.pc_down_since = t
            log("WATCHER", f"PC is {'reachable' if online else 'not reachable'}")
            return
        if online == prev:
            return
        if not online:
            self.pc_down_since = t
            state = self.power_state()
            log("WATCHER", "PC is now unreachable (" + {"asleep": "asleep", "off": "turned off",
                                                       "restarting": "restarting"}.get(state, "asleep or off") + ")")
            if self.session_active() and state != "restarting":
                self.end_session("the PC went to sleep" if state == "asleep" else
                                 "the PC was turned off" if state == "off" else "the PC went offline")
            if self.suspend_attempts:
                log("SUSPEND", "Confirmed: the PC went to sleep")
            elif self.desired() == "awake" and self.pc_seen_up:
                self.pc_offline_since = t
                log("WATCHER", f"PC went offline while on screen. If it stays offline for "
                               f"{self.cfg.pc_sleep_confirm}s, CouchLink takes it as put to sleep or "
                               f"turned off on purpose and won't wake it again")
            return
        self.pc_online_since = t
        was = self.power_note[0] if self.power_note else None
        self.power_note = None
        if was in ("reboot", "shutdown") and self.pc_down_since is not None and t - self.pc_down_since < 600:
            log("WATCHER", "PC is back after restarting")
        if self.pc_offline_since is not None:
            if was not in ("reboot", "shutdown"):
                log("WATCHER", "PC is back (it was only a network blip)")
            self.pc_offline_since = None
        ours = self.last_wol is not None and t - self.last_wol <= self.cfg.wake_correlation
        d = self.desired()
        if self.session_active() and d != "awake":
            self.session_seen_up = True
            log("WATCHER", "PC is now reachable (remote session)" +
                (". Leaving the TV as it is" if self.cfg.use_tv else ""))
            return
        if not self.cfg.use_tv:
            log("WATCHER", "PC is now reachable" + (" (woken by CouchLink)" if ours else ""))
            return      # without a TV, CouchLink leaves an awake PC alone
        if d == "awake":
            log("WATCHER", "PC is now reachable" + (" (woken by CouchLink)" if ours else ""))
            # Its HDMI outputs just came alive. This is when a soundbar or TV may switch by itself.
            self.guard_until = max(self.guard_until, t + self.cfg.guard_window)
            self.after(2.0, "soundbar audio", self.request_soundbar_audio)
        elif ours:
            log("WATCHER", "PC woke up from CouchLink's Wake-on-LAN, but the TV has left its input. "
                           "Putting it back to sleep")
        else:
            log("WATCHER", "PC woke up by itself")
            self.auto_switch_to_pc()

    def handle_pc_slept(self):
        """The PC went to sleep by itself (Steam menu, power button) while on screen."""
        self.pc_offline_since = None
        self.apply_on_pc_sleep("was turned off" if self.power_state() == "off" else "went to sleep")

    def apply_on_pc_sleep(self, went):
        """Do what on_pc_sleep says for a PC that went to sleep while on screen."""
        mode = self.cfg.on_pc_sleep
        if mode == "switch":
            target = self.cfg.pc_sleep_switch_to
            self.cec(f"tx {self.la:x}F:82:{target}",
                     f"PC {went}: switching to {self.input_name(target)}")
            self.set_input(target, "self", f" (PC {went})")
        elif mode == "standby":
            self.cec("standby 0", f"PC {went}: putting the TV in standby")
            self.tv_power = "standby"
            self.set_input("standby", "standby", f" (PC {went})")
        else:
            log("WATCHER", f"PC {went} while on screen. Leaving the TV as it is "
                           "(on_pc_sleep = \"nothing\")")

    # ---- remote sessions ------------------------------------------------
    def session_active(self):
        return self.session_since is not None

    def wake(self, tv, frm=""):
        """Wake the PC on request. With tv, like a controller wake: the TV turns on and
        switches to the PC. Without, a remote session: the PC stays awake, the TV is left alone.
        Returns (result, message)."""
        if tv:
            log("ACTION", f"Wake PC and TV requested{frm}")
            # Wake it even if it was put to sleep on this input or earlier wakes gave up
            self.wol_attempts = 0
            self.wake_gave_up = False
            self.pc_seen_up = bool(self.pc_online)
            self.pc_offline_since = None
            self.auto_switch_to_pc(requested=True)
            return "waking_pc_and_tv", "Turning the TV on and switching it to the PC"
        if self.tv_input in self.pc_set:
            log("ACTION", f"Wake PC requested{frm}, but the TV is already on the PC. "
                          "No remote session needed")
            return "not_needed", "The TV is on the PC, so the PC stays awake anyway"
        log("ACTION", f"Wake PC requested{frm}")
        if self.session_active():
            log("ACTION", "A remote session is already running")
            return "session_running", "A remote session is already running"
        t = self.now()
        self.session_since = t
        self.session_ended_at = None
        self.suspend_attempts = 0
        self.sleep_gave_up = False
        self.session_seen_up = bool(self.pc_online)
        if not self.session_seen_up:
            self.wol_attempts = 0
            self.wake_gave_up = False
        message = ("Remote session started: keeping the PC awake without the TV until it goes to "
                   "sleep or the session is ended")
        log("ACTION", message)
        if not self.session_seen_up:
            self.send_wol(t)
        return "session_started", message

    def request_sleep(self, frm=""):
        """Put the PC to sleep on request. Ends a remote session. Returns (result, message)."""
        log("ACTION", f"Put PC to sleep requested{frm}")
        if self.pc_online is not True:
            log("ACTION", "The PC is not reachable, so it is already asleep or off")
            return "already_asleep", "The PC is already asleep or off"
        self.end_session("the PC was put to sleep")
        self.session_ended_at = None    # sleep was asked for, so don't wait
        if self.suspend_inflight:
            return "suspending", "Already putting the PC to sleep"
        if self.tv_input in self.pc_set and self.cfg.on_pc_sleep != "nothing":
            self.apply_on_pc_sleep("was put to sleep")   # the TV does what it does when the PC sleeps itself
        t = self.now()
        self.sleep_gave_up = False
        self.suspend_attempts += 1
        self.last_suspend = t
        self.suspend_inflight = True
        log("SUSPEND", "Suspending the PC (requested)")
        self.actions.suspend()
        return "suspending", "Putting the PC to sleep"

    def action(self, name, params=None, source=None):
        """An action from the status page or API. Returns (ok, result, message)."""
        params = params if isinstance(params, dict) else {}
        frm = f" from {source}" if source else ""
        t = self.now()
        if name == "wake" and params.get("tv") and not self.cfg.use_tv:
            # Refused before the rate limit, so it doesn't hold up a Wake PC right after
            log("ACTION", f"Wake PC and TV requested{frm}, but CouchLink is set up without a TV")
            return False, "no_tv", "CouchLink is set up without a TV. Use Wake PC instead"
        if name in ("wake", "sleep"):
            prev = self.last_action.get(name)
            if prev is not None and t - prev < ACTION_COOLDOWN:
                what = "Wake" if name == "wake" else "Put PC to sleep"
                log("ACTION", f"{what} requested{frm}, but one was asked for less than "
                              f"{ACTION_COOLDOWN}s ago. Ignored")
                return False, "too_soon", f"Wait {ACTION_COOLDOWN} seconds between requests"
            self.last_action[name] = t
        if name == "wake":
            return (True,) + self.wake(bool(params.get("tv")), frm)
        if name == "sleep":
            return (True,) + self.request_sleep(frm)
        if name == "end_session":
            log("ACTION", f"End remote session requested{frm}")
            if not self.session_active():
                return True, "no_session", "No remote session is running"
            self.end_session("ended by hand")
            return True, "session_ended", "Remote session ended"
        return False, "unknown_action", f"Unknown action: {name}"

    def end_session(self, why):
        if self.session_since is None:
            return
        self.session_since = None
        self.session_ended_at = self.now()
        log("ACTION", f"Remote session ended: {why}")

    def session_tick(self, t):
        """Wake the PC for a remote session, and end it if the PC never comes back from a restart."""
        if self.pc_online is True:
            self.session_seen_up = True
        elif self.session_seen_up:
            if self.pc_online is False and self.power_state() == "off":
                self.end_session("the PC did not come back after restarting")
        else:
            self.send_wol(t)
            if self.wake_gave_up:
                self.end_session("the PC never answered Wake-on-LAN")

    def auto_switch_to_pc(self, requested=False):
        mode = self.cfg.auto_switch_on_pc_wake
        t = self.now()
        tv_off = self.tv_power == "standby" or self.tv_input == "standby"
        if not requested and (mode == "never" or (mode == "if_tv_on" and tv_off)):
            log("WATCHER", f"Not switching the TV (auto_switch_on_pc_wake = \"{mode}\"). "
                           "The PC goes back to sleep if the TV stays on another input")
            return
        if (not requested and self.last_auto_switch is not None
                and t - self.last_auto_switch < self.cfg.auto_switch_cooldown):
            log("WATCHER", f"Not switching the TV: it was switched to the PC less than "
                           f"{self.cfg.auto_switch_cooldown}s ago (auto_switch_cooldown)")
            return
        self.last_auto_switch = t
        self.cec("on 0", "Wake PC and TV requested: turning the TV on" if requested
                 else "PC woke by itself: turning the TV on")
        why = " (Wake PC and TV)" if requested else " (PC woke by itself)"
        if self.tv_power == "on" and self.tv_input != "standby":
            self.set_input(self.pc_primary, "self", why)
            return
        # The TV is off or its state is unknown. A starting TV may switch to the Pi
        # (Image View On came from the Pi's input) or route to another device, so
        # the PC's input is only claimed once the TV reports that it is on.
        self.tv_boot_started = t
        self.next_boot_poll = t
        self.tv_settle_until = 0
        log("TV", f"Waiting up to {self.cfg.tv_boot_timeout}s for the TV to report that it is on "
                  "before switching it to the PC")
        self.set_input(self.pc_primary, "boot", why[:-1] + ", TV starting)")

    def tv_ready(self):
        """The TV finished starting (or never answered). Claim the PC's input, then again a few times."""
        self.tv_boot_started = None
        if self.tv_input not in self.pc_set:
            return
        n, gap = self.cfg.tv_boot_reclaims, self.cfg.tv_boot_reclaim_interval
        t = self.now()
        self.tv_settle_until = t + n * gap
        self.guard_until = max(self.guard_until, self.tv_settle_until + self.cfg.guard_window)
        self.guard_count = 0
        self.assert_pc_input("the TV is on: switching it to the PC")

        def reclaim(i):
            if self.tv_input in self.pc_set and self.tv_boot_started is None:
                self.assert_pc_input(f"the TV may still be starting: claiming the PC's input again ({i}/{n})")
        for i in range(1, n + 1):
            self.after(i * gap, "re-claim", lambda i=i: reclaim(i))

    def on_suspend_done(self, returncode, detail):
        self.suspend_inflight = False
        if returncode == 0:
            log("SUSPEND", "Suspend command accepted, waiting for the PC to go offline")
        elif returncode == 255:
            log("SUSPEND", f"SSH ended with 255 ({detail or 'connection closed'}). That can be "
                           "normal if the PC suspended mid-command. Checking with ping", level="WARN")
        elif returncode is None:
            log("SUSPEND", f"Suspend command timed out: {detail}", level="WARN")
        else:
            log("SUSPEND", f"Suspend command failed ({returncode}): {detail}", level="ERROR")

    # ---- periodic --------------------------------------------------------
    def tick(self):
        t = self.now()
        due = [x for x in self.timers if x[0] <= t]
        self.timers = [x for x in self.timers if x[0] > t]
        for _, _, fn in sorted(due, key=lambda x: x[0]):
            fn()

        if self.tv_boot_started is not None:
            if t - self.tv_boot_started >= self.cfg.tv_boot_timeout:
                log("TV", f"The TV did not report that it is on within {self.cfg.tv_boot_timeout}s. "
                          "Switching it to the PC anyway", level="WARN")
                self.tv_ready()
            elif t >= self.next_boot_poll:
                self.next_boot_poll = t + TV_BOOT_POLL
                self.actions.cec_send("pow 0")

        if not self.cfg.use_tv:
            pass                # no TV to ask, and no input to guess at startup
        elif self.cfg.power_poll_interval and t >= self.next_power_poll:
            self.next_power_poll = t + self.cfg.power_poll_interval
            self.actions.cec_send("pow 0")
        elif not self.cfg.power_poll_interval and self.next_power_poll and t >= self.next_power_poll:
            self.next_power_poll = None     # still do the one startup query
            self.actions.cec_send("pow 0")

        if self.startup_pending and t >= self.startup_check_at and self.cfg.use_tv:
            if self.pc_online is not None:
                self.startup_pending = False
                if self.pc_online and self.tv_input is None and self.tv_power != "standby":
                    log("STARTUP", "PC is already on and the TV's input is unknown: "
                                   "assuming the PC is in use")
                    self.set_input(self.pc_primary, "startup", " (startup)")
            elif t >= self.startup_check_at + 15:
                self.startup_pending = False

        d = self.desired()
        if d == "awake" and self.pc_online is True:
            self.pc_seen_up = True
        if (self.pc_offline_since is not None and self.pc_online is False and d == "awake"
                and t - self.pc_offline_since >= self.cfg.pc_sleep_confirm
                and self.power_state() != "restarting"):
            self.handle_pc_slept()
            d = self.desired()
        if self.session_since is not None and d != "awake":
            self.session_tick(t)
            if self.session_active():
                return
        if d == "awake":
            self.ensure_awake(t)
        elif d == "asleep":
            self.ensure_asleep(t)

    def ensure_awake(self, t):
        if self.pc_online is True or self.wake_gave_up:
            return
        if self.pc_seen_up:
            return      # it was up on this input and went down by itself: don't fight the user
        self.send_wol(t)

    def send_wol(self, t):
        """Send Wake-on-LAN, or wait for the retry interval, or give up after max_wol_attempts."""
        if self.wol_attempts and t - self.last_wol < self.cfg.wol_retry_interval:
            return
        if self.wol_attempts >= self.cfg.max_wol_attempts:
            log("WOL", f"Giving up after {self.wol_attempts} Wake-on-LAN packets. The PC never "
                       "answered ping", level="ERROR")
            self.wake_gave_up = True
            return
        self.wol_attempts += 1
        self.last_wol = t
        log("WOL", f"Sending Wake-on-LAN to {self.cfg.pc_mac} "
                   f"(attempt {self.wol_attempts}/{self.cfg.max_wol_attempts})")
        self.actions.wol()

    def ensure_asleep(self, t):
        if self.pc_online is not True or self.suspend_inflight or self.sleep_gave_up:
            return
        away_for = t - self.input_since
        if away_for < self.cfg.away_debounce:
            return
        if self.session_ended_at is not None and t - self.session_ended_at < self.cfg.away_debounce:
            return      # a remote session just ended: give it the same wait as leaving the input
        if self.last_suspend is not None and t - self.last_suspend < self.cfg.suspend_retry_interval:
            return
        if self.suspend_attempts >= self.cfg.max_suspend_attempts:
            log("SUSPEND", f"Giving up after {self.suspend_attempts} suspend attempts. The PC still "
                           f"answers while the TV is on {self.input_name(self.tv_input)}", level="ERROR")
            self.sleep_gave_up = True
            return
        self.suspend_attempts += 1
        self.last_suspend = t
        self.suspend_inflight = True
        log("SUSPEND", f"Suspending the PC, attempt {self.suspend_attempts}/"
                       f"{self.cfg.max_suspend_attempts} (TV on {self.input_name(self.tv_input)} "
                       f"for {int(away_for)}s)")
        self.actions.suspend()

    # ---- status ---------------------------------------------------------
    def pc_state_started(self):
        """When the PC last came up or went down, as far as CouchLink has seen."""
        if self.pc_online is None:
            return None
        return self.pc_online_since if self.pc_online else self.pc_down_since

    def pc_state_seconds(self, t):
        since = self.pc_state_started()
        return int(t - since) if since is not None else None

    def snapshot(self):
        t = self.now()
        return {
            "version": VERSION,
            "dry_run": getattr(self.actions, "dry_run", False),
            "tv_power": self.tv_power,
            "tv_input": self.input_name(self.tv_input),
            "tv_input_for_seconds": int(t - self.input_since),
            "pc_should_be": self.desired() or "unknown",
            "pc_reachable": self.pc_online,
            "pc_power": self.power_state() or "unknown",
            "pc_state_seconds": self.pc_state_seconds(t),
            "pc_state_since_start": (self.first_ping_at is not None
                                     and self.pc_state_started() == self.first_ping_at),
            "wol_attempts": self.wol_attempts,
            "suspend_attempts": self.suspend_attempts,
            "guard_active": t < self.guard_until and self.tv_input in self.pc_set,
            "tv_starting": self.tv_boot_started is not None,
            "pc_sleep_check_seconds_left": (max(0, int(self.cfg.pc_sleep_confirm - (t - self.pc_offline_since)))
                                            if self.pc_offline_since is not None else None),
            "tv_input_address": self.tv_input,
            "pc_host": self.cfg.pc_host,
            "pc_name": self.pc_name,
            "pc_inputs": [self.input_name(a) for a in self.cfg.pc_addresses],
            "on_pc_sleep": self.cfg.on_pc_sleep,
            "on_pc_sleep_target": (self.input_name(self.cfg.pc_sleep_switch_to)
                                   if self.cfg.pc_sleep_switch_to else None),
            "power_poll_interval": self.cfg.power_poll_interval,
            "actions_enabled": self.cfg.status_page_buttons,
            "tv": self.cfg.use_tv,
            "remote_session_seconds": int(t - self.session_since) if self.session_active() else None,
            "last_wol_seconds_ago": int(t - self.last_wol) if self.last_wol is not None else None,
            "last_suspend_seconds_ago": int(t - self.last_suspend) if self.last_suspend is not None else None,
            "uptime_seconds": int(t - self.started_at),
            "recent_log": list(LOG_RING)[-40:],
            "recent_traffic": list(TRAFFIC_RING)[-40:],
        }
