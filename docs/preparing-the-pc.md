# Preparing the PC

What to set up on the gaming PC so the Pi can wake it and put it to sleep. Nothing is installed on the PC.

Do this once on the gaming PC. Run the commands in a terminal: on SteamOS,
switch to Desktop Mode and open Konsole. On Bazzite, open the terminal app from
Desktop Mode or the desktop.

In the examples below, replace `deck` with your username and `192.168.1.50`
with the PC's IP address. On SteamOS the user is always `deck`. On Bazzite it
is the account you created during setup, so set `ssh_user` in CouchLink's config
to match.

## 1. Set a password

sudo needs one, and fresh SteamOS installs do not have one.

```bash
passwd
```

Bazzite normally sets one during setup. Run `passwd` anyway if yours does not
have one.

## 2. Turn on the SSH server

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

## 3. Allow suspend without a password

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

## 4. Set up waking

CouchLink needs some way to wake the PC. Pick whatever your PC supports. You
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
If the PC wakes, CouchLink notices within a few seconds and switches the TV
and soundbar to it. You still need network waking for the PC to wake when you
pick its input on the TV. Some controllers need a udev rule before they can
wake the PC. Bazzite ships these rules for the controllers it knows about.

## 5. Give the Pi a key the PC trusts

The service on the Pi runs as root, so the key must belong to root on the Pi.
`sudo couchlink --setup` on the Pi offers to do this for you. To do it by
hand, run these **on the Pi**:

```bash
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519
sudo ssh-copy-id deck@192.168.1.50
sudo ssh deck@192.168.1.50 "echo ok"
```

The last command should print `ok` without asking for a password. Then test
the exact command CouchLink will use. This puts the PC to sleep:

```bash
sudo ssh deck@192.168.1.50 "sudo -n systemctl suspend"
```

On Bazzite and other SELinux systems, if the key is copied but SSH still asks
for a password, fix the labels on the PC's `.ssh` folder:

```bash
restorecon -Rv ~/.ssh
```

## 6. Give the PC a fixed address

Reserve the PC's IP address in your router's DHCP settings and put that IP in
`pc_host`. Names like `steamdeck.local` depend on mDNS working on both
machines and are less reliable.

---

Back to the [Documentation](README.md) or the [README](../README.md).
