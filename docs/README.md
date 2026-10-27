# CouchLink documentation

| Guide | What it covers |
|---|---|
| [Installation](installation.md) | What you need, installing on the Pi, the dry run, going live and updating |
| [Preparing the PC](preparing-the-pc.md) | SSH, the suspend rule and waking, on SteamOS and Bazzite |
| [Configuration](configuration.md) | Guided setup, finding your CEC addresses, the config file and every setting |
| [Status page and logs](status-page-and-logs.md) | Following what CouchLink does, the wake and sleep buttons, and the API |
| [Troubleshooting](troubleshooting.md) | Common problems and known limitations |
| [How it works](how-it-works.md) | The layout, the rule CouchLink enforces, the CEC messages it reads, and remote sessions |
| [Hardware and systems](hardware.md) | Supported PC systems and tested TVs and soundbars |
| [Credits](credits.md) | Projects CouchLink builds on, related projects and trademarks |

## Files in this repository

| File | Purpose |
|---|---|
| `couchlink.py` | The program. Installed as `/usr/local/bin/couchlink`. |
| `couchlink.example.toml` | Example settings, copied to `/etc/couchlink/config.toml` on first install |
| `install.sh` | Installer and updater |
| `test_couchlink.py` | Offline tests. Run with `python3 test_couchlink.py`. The installer adds `--quick`, which skips the tests of the installer itself. |
| `README.md` | Overview and quick start |
| `CONTRIBUTING.md` | Rules for changes to this repository |
| `docs/` | These guides and the status page screenshot |

---

Back to the [README](../README.md).
