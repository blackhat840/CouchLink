# How it works

What CouchLink does in detail, the layout it was built on, the rule it enforces, the CEC messages it listens for, and remote sessions.

## Why this exists

TVs, soundbars and streaming boxes talk to each other over HDMI-CEC. Most PC
graphics cards do not support CEC, so a PC on the same TV never finds out that
you switched inputs or turned the TV off.

## What it does

- **Wakes the PC** with Wake-on-LAN when the TV or soundbar is switched to the
  PC's input. Retries if the PC does not answer.
- **Suspends the PC** over SSH when you switch to another input or turn the TV
  off. It waits a few seconds first, so flipping past the PC's input on the
  way to another one does not count.
- **Switches the TV to the PC** when the PC wakes up on its own, for example
  from a controller or the power button. If the TV is off, CouchLink turns it
  on, waits for it to report that it is on, then claims the PC's input a few
  more times while the TV finishes starting.
- **Handles Sleep from the Steam menu.** When the PC goes to sleep by itself,
  CouchLink can switch the TV to another input, turn the TV off, or leave it
  alone. It never wakes the PC back up.
- **Undoes automatic input switches** that some TVs and soundbars make right
  after a device wakes up.
- **Keeps the PC awake for remote play.** A [remote session](#remote-sessions)
  keeps the PC awake without the TV, for streaming to another device.
- **Takes requests** from the wake and sleep buttons on the status page, and
  from scripts and Home Assistant through an API with a key. See
  [Status page and logs](status-page-and-logs.md#wake-and-sleep-buttons).
- **Optional:** forwards TV remote buttons to the PC as a USB keyboard.
- **Safe to try:** a dry-run mode that watches and logs without changing
  anything, a small status page, and offline tests.

## The layout

The Pi runs one program, `couchlink`, as a systemd service.

```
                       +-------------+
   PC (HDMI) --------> | soundbar    |
   streamer (HDMI) --> | HDMI in 1/2 | --ARC--> TV HDMI 1
                       +-------------+
   Raspberry Pi (HDMI) ----------------------> TV HDMI 4

   Pi --- network ---> PC     Wake-on-LAN, SSH suspend, ping
   Pi --- USB -------> PC     optional keyboard for remote buttons
```

This is the layout it was built on. The PC can also plug straight into the TV.
Any layout works as long as you tell CouchLink which address belongs to the PC.

## The rule

| TV input | PC should be | What CouchLink does |
|---|---|---|
| The PC's input | Awake | Sends Wake-on-LAN until the PC answers ping |
| Any other input | Asleep | Waits 5 seconds, then suspends the PC over SSH |
| Standby | Asleep | Same as above |
| Not known yet | Left alone | Waits for the TV to announce an input |

During a [remote session](#remote-sessions), the PC stays awake whatever the
TV is doing.

## CEC messages it listens for

CouchLink learns which input is active from these messages:

| Opcode | Name | Typical sender |
|---|---|---|
| `82` | Active Source | A device saying "show me now" |
| `86` | Set Stream Path | The TV picking an input |
| `80` | Routing Change | The TV or a switch changing route |
| `81` | Routing Information | A soundbar or switch reporting its active input |
| `36` | Standby | The TV turning off (or another device asking it to) |
| `90` | Report Power Status | The TV answering "are you on?" |

Different TVs use different messages, so CouchLink watches all of them.

## Sleep, power-off and restarts

A PC that is asleep, turned off or restarting looks the same from the network:
it stops answering ping. To tell them apart, CouchLink keeps one SSH session
open to the PC while it is on (`watch_pc_power`). Nothing is installed on the
PC.

The session runs `gdbus monitor` on the PC, which comes with SteamOS and
Bazzite. It reports the announcements the PC makes just before it changes
state:

| The PC announces | Status page shows |
|---|---|
| Going to sleep | Asleep |
| Shutting down | Off |
| Restarting | Restarting |

Recent versions of systemd say whether a shutdown is a restart or a power-off.
On older ones, a restart shows as Off until the PC comes back.

If `gdbus` is missing, the session still helps. Shutting down closes it, while
sleep only freezes it. If CouchLink can't open the session at all, for example
because the SSH key isn't set up yet, the status page shows "Asleep or off".

While the PC is restarting, the `on_pc_sleep` action does not run. If a
restart takes longer than 5 minutes, CouchLink treats the PC as off.

## Remote sessions

Normally the PC is awake only while the TV is on its input. A remote session
keeps it awake without the TV, for streaming to another device with Steam
Link or Moonlight.

Start one with the Wake PC button on the status page, or with the API. See
[Wake and sleep buttons](status-page-and-logs.md#wake-and-sleep-buttons).

CouchLink has two kinds of wake:

| Wake | What happens |
|---|---|
| Wake PC | Starts a remote session. CouchLink sends Wake-on-LAN and keeps the PC awake. The TV is left alone. |
| Wake PC and TV | Same as waking the PC with a controller. The TV turns on and switches to the PC. |

A remote session has no time limit, so it never ends in the middle of a game.
It ends when:

- the PC goes to sleep by itself or is turned off
- you press End remote session or Put PC to sleep
- the TV is switched to the PC, which turns it into normal use

When you stop streaming, the PC's own sleep timer puts it to sleep, and that
ends the session. Make sure the timer is turned on in the PC's power settings.
If the PC sleeps while you are still streaming, make the timer longer. A
restart does not end a session. When a session ends and the TV is on another input,
CouchLink waits `away_debounce` seconds and then suspends the PC, as it does
after leaving the PC's input.

A remote session has to be started by CouchLink. If something else wakes the
PC, such as Moonlight's own Wake-on-LAN or a phone app, CouchLink sees the PC
wake by itself and turns the TV on and switches it to the PC
(`auto_switch_on_pc_wake`).

---

Back to the [Documentation](README.md) or the [README](../README.md).
