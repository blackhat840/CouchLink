# CouchLink

Make a SteamOS or Bazzite gaming PC behave like a console on your TV.

A Raspberry Pi on the TV's HDMI-CEC bus keeps the PC and the TV in sync. It
works with SteamOS, Bazzite and any other Linux PC that uses systemd. Nothing
is installed on the PC.

![The CouchLink status page with the PC online on HDMI 1.2](docs/dashboard.png)

## What it does

- **Wakes the PC** with Wake-on-LAN when the TV or soundbar is switched to the
  PC's input.
- **Suspends the PC** over SSH when you switch to another input or turn the TV
  off.
- **Switches the TV to the PC** when the PC wakes up on its own, for example
  from a controller. It turns the TV on first if needed.
- **Handles Sleep from the Steam menu.** CouchLink can switch the TV to another
  input, turn it off, or leave it alone.
- **Remote play.** Wake the PC from the status page and keep it awake without
  the TV, for streaming to a Steam Deck or phone with Steam Link or Moonlight.
  It also works [without a TV](docs/how-it-works.md#without-a-tv), as a wake
  and sleep switch for remote play only.
- **Wake and sleep buttons** on the status page, and an API with named keys
  for scripts, phone shortcuts and Home Assistant.
- **A settings page** with a password, to change settings and API keys from a
  browser.
- **Undoes automatic input switches** that some TVs and soundbars make right
  after a device wakes up.
- **Optional:** forwards TV remote buttons to the PC as a USB keyboard.
- **Setup in a browser:** after the installer, the settings page finds the
  PC's MAC address and the HDMI inputs with you. The terminal and the config
  file are still there for advanced setups.
- **Safe to try:** a dry run that changes nothing, a status page, and offline
  tests.

## What you need

- A Raspberry Pi with HDMI, such as a Pi Zero W, on Raspberry Pi OS Lite
  (Bookworm or newer).
- A TV with HDMI-CEC turned on, unless you only want
  [remote play without a TV](docs/how-it-works.md#without-a-tv).
- A gaming PC running SteamOS, Bazzite or another Linux with systemd, on the
  same network as the Pi. Wired Ethernet is the most reliable way to wake it.

See [Installation](docs/installation.md#what-you-need) for details.

## Quick start

On the Pi, plugged into the TV and logged in over SSH:

```bash
curl -fsSLO https://raw.githubusercontent.com/blackhat840/CouchLink/main/install.sh
sudo bash install.sh
```

The installer downloads the latest release and sets everything up. No git or
GitHub account needed. New to the Pi? Follow the
[step by step guide](docs/installation.md#step-by-step).

Then:

1. Open the settings page the installer shows, such as
   `http://192.168.1.20:8080/admin`, on your phone or computer. Enter the setup
   code it printed and choose a password.
2. Fill in the PC's address, and with the TV on, let **Find the HDMI inputs**
   find the PC, the Pi and any soundbar. Save, and CouchLink starts working.
3. Set up the PC so the Pi can wake it and put it to sleep. See
   [Preparing the PC](docs/preparing-the-pc.md).
4. Give the Pi an SSH key the PC trusts: `sudo bash install.sh --setup-ssh`.

![Find the HDMI inputs on the settings page, with the PC, the Pi and the soundbar found](docs/setup-hdmi-inputs.png)

Rather set up in the terminal, or edit the config file yourself? See
[Advanced setup](docs/installation.md#advanced-setup).

## Updating

On the Pi, in the folder that holds `install.sh`:

```bash
sudo bash install.sh --update
```

This installs the latest release from GitHub, keeps your settings and brings
your config up to date. See
[Updating](docs/installation.md#updating) for the other options.

## Documentation

- [Installation](docs/installation.md)
- [Preparing the PC](docs/preparing-the-pc.md)
- [Configuration](docs/configuration.md)
- [Status page and logs](docs/status-page-and-logs.md)
- [Troubleshooting](docs/troubleshooting.md)
- [How it works](docs/how-it-works.md)
- [Hardware and systems](docs/hardware.md)
- [Credits](docs/credits.md)

## Project status

CouchLink is a work in progress. It has been checked on the hardware in
[Hardware and systems](docs/hardware.md), and its logic is covered by offline
tests. CEC behavior varies a lot between brands, so expect to spend some time
with the dry run on your own TV. Reports from other setups are welcome as
issues.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

CouchLink is free software, released under the GNU General Public License,
version 3 or later. See [LICENSE](LICENSE) for the full text. It is an
independent project and is not affiliated with any of the companies named in
it. See [Credits](docs/credits.md#trademarks).
