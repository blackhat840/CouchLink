# Installation

Setting up CouchLink on the Raspberry Pi, testing it, and keeping it up to date.

## What you need

| Item | Notes |
|---|---|
| Raspberry Pi | Any model with HDMI. The optional USB keyboard needs USB gadget support (Pi Zero, Zero W, Zero 2 W, Pi 4). Developed on a Pi Zero W. |
| Raspberry Pi OS Lite | Bookworm or newer, for Python 3.11+. Use the 32-bit image on a Zero W. |
| HDMI cable | Mini HDMI for the Zero family. |
| A CEC-capable TV | CEC must be enabled in the TV's settings. Brands call it different names, such as Anynet+, Bravia Sync, SimpLink or just CEC. |
| A gaming PC | SteamOS, Bazzite or a similar distro. See [Hardware and systems](hardware.md). |
| A network | The Pi and the PC on the same LAN. |

Wired Ethernet is the most reliable way to wake the PC. See
[Preparing the PC](preparing-the-pc.md).

## Step by step

1. **Flash the SD card.** In Raspberry Pi Imager, choose Raspberry Pi OS Lite.
   In the settings, set a hostname (for example `couchlink`), a username and
   password, your Wi-Fi network and country, and turn on SSH.

2. **Plug the Pi into the TV** before powering it on, and power it from the
   PWR port. The first boot takes a few minutes.

3. **Log in to the Pi** from your computer. Open Terminal on a Mac or Linux,
   or PowerShell on Windows, and run this with the username and hostname you
   set in step 1:

   ```bash
   ssh <username>@couchlink.local
   ```

4. **Download and run the installer** on the Pi:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/blackhat840/CouchLink/main/install.sh
   sudo bash install.sh
   ```

   To also set up the USB keyboard for TV remote buttons, run
   `sudo bash install.sh --with-hid` instead, then reboot when it finishes.

   The installer downloads the latest release from GitHub, installs
   `cec-utils`, runs the offline tests, installs the program as
   `/usr/local/bin/couchlink`, creates `/etc/couchlink/config.toml`, and
   installs `couchlink.service` without enabling it. You do not need git or a
   GitHub account. At the end it asks for the settings (see
   [Configuration](configuration.md#the-settings)), then finds the CEC
   addresses with you (see
   [Finding your CEC addresses](configuration.md#finding-your-cec-addresses)),
   and prints the next steps.

   If you prefer git, `git clone https://github.com/blackhat840/CouchLink.git`,
   then `cd CouchLink` and `sudo bash install.sh` installs the files from the
   clone instead.

5. **Find your CEC addresses**, if you skipped it in step 4. Turn the TV on,
   then run `sudo bash install.sh --setup-cec`. See
   [Configuration](configuration.md#finding-your-cec-addresses).

6. **Prepare the PC** so the Pi can wake it and put it to sleep. See
   [Preparing the PC](preparing-the-pc.md).

7. **Do a dry run**, then go live. See below.

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
| Put the PC to sleep from its own menu | `PC went offline while on screen`, then about 12 seconds later the `on_pc_sleep` action |

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

Log in to the Pi and run this in the folder that holds `install.sh`. If you
followed the steps above, that is your home folder:

```bash
sudo bash install.sh --update
```

This downloads the latest release from GitHub and installs it. Other options:

| Command | What it does |
|---|---|
| `sudo bash install.sh --release v2.3.1` | Installs that release |
| `sudo bash install.sh --release` | Lists the releases and asks which one to install |
| `bash install.sh --list-releases` | Lists the releases |
| `sudo bash install.sh --update --local` | Installs the files next to `install.sh` instead, for example after `git pull` |

An update does the following, in order:

1. Downloads the release into a temporary folder and runs its installer.
2. Skips the package step.
3. Runs the offline tests.
4. Brings a copy of your config up to date for the new version and checks
   it. See [Updating an older config](configuration.md#updating-an-older-config).
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

Your settings are always kept. If the new version adds or changes settings,
your config is brought up to date and the previous version is saved as
`config.toml.bak`. The newest example is saved next to it, so you can see
what a new version added:

```bash
diff /etc/couchlink/config.toml /etc/couchlink/config.example.toml
```

## What gets installed

| Path | What it is |
|---|---|
| `/usr/local/bin/couchlink` | The program |
| `/etc/couchlink/config.toml` | Your settings |
| `/etc/systemd/system/couchlink.service` | The service |
| `/usr/local/bin/couchlink-hid-gadget.sh` and `couchlink-hid.service` | The USB keyboard, with `--with-hid` only |

---

Back to the [Documentation](README.md) or the [README](../README.md).
