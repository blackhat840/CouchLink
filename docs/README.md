# CouchLink documentation

| Guide | What it covers |
|---|---|
| [Installation](installation.md) | What you need, installing on the Pi, the dry run, going live and updating |
| [Preparing the PC](preparing-the-pc.md) | SSH, the suspend rule and waking, on SteamOS and Bazzite |
| [Configuration](configuration.md) | Finding your CEC addresses and changing settings on the settings page, the advanced options in the terminal and the config file, and every setting |
| [Status page and logs](status-page-and-logs.md) | Following what CouchLink does, the wake and sleep buttons, the settings page and the API |
| [Troubleshooting](troubleshooting.md) | Common problems and known limitations |
| [How it works](how-it-works.md) | The layout, the rule CouchLink enforces, the CEC messages it reads, remote sessions and running without a TV |
| [Hardware and systems](hardware.md) | Supported PC systems and tested TVs and soundbars |
| [Credits](credits.md) | Projects CouchLink builds on, related projects and trademarks |

## Files in this repository

| File | Purpose |
|---|---|
| `couchlink/` | The program. Installed in `/usr/local/lib/couchlink`, with the `couchlink` command in `/usr/local/bin`. |
| `couchlink/static/` | The status page and the settings page: their HTML, CSS and JavaScript |
| `couchlink.example.toml` | Example settings, copied to `/etc/couchlink/config.toml` on first install |
| `install.sh` | Installer and updater |
| `tests/` | Offline tests, one file for each part of the program |
| `test_couchlink.py` | Runs the tests: `python3 test_couchlink.py`. `install.sh --test` runs them with `--quick`, which skips the tests of the installer itself. |
| `README.md` | Overview and quick start |
| `CONTRIBUTING.md` | Rules for changes to this repository |
| `docs/` | These guides and the status page screenshot |

### The program

| Module | What it does |
|---|---|
| `controller.py` | All the decisions: which input the TV is on, when to wake and suspend the PC, remote sessions. It owns all state and runs on one thread. |
| `cec.py` | Talks to the HDMI-CEC bus through `cec-client`, and reads what it prints |
| `actions.py` | Wake-on-LAN, suspending over SSH, and the dry run |
| `power.py` | Pinging the PC, and the SSH session that hears it sleep, restart or shut down |
| `web.py` | The status page and its API |
| `admin.py` | The settings page: login, settings, API keys and the password |
| `auth.py` | API keys, passwords, login cookies and lockouts |
| `config.py` | Settings: defaults, checking, writing and `--migrate-config` |
| `setup_pc.py`, `setup_cec.py` | `--setup` and `--setup-cec` |
| `cli.py` | The `couchlink` command |

To run it from a checkout without installing: `python3 -m couchlink --dry-run`.

---

Back to the [README](../README.md).
