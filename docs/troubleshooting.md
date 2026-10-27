# Troubleshooting

Common problems, what to check, and the known limitations.

## Common problems

| Problem | What to check |
|---|---|
| The service exits straight away | Run `--check-config`. It lists every problem. |
| `Unknown setting` errors | The installed script is older than your config. Run the installer with `--update`. |
| `scan` shows no devices | CEC is off in the TV's settings, or the Pi was not connected to the TV at boot. Reboot with the TV connected. |
| No input changes are logged | Turn on `raw_debug` and look at what your TV sends when you switch. Make sure `watch_routing_messages` is on. |
| The PC never wakes | Test with `wakeonlan <mac>` from the Pi. Check the BIOS and OS wake settings. Try a subnet `wol_broadcast`. |
| `SSH ended with 255`, then `Confirmed: the PC went to sleep` | Normal. The connection drops as the PC suspends. |
| Suspend fails with a password prompt | The sudoers rule is missing or is being overridden. Check the file name starts with `zzz-`. |
| `Host key verification failed` | The SSH key was set up as your user, not root. Redo [step 5 of Preparing the PC](preparing-the-pc.md#5-give-the-pi-a-key-the-pc-trusts) with `sudo`. |
| `Connection refused` or timeouts on SSH | sshd is not running, or the firewall blocks it. On Bazzite, allow the `ssh` service in firewalld. |
| SSH asks for a password even after `ssh-copy-id` | On Bazzite, run `restorecon -Rv ~/.ssh` on the PC. Also check `ssh_user` matches your account. |
| Suspend stops working after a SteamOS update | Check that `/etc/sudoers.d/zzz-cec-suspend` and sshd survived the update. |
| The PC is suspended while you use it | Check the log for the input that was announced just before. If it came from the soundbar or another device, `ignore_announcements_from` or `guard_addresses` may help. |
| The TV switches away after the PC wakes | Look for `RECOVER` lines. If they appear but the TV does not come back, your TV may ignore the Pi's Active Source messages. |
| The TV is slow to switch to the PC after a controller wake | Look for `TV reports that it is on`. If the log shows `did not report that it is on` instead, your TV does not answer power queries while it starts. Lower `tv_boot_timeout`. |
| Uploads to `/etc/couchlink` fail | That folder belongs to root. Copy the file to your home folder and use `sudo install -m 644 <file> /etc/couchlink/config.toml`. |

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

---

Back to the [Documentation](README.md) or the [README](../README.md).
