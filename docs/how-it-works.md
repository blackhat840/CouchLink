# How it works

What CouchLink does in detail, the layout it was built on, the rule it enforces, and the CEC messages it listens for.

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

---

Back to the [Documentation](README.md) or the [README](../README.md).
