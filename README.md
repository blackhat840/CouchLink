# Couchlink

Make a SteamOS or Bazzite gaming PC behave like a console on your TV.

A Raspberry Pi on the TV's HDMI-CEC bus keeps the PC and the TV in sync. It
works with SteamOS, Bazzite and any other Linux PC that uses systemd.

## Contents

- [Why this exists](#why-this-exists)
- [What it does](#what-it-does)
- [Supported systems](#supported-systems)
- [Project status](#project-status)
- [How it works](#how-it-works)
- [What you need](#what-you-need)
- [Quick start](#quick-start)
- [Finding your CEC addresses](#finding-your-cec-addresses)
- [Configuration](#configuration)
- [Preparing the PC](#preparing-the-pc)
- [Testing with a dry run](#testing-with-a-dry-run)
- [Going live](#going-live)
- [Updating](#updating)
- [Settings reference](#settings-reference)
- [Logs and the status page](#logs-and-the-status-page)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Files in this repository](#files-in-this-repository)
- [Tested hardware](#tested-hardware)
- [Credits](#credits)
- [Related projects](#related-projects)
- [Trademarks](#trademarks)
- [License](#license)

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
  from a controller or the power button. If the TV is off, Couchlink turns it
  on, waits for it to report that it is on, then claims the PC's input a few
  more times while the TV finishes starting.
- **Handles Sleep from the Steam menu.** When the PC goes to sleep by itself,
  Couchlink can switch the TV to another input, turn the TV off, or leave it
  alone. It never wakes the PC back up.
- **Undoes automatic input switches** that some TVs and soundbars make right
  after a device wakes up.
- **Optional:** forwards TV remote buttons to the PC as a USB keyboard.
- **Safe to try:** a dry-run mode that watches and logs without changing
  anything, a small status page, and offline tests.

## Supported systems

Couchlink itself runs on the Raspberry Pi. The PC only needs an SSH server, a
sudo rule that allows suspend, and a way to be woken. Nothing is installed on
the PC.

| PC operating system | Status | Notes |
|---|---|---|
| SteamOS 3 | Developed on it | Read-only system, default user `deck` |
| Bazzite | Instructions included, not yet tested by the author | Fedora Atomic base; `/etc` is writable, so no read-only toggle is needed |
| ChimeraOS, Nobara, CachyOS and others | Should work | Anything with systemd, sudo and NetworkManager |

If you run it on an untested system, please open an issue saying how it went.

## Project status

This is a work in progress.

- The CEC behavior has been checked on the hardware in
  [Tested hardware](#tested-hardware): bus scans, traffic traces, and the
  commands that switch the TV and soundbar.
- Couchlink's logic is covered by offline tests that replay simulated traffic,
  including messages captured from that hardware.
- Long-term live testing is ongoing.

## How it works

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
Any layout works as long as you tell Couchlink which address belongs to the PC.

The rule it enforces:

| TV input | PC should be | What Couchlink does |
|---|---|---|
| The PC's input | Awake | Sends Wake-on-LAN until the PC answers ping |
| Any other input | Asleep | Waits 5 seconds, then suspends the PC over SSH |
| Standby | Asleep | Same as above |
| Not known yet | Left alone | Waits for the TV to announce an input |

Couchlink learns which input is active by listening for these CEC messages:

| Opcode | Name | Typical sender |
|---|---|---|
| `82` | Active Source | A device saying "show me now" |
| `86` | Set Stream Path | The TV picking an input |
| `80` | Routing Change | The TV or a switch changing route |
| `81` | Routing Information | A soundbar or switch reporting its active input |
| `36` | Standby | The TV turning off (or another device asking it to) |
| `90` | Report Power Status | The TV answering "are you on?" |

Different TVs use different messages, so Couchlink watches all of them.

## What you need

| Item | Notes |
|---|---|
| Raspberry Pi | Any model with HDMI. The optional USB keyboard needs USB gadget support (Pi Zero, Zero W, Zero 2 W, Pi 4). Developed on a Pi Zero W. |
| Raspberry Pi OS Lite | Bookworm or newer, for Python 3.11+. Use the 32-bit image on a Zero W. |
| HDMI cable | Mini HDMI for the Zero family. |
| A CEC-capable TV | CEC must be enabled in the TV's settings. Brands call it different names, such as Anynet+, Bravia Sync, SimpLink or just CEC. |
| A gaming PC | SteamOS, Bazzite or a similar distro. See [Supported systems](#supported-systems). |
| A network | The Pi and the PC on the same LAN. |

Wired Ethernet is the most reliable way to wake the PC. See
[Preparing the PC](#preparing-the-pc).

## Quick start

1. **Flash the SD card.** In Raspberry Pi Imager, choose Raspberry Pi OS Lite.
   In the settings, set a hostname (for example `couchlink`), a username and
   password, your Wi-Fi network and country, and turn on SSH.

2. **Plug the Pi into the TV** before powering it on, and power it from the
   PWR port. The first boot takes a few minutes.

3. **Get the files onto the Pi.**

   ```bash
   sudo apt install -y git
   git clone https://github.com/blackhat840/Couchlink.git
   cd Couchlink
   ```

   Or copy `couchlink.py`, `couchlink.example.toml`, `test_couchlink.py` and
   `install.sh` into one folder with any SFTP client.

4. **Run the installer.**

   ```bash
   sudo bash install.sh              # Couchlink only
   sudo bash install.sh --with-hid   # also the USB keyboard (reboot afterwards)
   ```

   It installs `cec-utils`, runs the offline tests, installs the program as
   `/usr/local/bin/couchlink`, creates `/etc/couchlink/config.toml`, and
   installs `couchlink.service` without enabling it.

5. **Find your CEC addresses** and fill in the config. See the next two
   sections.

6. **Prepare the PC** so the Pi can wake it and put it to sleep.

7. **Do a dry run**, then go live.

## Finding your CEC addresses

Every HDMI device has a physical address that describes where it is plugged
in. A device on TV input 2 is `2.0.0.0`. A device on input 1 of a soundbar
that is itself on TV input 1 is `1.1.0.0`. Couchlink accepts either `1.1.0.0` or
the short form `11:00`.

**Step 1: scan the bus.**

```bash
echo "scan" | cec-client -s -d 1
```

This lists every device that speaks CEC. Trimmed example from the tested setup:

```
device #0: TV            address: 0.0.0.0
device #1: Recorder 1    address: 4.0.0.0   (the Pi itself)
device #4: Playback 1    address: 1.1.0.0   (Google streamer)
device #5: Audio         address: 1.0.0.0   (soundbar on the TV's ARC port)
```

**Step 2: find the PC.** The PC does not speak CEC, so it will not appear in
the scan. Watch the bus while you switch to the PC's input by hand:

```bash
stdbuf -oL cec-client -d 8 | while IFS= read -r line; do
  case "$line" in *">>"*|*"<<"*) printf '%(%H:%M:%S)T %s\n' -1 "$line" ;; esac
done
```

Switch to the PC's input with the TV or soundbar remote and look for a line
that names an address, such as:

```
19:51:07 TRAFFIC: [ 20643]  >> 5f:81:12:00
```

Here the soundbar (logical address `5`) reports that its active input is now
`1.2.0.0`. That is the PC.

Stop `couchlink.service` before running either command. Only one program can
use the CEC adapter at a time.

## Configuration

The installer creates `/etc/couchlink/config.toml`. It belongs to root, so edit
it with:

```bash
sudo nano /etc/couchlink/config.toml
```

You only need to set the values for your own setup. Everything else has a
sensible default (see [Settings reference](#settings-reference)).

Example for a PC behind the soundbar:

```toml
[pc]
pc_host = "192.168.1.50"         # the PC's IP address
pc_mac  = "aa:bb:cc:dd:ee:ff"    # MAC of the network card used for Wake-on-LAN

[cec]
pc_addresses = ["12:00"]         # soundbar input 2
pi_address = "40:00"             # TV input 4
soundbar_address = "10:00"       # TV input 1 (ARC)

[behavior]
on_pc_sleep = "switch"           # if the PC goes to sleep by itself,
pc_sleep_switch_to = "11:00"     # switch the TV to the streamer
```

Check it after every change:

```bash
sudo couchlink --check-config
```

It prints every setting with the value Couchlink will use, or a list of what is
wrong. Misspelled setting names are reported too.

Section names such as `[pc]` are only there for readability. Each section may
appear only once in the file.

## Preparing the PC

Do this once on the gaming PC. Run the commands in a terminal: on SteamOS,
switch to Desktop Mode and open Konsole. On Bazzite, open the terminal app from
Desktop Mode or the desktop.

In the examples below, replace `deck` with your username and `192.168.1.50`
with the PC's IP address. On SteamOS the user is always `deck`. On Bazzite it
is the account you created during setup, so set `ssh_user` in Couchlink's config
to match.

### 1. Set a password

sudo needs one, and fresh SteamOS installs do not have one.

```bash
passwd
```

Bazzite normally sets one during setup. Run `passwd` anyway if yours does not
have one.

### 2. Turn on the SSH server

```bash
sudo systemctl enable --now sshd
```

On Bazzite, the `ujust` helper also has an SSH recipe (run `ujust` to see
the list). Either way works.

If the Pi cannot connect later, the firewall may be blocking SSH. Bazzite and
other Fedora-based systems use firewalld:

```bash
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --reload
```

### 3. Allow suspend without a password

This lets the Pi run exactly one command as root, `systemctl suspend`, and
nothing else. First check where `systemctl` lives:

```bash
command -v systemctl
```

Create a sudoers file:

```bash
sudo visudo -f /etc/sudoers.d/zzz-cec-suspend
```

Add one line, with your username and the path from above:

```
deck ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend
```

Save, then check the syntax and permissions:

```bash
sudo chmod 0440 /etc/sudoers.d/zzz-cec-suspend
sudo visudo -c
```

Why the `zzz-` prefix: sudo uses the last rule that matches, and these
distros ship a group rule that always asks for a password. Naming the file so
it sorts last makes sure this rule wins.

Notes by system:

| System | What to know |
|---|---|
| SteamOS | If saving fails with "Read-only file system", run `sudo steamos-readonly disable` first and `sudo steamos-readonly enable` afterwards. Recheck this file after big SteamOS updates. |
| Bazzite | `/etc` is writable and survives updates, so nothing extra is needed. |

### 4. Set up waking

Couchlink needs some way to wake the PC. Pick whatever your PC supports. You
can combine them.

**Wired Ethernet (most reliable).** Works from sleep and from power-off.
Enable Wake on LAN in the BIOS and turn off ErP (sometimes called deep sleep).
Then enable it on the network connection:

```bash
nmcli connection show
sudo nmcli connection modify "<wired connection>" 802-3-ethernet.wake-on-lan magic
```

Bazzite also has a `ujust` recipe for Wake-on-LAN that does the same job.

**Wi-Fi.** Only some Wi-Fi cards can wake on a magic packet, and only from
sleep. Check yours:

```bash
iw list | grep -A8 -i wowlan
```

If the output includes "wake up on magic packet", enable it:

```bash
sudo nmcli connection modify "<wifi connection>" 802-11-wireless.wake-on-wlan magic
```

**Controller.** Many controllers, and their USB dongles, can wake a sleeping
PC by themselves. Put the PC to sleep and press the controller's home button.
If the PC wakes, Couchlink notices within a few seconds and switches the TV
and soundbar to it. You still need network waking for the PC to wake when you
pick its input on the TV. Some controllers need a udev rule before they can
wake the PC. Bazzite ships these rules for the controllers it knows about.

### 5. Give the Pi a key the PC trusts

The service on the Pi runs as root, so the key must belong to root on the Pi.
Run these **on the Pi**:

```bash
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519
sudo ssh-copy-id deck@192.168.1.50
sudo ssh deck@192.168.1.50 "echo ok"
```

The last command should print `ok` without asking for a password. Then test
the exact command Couchlink will use. This puts the PC to sleep:

```bash
sudo ssh deck@192.168.1.50 "sudo -n systemctl suspend"
```

On Bazzite and other SELinux systems, if the key is copied but SSH still asks
for a password, fix the labels on the PC's `.ssh` folder:

```bash
restorecon -Rv ~/.ssh
```

### 6. Give the PC a fixed address

Reserve the PC's IP address in your router's DHCP settings and put that IP in
`pc_host`. Names like `steamdeck.local` depend on mDNS working on both
machines and are less reliable.

## Testing with a dry run

A dry run watches the bus and logs every decision without waking, suspending
or switching anything. It does ask the TV whether it is on, which changes
nothing.

```bash
sudo systemctl stop couchlink.service 2>/dev/null
sudo couchlink --dry-run 2>&1 | tee ~/dryrun.log
```

Open `http://couchlink.local:8080` on your phone to follow along. Then try each
of these:

| Do this | Expect in the log |
|---|---|
| Switch to the PC's input | `TV input: HDMI 1.1 -> HDMI 1.2 (PC)` and `Would send Wake-on-LAN` |
| Switch to another input | The new input, then about 5 seconds later `Would run: ssh deck@192.168.1.50 sudo -n systemctl suspend` |
| Turn the TV off | `Standby broadcast from the TV`, then a suspend |
| Turn only the soundbar off | `Standby from audio system (5)`, `TV power status: on`, and no suspend |
| Put the PC to sleep from its own menu | `PC dropped off while on screen`, then about 12 seconds later the `on_pc_sleep` action |

A dry run does not send the commands that pull the TV back to the PC, so
automatic input switches will not be undone during it. That only works live.

## Going live

Try it in the foreground first, so you can watch:

```bash
sudo couchlink
```

When you are happy with it, enable the service:

```bash
sudo systemctl enable --now couchlink.service
sudo journalctl -u couchlink.service -f
```

To stop it:

```bash
sudo systemctl disable --now couchlink.service
```

## Updating

From the folder you installed from, run:

```bash
cd Couchlink
sudo bash install.sh --update
```

This downloads the latest release from GitHub and installs it. Other options:

| Command | What it does |
|---|---|
| `sudo bash install.sh --update --release v2.2` | Installs that release |
| `sudo bash install.sh --update --release` | Lists the releases and asks which one to install |
| `bash install.sh --list-releases` | Lists the releases |
| `sudo bash install.sh --update --local` | Installs the files next to `install.sh` instead, for example after `git pull` |

An update does the following, in order:

1. Downloads the release into a temporary folder and runs its installer.
2. Skips the package step.
3. Runs the offline tests.
4. Checks your existing config against the new version.
5. Installs the new script.
6. Restarts the service if it is running.

If the tests or the config check fail, nothing is changed and the installed
version keeps running. Installing a release older than the installed version
asks you to confirm. If the installed version is already the latest release,
nothing happens.

Releases are downloaded from GitHub over HTTPS. They are not signed.

`install.sh` from version 2.2 and earlier does not download releases. Its
`--update` installs the files next to it. Run `git pull` once to get the new
installer.

Your config is never overwritten. The newest example is saved next to it, so
you can see what a new version added:

```bash
diff /etc/couchlink/config.toml /etc/couchlink/config.example.toml
```

## Settings reference

All settings are optional except the ones marked as required. Times are in
seconds.

### The PC

| Setting | Default | Meaning |
|---|---|---|
| `pc_host` | required | The PC's IP address or hostname. An IP with a DHCP reservation is the most reliable. |
| `pc_mac` | required | MAC address of the network card that receives Wake-on-LAN. |
| `pc_name` | unset | Name shown on the status page. If unset, Couchlink asks the PC for its hostname over SSH, then tries reverse DNS. |
| `ssh_user` | `"deck"` | The user the Pi logs in as. Correct for SteamOS; on Bazzite and others, set your own username. |
| `suspend_command` | `"sudo -n systemctl suspend"` | The command run on the PC to put it to sleep. |
| `wol_broadcast` | `"255.255.255.255"` | Where the Wake-on-LAN packet is sent. Try your subnet broadcast, such as `"192.168.1.255"`, if waking is unreliable. |
| `wol_port` | `9` | UDP port for Wake-on-LAN. |

### CEC addresses

| Setting | Default | Meaning |
|---|---|---|
| `pc_addresses` | required | List of inputs that mean the PC is on screen, such as `["12:00"]`. |
| `pi_address` | required | The input the Pi is plugged into. |
| `soundbar_address` | unset | The TV input the soundbar is on. Used to spot automatic switches and for log names. |
| `soundbar_pc_audio_address` | unset | The soundbar input carrying the PC's audio, if the PC's picture goes to the TV directly. |
| `guard_addresses` | `[]` | Extra inputs to treat as automatic switches right after a wake. |
| `pi_logical_address` | `1` | The Pi's CEC logical address. `cec-client` uses 1 by default. |
| `ignore_announcements_from` | `[]` | Logical addresses whose input announcements are ignored, such as `[5]` for a soundbar. |

### Behavior

| Setting | Default | Meaning |
|---|---|---|
| `claim_pc_input` | `true` | When the PC is selected, the Pi announces itself as the active source on the PC's behalf. This keeps the TV and soundbar on the PC and can route remote buttons to the Pi. |
| `auto_switch_on_pc_wake` | `"always"` | When the PC wakes by itself: `"always"` turns the TV on and switches to it, `"if_tv_on"` only switches if the TV is already on, `"never"` does nothing. |
| `on_pc_sleep` | `"nothing"` | When the PC goes to sleep by itself while on screen: `"switch"` changes to `pc_sleep_switch_to`, `"standby"` turns the TV off, `"nothing"` leaves the TV alone. |
| `pc_sleep_switch_to` | unset | The input to change to with `on_pc_sleep = "switch"`. |
| `select_soundbar_audio` | `false` | Experimental. Asks the soundbar to switch to `soundbar_pc_audio_address` when the PC comes on. |
| `verify_non_tv_standby` | `true` | When a device other than the TV sends standby (soundbars often do), ask the TV whether it is really off before acting. |
| `watch_routing_messages` | `true` | Use Routing Change and Routing Information messages. Needed for many TVs. |
| `hid_passthrough` | `true` | Forward TV remote buttons to the PC as a USB keyboard. Needs the `--with-hid` install. |
| `hidg_device` | `"/dev/hidg0"` | The USB keyboard device. |
| `dashboard_port` | `8080` | Port for the status page. `0` turns it off. |

### Timing

| Setting | Default | Meaning |
|---|---|---|
| `away_debounce` | `5` | How long the TV must stay off the PC's input before the PC is suspended. |
| `suspend_retry_interval` | `20` | Time between suspend attempts. |
| `max_suspend_attempts` | `4` | Suspend attempts before giving up and logging an error. |
| `wol_retry_interval` | `10` | Time between Wake-on-LAN packets. |
| `max_wol_attempts` | `6` | Wake-on-LAN packets before giving up. |
| `wake_correlation` | `45` | If the PC comes online this soon after a Wake-on-LAN packet, Couchlink treats it as its own wake. |
| `guard_window` | `15` | How long after a wake an automatic input switch gets undone. |
| `guard_delay` | `1.5` | Pause before undoing an automatic switch. |
| `max_guard_recoveries` | `2` | Automatic switches undone per wake. |
| `auto_switch_cooldown` | `30` | Minimum time between switching the TV to the PC on its own. |
| `tv_boot_timeout` | `20` | When the PC wakes with the TV off: how long to wait for the TV to report that it is on before switching it to the PC anyway. |
| `tv_boot_reclaims` | `3` | After the TV reports that it is on, how many more times to claim the PC's input. |
| `tv_boot_reclaim_interval` | `2` | Time between those claims. Input changes in this period are undone. |
| `pc_sleep_confirm` | `12` | How long the PC must stay offline before it counts as asleep. Stops Wi-Fi hiccups from triggering `on_pc_sleep`. |
| `power_poll_interval` | `60` | How often to ask the TV whether it is on. `0` asks only at startup. |
| `power_query_delays` | `[2, 8]` | When to ask the TV after another device sends standby. |
| `ping_interval` | `3` | How often to check whether the PC is reachable. |
| `tick_interval` | `1` | How often Couchlink re-checks its rules. |

### Diagnostics

| Setting | Default | Meaning |
|---|---|---|
| `raw_debug` | `true` | Log every raw CEC message. Turn off once everything works. |
| `cec_client_command` | `["cec-client", "-d", "8", "-o", "Couchlink"]` | How `cec-client` is started. `-o` sets the name the TV shows for the Pi. |

## Logs and the status page

Every line is `[LEVEL] [COMPONENT] message`. journald adds the time.

```bash
sudo journalctl -u couchlink.service -f                  # live
sudo journalctl -u couchlink.service -f | grep -v DEBUG  # without raw CEC traffic
sudo journalctl -u couchlink.service | grep -E "\[(INPUT|SUSPEND|WOL)\]"
```

| Component | What it logs |
|---|---|
| `INPUT` | Every input change, with the message and sender that caused it |
| `WOL` | Wake-on-LAN packets |
| `SUSPEND` | Suspend attempts and results |
| `WATCHER` | The PC becoming reachable or unreachable, and what Couchlink did about it |
| `RECOVER` | Automatic input switches being undone |
| `TV` | Standby messages and TV power status |
| `CEC-TX` | Every command Couchlink sends, with the reason |
| `CEC` | Raw traffic, when `raw_debug` is on |
| `KEY` | Remote buttons forwarded to the PC |
| `DRY-RUN` | What would have happened |
| `CONFIG`, `STARTUP`, `CONTROL` | Setup problems and unexpected errors |

### Status page

Open `http://<pi>:8080` on a phone or computer on the same network. It updates
every 2 seconds.

![The Couchlink status page with the PC online on HDMI 1.2](docs/dashboard.png)

It shows:

- The PC's name and state (online, waking up, going to sleep, asleep), and the
  path from the TV to the PC. The name comes from `pc_name`, or from the PC's
  own hostname if that is not set.
- TV power and input, whether the PC answers on the network, Wake-on-LAN and
  suspend attempts, the input guard, the sleep check and uptime.
- Recent events and raw CEC traffic, each in its own tab.
- Links to the raw JSON and to this repository.

The page loads nothing from the internet.

`http://<pi>:8080/api` returns the same information as JSON. There is no
login, so keep the page on a trusted network.

## Troubleshooting

| Problem | What to check |
|---|---|
| The service exits straight away | Run `--check-config`. It lists every problem. |
| `Unknown setting` errors | The installed script is older than your config. Run the installer with `--update`. |
| `scan` shows no devices | CEC is off in the TV's settings, or the Pi was not connected to the TV at boot. Reboot with the TV connected. |
| No input changes are logged | Turn on `raw_debug` and look at what your TV sends when you switch. Make sure `watch_routing_messages` is on. |
| The PC never wakes | Test with `wakeonlan <mac>` from the Pi. Check the BIOS and OS wake settings. Try a subnet `wol_broadcast`. |
| `SSH ended with 255`, then `Confirmed: the PC went to sleep` | Normal. The connection drops as the PC suspends. |
| Suspend fails with a password prompt | The sudoers rule is missing or is being overridden. Check the file name starts with `zzz-`. |
| `Host key verification failed` | The SSH key was set up as your user, not root. Redo step 5 of [Preparing the PC](#preparing-the-pc) with `sudo`. |
| `Connection refused` or timeouts on SSH | sshd is not running, or the firewall blocks it. On Bazzite, allow the `ssh` service in firewalld. |
| SSH asks for a password even after `ssh-copy-id` | On Bazzite, run `restorecon -Rv ~/.ssh` on the PC. Also check `ssh_user` matches your account. |
| Suspend stops working after a SteamOS update | Check that `/etc/sudoers.d/zzz-cec-suspend` and sshd survived the update. |
| The PC is suspended while you use it | Check the log for the input that was announced just before. If it came from the soundbar or another device, `ignore_announcements_from` or `guard_addresses` may help. |
| The TV switches away after the PC wakes | Look for `RECOVER` lines. If they appear but the TV does not come back, your TV may ignore the Pi's Active Source messages. |
| The TV is slow to switch to the PC after a controller wake | Look for `TV reports that it is on`. If the log shows `did not report that it is on` instead, your TV does not answer power queries while it starts. Lower `tv_boot_timeout`. |
| Uploads to `/etc/couchlink` fail | That folder belongs to root. Copy the file to your home folder and use `sudo install -m 644 <file> /etc/couchlink/config.toml`. |

## Known limitations

- **CEC varies a lot between brands.** Couchlink has only been tested on the
  hardware listed below. Your TV may announce input changes differently.
- **The Pi cannot ask the TV which input it is on.** It relies on what the TV
  and other devices announce. If the TV switches silently, Couchlink does not
  know.
- **The Pi speaks for the PC.** It announces itself as the active source at
  the PC's address. Some TVs ignore that. If yours does, automatic switching
  to the PC will not work, but waking and suspending will.
- **The Pi's own input is never kept while the PC is awake.** If the TV
  switches to the Pi's input while the PC is on screen, Couchlink switches it
  back. To look at the Pi's own screen, stop `couchlink.service` first.
- **A PC restart looks like going to sleep.** If the PC is offline for longer
  than `pc_sleep_confirm`, the `on_pc_sleep` action runs. When the PC comes
  back, the TV switches back to it.
- **Wake-on-LAN over Wi-Fi is hit and miss**, and never works from
  power-off. Wired Ethernet is much more reliable.
- **Remote buttons only reach the Pi** if the TV sends them to it. That
  depends on the TV accepting the Pi as the active source.
- **Security.** The Pi holds an SSH key that can log in to the PC. The sudoers
  rule limits it to the suspend command. Keep the Pi and the status page on a
  trusted network.

## Files in this repository

| File | Purpose |
|---|---|
| `couchlink.py` | The program. Installed as `/usr/local/bin/couchlink`. |
| `couchlink.example.toml` | Example settings, copied to `/etc/couchlink/config.toml` on first install |
| `install.sh` | Installer and updater |
| `test_couchlink.py` | Offline tests. Run with `python3 test_couchlink.py` |
| `README.md` | This file |
| `CONTRIBUTING.md` | Rules for changes to this repository |
| `docs/dashboard.png` | Screenshot of the status page |

What gets installed on the Pi:

| Path | What it is |
|---|---|
| `/usr/local/bin/couchlink` | The program |
| `/etc/couchlink/config.toml` | Your settings |
| `/etc/systemd/system/couchlink.service` | The service |
| `/usr/local/bin/couchlink-hid-gadget.sh` and `couchlink-hid.service` | The USB keyboard, with `--with-hid` only |

## Tested hardware

Couchlink has run on two setups. Their TVs behave quite differently on the
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
and soundbar models and what you saw in the trace.

## Credits

Couchlink stands on the work of these projects:

- **[libcec](https://github.com/Pulse-Eight/libcec)** by Pulse-Eight, which
  provides `cec-client`. Couchlink does all of its CEC talking through
  `cec-client`, which it runs as a separate program rather than linking to.
  libcec is licensed under the GPL, version 2 or later. On Raspberry Pi OS it
  comes from the Debian `cec-utils` package.
- **The Linux kernel's USB gadget framework** (configfs, `libcomposite` and the
  `dwc2` driver), which lets the Pi act as a USB keyboard. The keyboard's HID
  report descriptor in `install.sh` is the standard boot keyboard descriptor
  from the kernel documentation (`Documentation/usb/gadget_hid.rst`).
- **[PiKVM](https://github.com/pikvm)**, whose kernel patch adds USB remote
  wakeup to the Pi's `dwc2` driver and the `wakeup_on_write` setting that the
  keyboard script turns on when it is available. Couchlink does not include the
  patch. It only uses the setting if your kernel has it.

## Related projects

- **[steamos-cec-toolkit](https://github.com/Twsts/steamos-cec-toolkit)**
  adds console-style CEC behavior by running on the SteamOS PC itself, using a
  CEC-capable display adapter. Couchlink takes the opposite approach: nothing
  is installed on the PC and no special adapter is needed, at the cost of a
  Raspberry Pi.

## Trademarks

Couchlink is an independent project and is not affiliated with, endorsed by,
or sponsored by any of the companies below. All names are used only to
describe compatibility.

HDMI is a trademark of HDMI Licensing Administrator, Inc. Steam and SteamOS
are trademarks of Valve Corporation. Xbox is a trademark of Microsoft
Corporation. Raspberry Pi is a trademark of Raspberry Pi Ltd. Vizio, Samsung,
Anynet+, Google, Gigabyte and Pulse-Eight are trademarks of their respective
owners. Bazzite is a project of Universal Blue.

## License

Couchlink is free software, released under the GNU General Public License,
version 3 or later. See [LICENSE](LICENSE) for the full text.
