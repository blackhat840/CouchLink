# Troubleshooting

Common problems, what to check, and the known limitations.

## Common problems

| Problem | What to check |
|---|---|
| The service exits straight away | Run `--check-config`. It lists every problem. |
| `Unknown setting` errors | The installed version is older than your config, or the setting has a typo. Run the installer with `--update`, or run `sudo couchlink --migrate-config` to fix typos and old settings. |
| `scan` shows no devices, or `--setup-cec` says no CEC devices answered | CEC is off in the TV's settings, or the Pi was not connected to the TV at boot. Reboot with the TV connected. |
| `--setup-cec` does not hear the switch to the PC | Your TV may not announce input changes. Type the input number when it asks. If the PC is already on screen, switch away first, then back. |
| No input changes are logged | Turn on `raw_debug` and look at what your TV sends when you switch. Make sure `watch_routing_messages` is on. |
| The PC never wakes | Test with `wakeonlan <mac>` from the Pi. Check the BIOS and OS wake settings. Try a subnet `wol_broadcast`. |
| `SSH ended with 255`, then `Confirmed: the PC went to sleep` | Normal. The connection drops as the PC suspends. |
| Suspend fails with a password prompt | The sudoers rule is missing or is being overridden. Check the file name starts with `zzz-`. |
| `Host key verification failed` | The SSH key was set up as your user, not root. Redo [step 5 of Preparing the PC](preparing-the-pc.md#5-give-the-pi-a-key-the-pc-trusts) with `sudo`. |
| `No route to host` on SSH | The PC is asleep or off, or on Bazzite the firewall blocks SSH. Wake the PC and try again. On Bazzite, run `sudo firewall-cmd --permanent --add-service=ssh && sudo firewall-cmd --reload` on the PC. |
| `Connection refused` or timeouts on SSH | sshd is not running, or the firewall blocks it. On Bazzite, allow the `ssh` service in firewalld. |
| SSH asks for a password even after `ssh-copy-id` | On Bazzite, run `restorecon -Rv ~/.ssh` on the PC. Also check `ssh_user` matches your account. |
| Suspend stops working after a SteamOS update | Check that `/etc/sudoers.d/zzz-cec-suspend` and sshd survived the update. |
| The status page says "Asleep or off" | CouchLink can't open its SSH session to the PC. Check that `sudo ssh <user>@<pc-ip> "echo ok"` works on the Pi, and look for `Can't watch the PC's power state` in the log. |
| The PC is suspended while you use it | Check the log for the input that was announced just before. If it came from the soundbar or another device, `ignore_announcements_from` or `guard_addresses` may help. |
| The TV switches away after the PC wakes | Look for `RECOVER` lines. If they appear but the TV does not come back, your TV may ignore the Pi's Active Source messages. |
| The TV is slow to switch to the PC after a controller wake | Look for `TV reports that it is on`. If the log shows `did not report that it is on` instead, your TV does not answer power queries while it starts. Lower `tv_boot_timeout`. |
| The status page has no wake and sleep buttons | Check that `status_page_buttons` isn't set to `false`, and that the Pi runs 2.6 or newer. |
| A script gets `Wrong API key` after you generated a new one | Generating a key with the same name replaces it. Update the key in the script, or give each script its own key. |
| You forgot the settings page's password | Set a new one with `sudo couchlink --admin-password`. |
| The settings page says to set a password first | Set one with `sudo couchlink --admin-password`. |
| After saving settings, the page waits | CouchLink restarts to use them, which takes a few seconds, longer on a Pi Zero. If it doesn't come back, check `sudo journalctl -u couchlink.service -n 50`. A wrong value isn't saved, so a restart doesn't fail because of one. |
| The API answers `locked_out` | 5 wrong keys came from that address within a minute. Wait a minute. |
| The TV turns on when you wake the PC to stream | With a TV, this means the PC was woken by something else, such as Moonlight or a phone app, so CouchLink treats it like a controller wake. Use Wake PC on the status page or `POST /api/wake` with `{"tv": false}` instead. If you never use the TV with the PC, run CouchLink [without a TV](how-it-works.md#without-a-tv). |
| The PC goes back to sleep while you stream to another device | Start the stream with Wake PC, which starts a [remote session](how-it-works.md#remote-sessions). Without one, CouchLink suspends the PC when the TV is not on its input. Without a TV, check the PC's own sleep timer. |
| Uploads to `/etc/couchlink` fail | That folder belongs to root. Copy the file to your home folder and use `sudo install -m 600 <file> /etc/couchlink/config.toml`. The config is readable by root only, because it holds password and key hashes. |

## Known limitations

- **CEC varies a lot between brands.** CouchLink has only been tested on the
  hardware in
  [Hardware and systems](hardware.md). Your TV may announce input changes differently.
- **The Pi cannot ask the TV which input it is on.** It relies on what the TV
  and other devices announce. If the TV switches silently, CouchLink does not
  know.
- **The Pi speaks for the PC.** It announces itself as the active source at
  the PC's address. Some TVs ignore that. If yours does, automatic switching
  to the PC will not work, but waking and suspending will.
- **The Pi's own input is never kept while the PC is awake.** If the TV
  switches to the Pi's input while the PC is on screen, CouchLink switches it
  back. To look at the Pi's own screen, stop `couchlink.service` first.
- **A restart can look like a power-off on older systemd.** CouchLink only
  knows a shutdown is a restart if the PC says so. If it doesn't, and the PC is
  offline for longer than `pc_sleep_confirm`, the `on_pc_sleep` action runs.
  When the PC comes back, the TV switches back to it.
- **Wake-on-LAN over Wi-Fi is hit and miss**, and never works from
  power-off. Wired Ethernet is much more reliable.
- **Remote buttons only reach the Pi** if the TV sends them to it. That
  depends on the TV accepting the Pi as the active source.
- **Only CouchLink can start a remote session.** With a TV, waking the PC any
  other way, such as with Moonlight's own Wake-on-LAN, looks the same as a
  controller wake, so the TV turns on. Without a TV, the PC is left alone.
- **Security.** The Pi holds an SSH key that can log in to the PC. The sudoers
  rule limits it to the suspend command. The status page and its wake and sleep
  buttons have no login. Scripts need an API key, and the settings page needs
  a password. CouchLink serves plain HTTP, so passwords and keys cross the
  network unencrypted. Keep the Pi on a trusted network, and use a VPN to reach
  it from outside the home.

---

Back to the [Documentation](README.md) or the [README](../README.md).
