#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Couchlink installer and updater for Raspberry Pi OS.
#
# Run from the folder that holds couchlink.py, couchlink.example.toml and
# (optionally) test_couchlink.py:
#
#     sudo bash install.sh              # first install
#     sudo bash install.sh --with-hid   # first install + USB keyboard for remote buttons
#     sudo bash install.sh --update     # update an existing install
#
# First install:
#   1. Installs cec-utils and ping
#   2. Runs the offline tests (if test_couchlink.py is here)
#   3. Installs the program as /usr/local/bin/couchlink
#   4. Creates /etc/couchlink/config.toml from the example (never overwrites yours)
#   5. Installs couchlink.service, left disabled until you've tested
#   6. With --with-hid: enables USB gadget mode and a keyboard gadget service
#      (takes effect after a reboot)
#
# --update skips the package step, runs the tests, checks your existing config
# against the new version BEFORE replacing anything, installs it, and restarts
# the service if it's running. If the tests or the config check fail, the
# installed version is left untouched.
set -euo pipefail

ROOT="${ROOT:-}"                 # for testing only: install into a fake root
TEST_MODE="${INSTALL_TEST:-0}"   # for testing only: skip apt, systemctl
WITH_HID=0
UPDATE=0
for arg in "$@"; do
    case "$arg" in
        --with-hid) WITH_HID=1 ;;
        --update) UPDATE=1 ;;
        -h|--help) sed -n '5,26p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*"; exit 1; }
run()  { if [ "$TEST_MODE" = 1 ]; then echo "[test mode] skipped: $*"; else "$@"; fi; }
version_of() { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$1" 2>/dev/null | head -1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$TEST_MODE" = 1 ] || [ "$(id -u)" = 0 ] || die "Run with sudo: sudo bash $0 $*"
[ -f "$HERE/couchlink.py" ] || die "couchlink.py not found next to this script"
[ -f "$HERE/couchlink.example.toml" ] || die "couchlink.example.toml not found next to this script"

BIN="$ROOT/usr/local/bin/couchlink"
CONF_DIR="$ROOT/etc/couchlink"
CONF="$CONF_DIR/config.toml"
UNIT_DIR="$ROOT/etc/systemd/system"
NEW_VER="$(version_of "$HERE/couchlink.py")"
OLD_VER=""

if [ "$UPDATE" = 1 ]; then
    [ -f "$BIN" ] && [ -f "$CONF" ] || die "No existing install found. Run without --update first."
    OLD_VER="$(version_of "$BIN")"
    if [ "$TEST_MODE" != 1 ]; then
        command -v cec-client >/dev/null || die "cec-client is missing. Run without --update to reinstall packages."
    fi
    say "Updating Couchlink ${OLD_VER:-?} -> ${NEW_VER:-?}"
fi

# ---------------------------------------------------------------- python
say "Checking Python"
python3 - <<'PYCHECK' || die "Python 3.11 or newer is needed (Raspberry Pi OS Bookworm or newer)"
import sys
print(f"Python {sys.version.split()[0]}")
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYCHECK

# ---------------------------------------------------------------- packages
if [ "$UPDATE" = 1 ]; then
    echo "Skipping packages (--update)"
else
    say "Installing packages (cec-utils, ping)"
    run apt-get update
    run apt-get install -y cec-utils iputils-ping
fi

# ---------------------------------------------------------------- tests
if [ -f "$HERE/test_couchlink.py" ]; then
    say "Running offline tests"
    if ( cd "$HERE" && python3 test_couchlink.py ) >/tmp/couchlink-test.log 2>&1; then
        grep -E "^Ran|^OK" /tmp/couchlink-test.log
    else
        tail -25 /tmp/couchlink-test.log
        die "Offline tests failed, so nothing was installed. Full log: /tmp/couchlink-test.log"
    fi
else
    warn "test_couchlink.py not found, skipping offline tests"
fi

# ---------------------------------------------------------------- update pre-check
if [ "$UPDATE" = 1 ]; then
    say "Checking your config against the new version"
    if python3 "$HERE/couchlink.py" --config "$CONF" --check-config >/tmp/couchlink-check.log 2>&1; then
        echo "Config OK with ${NEW_VER:-the new version}"
    else
        grep CONFIG /tmp/couchlink-check.log || cat /tmp/couchlink-check.log
        die "Your config doesn't pass with the new version, so nothing was changed. Fix it (compare with couchlink.example.toml) and run --update again."
    fi
fi

# ---------------------------------------------------------------- program
say "Installing Couchlink"
install -d "$ROOT/usr/local/bin" "$CONF_DIR" "$UNIT_DIR"
install -m 0755 "$HERE/couchlink.py" "$BIN"
echo "Installed $BIN (${NEW_VER:-unknown version})"

