# Hardware and systems

Which PC systems CouchLink supports, and the TVs and soundbars it has been tested with.

## Supported systems

CouchLink itself runs on the Raspberry Pi. The PC only needs an SSH server, a
sudo rule that allows suspend, and a way to be woken. Nothing is installed on
the PC.

| PC operating system | Status | Notes |
|---|---|---|
| SteamOS 3 | Developed on it | Read-only system, default user `deck` |
| Bazzite | Instructions included, not yet tested by the author | Fedora Atomic base. `/etc` is writable, so no read-only toggle is needed |
| ChimeraOS, Nobara, CachyOS and others | Should work | Anything with systemd, sudo and NetworkManager |

If you run it on an untested system, please open an issue saying how it went.

## Tested hardware

CouchLink has run on two setups. Their TVs behave quite differently on the
CEC bus.

### Current setup: Vizio TV and soundbar

| Device | Model | CEC address |
|---|---|---|
| TV | Vizio P759-G1 | `0.0.0.0` |
| Soundbar | Vizio Elevate P514a-H6, on the TV's ARC port | `1.0.0.0` |
| Streamer | Google TV Streamer, on soundbar input 1 | `1.1.0.0` |
| PC | SteamOS 3 on a Gigabyte X870I Aorus Pro Ice, on soundbar input 2 | `1.2.0.0` (not a CEC device) |
| Raspberry Pi | Pi Zero W, Raspberry Pi OS Lite 32-bit, on TV input 4 | `4.0.0.0` |

What this setup does on the bus:

- The TV reports manual input changes with Routing Change (`80`), not Active
  Source or Set Stream Path.
- The soundbar reports its own input changes with Routing Information (`81`).
- Whenever the TV selects input 1, it routes to the streamer (`11:00`), which
  can pull the soundbar back off the PC.
- Sending `tx 1F:82:12:00` from the Pi switches the TV to input 1 and the
  soundbar to input 2 in one go, and they stay there. `tx 1F:82:11:00`
  switches back to the streamer.

### Earlier setup: Samsung TV

A Samsung TV (Samsung calls CEC "Anynet+"), a Pi Zero W, and a Linux PC
plugged straight into the TV. The model and input addresses were not recorded.

What that TV did on the bus:

- It announced input changes with Active Source (`82`) and Set Stream Path
  (`86`).
- Going from the PC's input to the TV's built-in apps first announced a route
  to the Pi's input, then the apps. Suspending still worked, because every
  input other than the PC's is treated the same.
- Right after the Pi woke the PC, the TV sometimes switched itself to the Pi's
  input. The bus showed the TV sending a routing command, and the Pi
  (`cec-client`) answering with Active Source and Image View On.

### Your hardware

If you get it working on other hardware, please open an issue with your TV
and soundbar models, what you saw in the trace, and the output of
`sudo couchlink --share-config`.

---

Back to the [Documentation](README.md) or the [README](../README.md).