if [ -f "$CONF" ]; then
    echo "Kept your existing $CONF"
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF_DIR/config.example.toml"
    echo "Latest example saved as $CONF_DIR/config.example.toml for comparison"
else
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF"
    echo "Created $CONF from the example"
fi

SERVICE_FILE="$UNIT_DIR/couchlink.service"
SERVICE_NEW="$(mktemp)"
cat > "$SERVICE_NEW" <<'UNIT'
[Unit]
Description=Couchlink (keeps a gaming PC in step with the TV over HDMI-CEC)
After=network-online.target couchlink-hid.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -u /usr/local/bin/couchlink
Restart=always
RestartSec=2
RestartPreventExitStatus=78

[Install]
WantedBy=multi-user.target
UNIT
SERVICE_CHANGED=0
if [ -f "$SERVICE_FILE" ] && cmp -s "$SERVICE_NEW" "$SERVICE_FILE"; then
    echo "couchlink.service unchanged"
else
    install -m 0644 "$SERVICE_NEW" "$SERVICE_FILE"
    SERVICE_CHANGED=1
    if [ "$UPDATE" = 1 ]; then
        echo "Updated couchlink.service"
    else
        echo "Installed couchlink.service"
    fi
fi
rm -f "$SERVICE_NEW"

# ---------------------------------------------------------------- USB keyboard gadget
REBOOT_NEEDED=0
if [ "$WITH_HID" = 1 ]; then
    say "Setting up the USB keyboard gadget"
    BOOT="$ROOT/boot/firmware"
    [ -d "$BOOT" ] || BOOT="$ROOT/boot"
    CONFIG_TXT="$BOOT/config.txt"
    CMDLINE_TXT="$BOOT/cmdline.txt"
    [ -f "$CONFIG_TXT" ] || die "$CONFIG_TXT not found"
    [ -f "$CMDLINE_TXT" ] || die "$CMDLINE_TXT not found"

    if grep -qE '^\s*dtoverlay=dwc2' "$CONFIG_TXT"; then
        echo "config.txt already has dtoverlay=dwc2"
    else
        cp "$CONFIG_TXT" "$CONFIG_TXT.bak-couchlink"
        printf '\n[all]\n# USB device (gadget) mode for the Couchlink keyboard\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CONFIG_TXT"
        echo "Added dtoverlay=dwc2 to config.txt (backup: config.txt.bak-couchlink)"
        REBOOT_NEEDED=1
    fi

    if grep -q 'modules-load=[^ ]*dwc2' "$CMDLINE_TXT"; then
        echo "cmdline.txt already loads dwc2"
    else
        cp "$CMDLINE_TXT" "$CMDLINE_TXT.bak-couchlink"
        # cmdline.txt must stay ONE line: insert after rootwait, or append if rootwait is missing
        if grep -q 'rootwait' "$CMDLINE_TXT"; then
            sed -i '1 s/rootwait/rootwait modules-load=dwc2/' "$CMDLINE_TXT"
        else
            sed -i '1 s/$/ modules-load=dwc2/' "$CMDLINE_TXT"
        fi
        echo "Added modules-load=dwc2 to cmdline.txt (backup: cmdline.txt.bak-couchlink)"
        REBOOT_NEEDED=1
    fi

    cat > "$ROOT/usr/local/bin/couchlink-hid-gadget.sh" <<'GADGET'
#!/bin/bash
# USB keyboard gadget for Couchlink. Safe to run more than once.
#
# Uses the Linux USB gadget configfs interface. The HID report descriptor is
# the standard boot keyboard descriptor from the Linux kernel documentation
# (Documentation/usb/gadget_hid.rst).
set -e
modprobe libcomposite
# A gadget is already bound to the USB port: nothing to do.
for udc in /sys/kernel/config/usb_gadget/*/UDC; do
    if [ -f "$udc" ] && [ -n "$(cat "$udc" 2>/dev/null)" ]; then
        echo "A USB gadget is already active ($(dirname "$udc"))"; exit 0
    fi
done
G=/sys/kernel/config/usb_gadget/couchlink
mkdir -p "$G" && cd "$G"
echo 0x1d6b > idVendor                    # Linux Foundation
echo 0x0104 > idProduct                   # Multifunction Composite Gadget
mkdir -p strings/0x409
echo "0001"               > strings/0x409/serialnumber
echo "Couchlink"          > strings/0x409/manufacturer
echo "Couchlink Keyboard" > strings/0x409/product
mkdir -p configs/c.1/strings/0x409
echo "Config 1" > configs/c.1/strings/0x409/configuration
echo 250  > configs/c.1/MaxPower
echo 0xa0 > configs/c.1/bmAttributes      # bus powered + remote wakeup
mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol       # keyboard
echo 1 > functions/hid.usb0/subclass       # boot interface
echo 8 > functions/hid.usb0/report_length
# wakeup_on_write only exists on kernels with PiKVM's remote wakeup patch.
if [ -e functions/hid.usb0/wakeup_on_write ]; then
    echo 1 > functions/hid.usb0/wakeup_on_write
fi
echo -ne \\x05\\x01\\x09\\x06\\xa1\\x01\\x05\\x07\\x19\\xe0\\x29\\xe7\\x15\\x00\\x25\\x01\\x75\\x01\\x95\\x08\\x81\\x02\\x95\\x01\\x75\\x08\\x81\\x03\\x95\\x05\\x75\\x01\\x05\\x08\\x19\\x01\\x29\\x05\\x91\\x02\\x95\\x01\\x75\\x03\\x91\\x03\\x95\\x06\\x75\\x08\\x15\\x00\\x25\\x65\\x05\\x07\\x19\\x00\\x29\\x65\\x81\\x00\\xc0 > functions/hid.usb0/report_desc
[ -e configs/c.1/hid.usb0 ] || ln -s functions/hid.usb0 configs/c.1/
ls /sys/class/udc > UDC
echo "USB keyboard gadget active on $(cat UDC)"
GADGET
    chmod 0755 "$ROOT/usr/local/bin/couchlink-hid-gadget.sh"

    cat > "$UNIT_DIR/couchlink-hid.service" <<'HIDUNIT'
[Unit]
Description=Couchlink USB keyboard gadget
After=sys-kernel-config.mount
Before=couchlink.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/couchlink-hid-gadget.sh

[Install]
WantedBy=multi-user.target
HIDUNIT
    run systemctl daemon-reload
    run systemctl enable couchlink-hid.service
    echo "Installed and enabled couchlink-hid.service"
fi

if [ "$SERVICE_CHANGED" = 1 ] || [ "$WITH_HID" = 1 ]; then
    run systemctl daemon-reload
fi

# ---------------------------------------------------------------- update: restart and finish
if [ "$UPDATE" = 1 ]; then
    say "Restarting"
    if [ "$TEST_MODE" = 1 ]; then
        echo "[test mode] skipped: restart couchlink.service if active"
    elif systemctl is-active --quiet couchlink.service; then
        systemctl restart couchlink.service
        sleep 3
        if systemctl is-active --quiet couchlink.service; then
            echo "couchlink.service restarted and running ${NEW_VER}"
        else
            warn "The service didn't stay up. Check: sudo journalctl -u couchlink.service -n 50"
        fi
    else
        echo "couchlink.service isn't running, so there's nothing to restart."
        echo "The new version is used the next time it starts (service or foreground)."
    fi
    say "Updated ${OLD_VER:-?} -> ${NEW_VER:-?}. Your config was kept."
    echo "To see settings added in the new version:"
    echo "  diff /etc/couchlink/config.toml /etc/couchlink/config.example.toml"
    if [ "$REBOOT_NEEDED" = 1 ]; then
        warn "Reboot needed for the USB keyboard gadget: sudo reboot"
    fi
    exit 0
fi

# ---------------------------------------------------------------- config check
say "Checking the config"
if python3 "$BIN" --config "$CONF" --check-config >/tmp/couchlink-check.log 2>&1; then
    echo "Config OK"
else
    grep CONFIG /tmp/couchlink-check.log || cat /tmp/couchlink-check.log
    echo "(Expected on a fresh install: fill in the placeholders, see below.)"
fi

# ---------------------------------------------------------------- next steps
say "Done. Next steps"
cat <<STEPS
1. Find the CEC addresses (stop the service first if it's running):
     sudo systemctl stop couchlink.service 2>/dev/null; echo "scan" | cec-client -s -d 1
2. Fill in the config:
     sudo nano /etc/couchlink/config.toml
     sudo couchlink --check-config
3. Dry run (changes nothing; watch it decide). Ctrl+C to stop:
     sudo couchlink --dry-run
   Status page: http://$(hostname 2>/dev/null || echo '<pi>').local:8080
4. Before going live, give root an SSH key the PC trusts:
     sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519
     sudo ssh-copy-id deck@<pc-ip>
     sudo ssh deck@<pc-ip> "echo ok"
5. Go live:
     sudo systemctl enable --now couchlink.service
     sudo journalctl -u couchlink.service -f
6. Later updates: copy or pull the new files here and run
     sudo bash install.sh --update
STEPS
if [ "$REBOOT_NEEDED" = 1 ]; then
    warn "Reboot needed for the USB keyboard gadget: sudo reboot"
fi
