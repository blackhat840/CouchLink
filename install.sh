#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# CouchLink installer and updater for Raspberry Pi OS.
#
# install.sh works on its own. If couchlink.py is not next to it, it downloads
# the latest release from GitHub and installs that:
#
#     curl -fsSLO https://raw.githubusercontent.com/blackhat840/CouchLink/main/install.sh
#     sudo bash install.sh
#
# It also works from a folder that holds all of the files (a git clone or a
# release download), and then installs those files:
#
#     sudo bash install.sh              # first install
#     sudo bash install.sh --with-hid   # first install + USB keyboard for remote buttons
#     sudo bash install.sh --update     # update to the latest release on GitHub
#
# More update options:
#
#     --release v2.3.1           install that release
#     --release                  list the releases and ask which one to install
#     --list-releases            list the releases
#     --local                    install the files next to this script, never
#                                download (after git pull, or files copied by hand)
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
# --update downloads the release into a temporary folder and runs that
# release's installer. It skips the package step, runs the tests, checks your
# existing config against the new version BEFORE replacing anything, installs
# it, and restarts the service if it's running. If the tests or the config
# check fail, the installed version is left untouched. Installing a release
# older than the installed version asks you to confirm.
set -euo pipefail

ROOT="${ROOT:-}"                 # for testing only: install into a fake root
TEST_MODE="${INSTALL_TEST:-0}"   # for testing only: skip apt, systemctl
WITH_HID=0
UPDATE=0
LOCAL=0
LIST=0
PICK=0
RELEASE=""
ALL_ARGS="$*"
while [ $# -gt 0 ]; do
    case "$1" in
        --with-hid) WITH_HID=1 ;;
        --update) UPDATE=1 ;;
        --local) LOCAL=1 ;;
        --list-releases) LIST=1 ;;
        --release)
            if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then RELEASE="$2"; shift; else PICK=1; fi ;;
        --release=*) RELEASE="${1#*=}" ;;
        -h|--help) awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done
# Set when a newer installer runs this one from a downloaded release. Read it
# once and remove it, so the tests and other programs started below do not
# inherit it.
RELEASE_TAG="${COUCHLINK_RELEASE_TAG:-}"
unset COUCHLINK_RELEASE_TAG
[ -z "$RELEASE_TAG" ] || LOCAL=1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*"; exit 1; }
run()  { if [ "$TEST_MODE" = 1 ]; then echo "[test mode] skipped: $*"; else "$@"; fi; }
version_of() { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$1" 2>/dev/null | head -1; }
have_tty() { (exec </dev/tty) 2>/dev/null; }

# Talks to GitHub with Python's standard library, so nothing extra is needed.
# COUCHLINK_API and COUCHLINK_ARCHIVE are for testing only.
release_tool() {
    python3 - "$@" <<'PYREL'
import json, os, re, sys, tarfile, tempfile, urllib.error, urllib.request

REPO = os.environ.get("COUCHLINK_REPO", "blackhat840/CouchLink")
API = os.environ.get("COUCHLINK_API", f"https://api.github.com/repos/{REPO}")
ARCHIVE = os.environ.get("COUCHLINK_ARCHIVE", f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.tar.gz")


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(2)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "couchlink-installer",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            fail(f"Not found on GitHub: {url}")
        fail(f"GitHub answered {e.code} for {url}")
    except (urllib.error.URLError, OSError) as e:
        fail(f"Could not reach GitHub ({getattr(e, 'reason', e)}). Check the Pi's network")


def version_key(v):
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[.-]", v.lstrip("vV"))]


cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "list":
    for r in json.loads(get(f"{API}/releases?per_page=30")):
        if not r.get("draft"):
            print("\t".join([r["tag_name"], (r.get("published_at") or "")[:10],
                             r.get("name") or "", "pre-release" if r.get("prerelease") else ""]))
elif cmd == "latest":
    print(json.loads(get(f"{API}/releases/latest"))["tag_name"])
elif cmd == "cmp":
    a, b = version_key(args[0]), version_key(args[1])
    print((a > b) - (a < b))
elif cmd == "fetch":
    tag, dest = args
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
        fail(f"{tag!r} is not a valid release name")
    data = get(ARCHIVE.format(tag=tag))
    with tempfile.TemporaryFile() as f:
        f.write(data)
        f.seek(0)
        with tarfile.open(fileobj=f, mode="r:gz") as tf:
            for m in tf.getmembers():
                parts = m.name.split("/")
                if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                    fail(f"Unsafe entry in the release archive: {m.name}")
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
            else:
                tf.extractall(dest)
    for name in sorted(os.listdir(dest)):
        if os.path.isfile(os.path.join(dest, name, "install.sh")):
            print(os.path.join(dest, name))
            break
    else:
        fail("The release archive has no install.sh")
PYREL
}

HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$TEST_MODE" = 1 ] || [ "$(id -u)" = 0 ] || [ "$LIST" = 1 ] || die "Run with sudo: sudo bash $0 $ALL_ARGS"

BIN="$ROOT/usr/local/bin/couchlink"
CONF_DIR="$ROOT/etc/couchlink"
CONF="$CONF_DIR/config.toml"
UNIT_DIR="$ROOT/etc/systemd/system"
OLD_VER=""
# Under a test root the logs go there too, so two installers never share one.
LOG_DIR="$ROOT/tmp"
mkdir -p "$LOG_DIR"
TEST_LOG="$LOG_DIR/couchlink-test.log"
CHECK_LOG="$LOG_DIR/couchlink-check.log"

# ---------------------------------------------------------------- releases on GitHub
if [ "$LIST" = 1 ]; then
    OUT="$(release_tool list)" || exit 1
    [ -n "$OUT" ] || die "No releases found"
    say "CouchLink releases (newest first)"
    printf '%s\n' "$OUT" | while IFS=$'\t' read -r tag date name pre; do
        printf '  %-10s %s  %s %s\n' "$tag" "$date" "$name" "$pre"
    done
    exit 0
fi

# Download a release when asked to, or when the program files are not next to
# this script (someone downloaded install.sh on its own).
HAVE_FILES=0
[ -f "$HERE/couchlink.py" ] && [ -f "$HERE/couchlink.example.toml" ] && HAVE_FILES=1
INSTALLED=0
[ -f "$BIN" ] && [ -f "$CONF" ] && INSTALLED=1
REMOTE=0
if [ "$LOCAL" = 0 ] && { [ "$UPDATE" = 1 ] || [ -n "$RELEASE" ] || [ "$PICK" = 1 ] || [ "$HAVE_FILES" = 0 ]; }; then
    REMOTE=1
fi

if [ "$REMOTE" = 1 ]; then
    if [ "$UPDATE" = 1 ] && [ "$INSTALLED" = 0 ]; then
        die "No existing install found. Run without --update to install CouchLink."
    fi
    if [ "$UPDATE" = 0 ] && [ "$INSTALLED" = 1 ]; then
        say "CouchLink is already installed, so this updates it"
        UPDATE=1
    fi
    [ "$INSTALLED" = 0 ] || OLD_VER="$(version_of "$BIN")"
    LATEST=0
    if [ "$PICK" = 1 ]; then
        have_tty || die "No terminal to choose from. Use --release <name> (see --list-releases)."
        OUT="$(release_tool list)" || exit 1
        [ -n "$OUT" ] || die "No releases found"
        mapfile -t TAGS < <(printf '%s\n' "$OUT" | cut -f1)
        say "CouchLink releases (installed: ${OLD_VER:-none})"
        i=1
        printf '%s\n' "$OUT" | while IFS=$'\t' read -r tag date name pre; do
            printf '  %2d) %-10s %s  %s %s\n' "$i" "$tag" "$date" "$name" "$pre"
            i=$((i + 1))
        done
        read -r -p "Release to install (1-${#TAGS[@]}): " n </dev/tty
        [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#TAGS[@]}" ] || die "No release chosen"
        RELEASE="${TAGS[$((n - 1))]}"
    elif [ -z "$RELEASE" ]; then
        RELEASE="$(release_tool latest)" || exit 1
        LATEST=1
    fi
    CMP=1
    [ "$INSTALLED" = 0 ] || CMP="$(release_tool cmp "$RELEASE" "${OLD_VER:-0}")"
    if [ "$CMP" = 0 ] && [ "$LATEST" = 1 ]; then
        say "CouchLink ${OLD_VER} is already the latest release ($RELEASE). Nothing to do."
        exit 0
    fi
    if [ "$CMP" = -1 ]; then
        have_tty || die "$RELEASE is older than the installed ${OLD_VER}. Run from a terminal to confirm."
        read -r -p "$RELEASE is older than the installed ${OLD_VER}. Install it anyway? [y/N] " a </dev/tty
        [ "$a" = y ] || [ "$a" = Y ] || die "Cancelled. Nothing was changed."
    fi
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    say "Downloading CouchLink $RELEASE from GitHub"
    SRC="$(release_tool fetch "$RELEASE" "$TMP")" || exit 1
    ARGS=()
    [ "$UPDATE" = 0 ] || ARGS+=(--update)
    [ "$WITH_HID" = 0 ] || ARGS+=(--with-hid)
    COUCHLINK_RELEASE_TAG="$RELEASE" bash "$SRC/install.sh" "${ARGS[@]}"
    exit 0
fi

[ -f "$HERE/couchlink.py" ] || die "couchlink.py not found next to this script. Run without --local to download it."
[ -f "$HERE/couchlink.example.toml" ] || die "couchlink.example.toml not found next to this script"
NEW_VER="$(version_of "$HERE/couchlink.py")"

if [ "$UPDATE" = 1 ]; then
    [ -f "$BIN" ] && [ -f "$CONF" ] || die "No existing install found. Run without --update first."
    OLD_VER="$(version_of "$BIN")"
    if [ "$TEST_MODE" != 1 ]; then
        command -v cec-client >/dev/null || die "cec-client is missing. Run without --update to reinstall packages."
    fi
    say "Updating CouchLink ${OLD_VER:-?} -> ${NEW_VER:-?}${RELEASE_TAG:+ (release $RELEASE_TAG)}"
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
    if ( cd "$HERE" && python3 test_couchlink.py ) > "$TEST_LOG" 2>&1; then
        grep -aE "^Ran|^OK" "$TEST_LOG"
    else
        tail -25 "$TEST_LOG"
        die "Offline tests failed, so nothing was installed. Full log: $TEST_LOG"
    fi
else
    warn "test_couchlink.py not found, skipping offline tests"
fi

# ---------------------------------------------------------------- update pre-check
MIGRATED=""
CONFIG_UPDATED=0
if [ "$UPDATE" = 1 ]; then
    say "Checking your config against the new version"
    CHECKED="$CONF"
    if grep -q -- "--migrate-config" "$HERE/couchlink.py"; then
        # Bring a copy up to date. Yours is only replaced once the new version is installed.
        MIGRATED="$LOG_DIR/couchlink-migrated.toml"
        cp "$CONF" "$MIGRATED"
        python3 "$HERE/couchlink.py" --config "$MIGRATED" --migrate-config \
            | sed "s|$MIGRATED|your config|g" \
            || die "Couldn't bring your config up to date, so nothing was changed."
        rm -f "$MIGRATED.bak"
        CHECKED="$MIGRATED"
    fi
    if python3 "$HERE/couchlink.py" --config "$CHECKED" --check-config > "$CHECK_LOG" 2>&1; then
        echo "Config OK with ${NEW_VER:-the new version}"
    else
        grep CONFIG "$CHECK_LOG" || cat "$CHECK_LOG"
        die "Your config doesn't pass with the new version, so nothing was changed. Fix it (compare with couchlink.example.toml) and run --update again."
    fi
fi

# ---------------------------------------------------------------- program
say "Installing CouchLink"
install -d "$ROOT/usr/local/bin" "$CONF_DIR" "$UNIT_DIR"
install -m 0755 "$HERE/couchlink.py" "$BIN"
echo "Installed $BIN (${NEW_VER:-unknown version})"

if [ -n "$MIGRATED" ] && ! cmp -s "$MIGRATED" "$CONF"; then
    cp "$CONF" "$CONF.bak"
    install -m 0644 "$MIGRATED" "$CONF"
    echo "Brought $CONF up to date (the previous version is in $CONF.bak)"
    CONFIG_UPDATED=1
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF_DIR/config.example.toml"
elif [ -f "$CONF" ]; then
    echo "Kept your existing $CONF"
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF_DIR/config.example.toml"
    echo "Latest example saved as $CONF_DIR/config.example.toml for comparison"
else
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF"
    echo "Created $CONF from the example"
fi
[ -z "$MIGRATED" ] || rm -f "$MIGRATED"

SERVICE_FILE="$UNIT_DIR/couchlink.service"
SERVICE_NEW="$(mktemp)"
cat > "$SERVICE_NEW" <<'UNIT'
[Unit]
Description=CouchLink (keeps a gaming PC in step with the TV over HDMI-CEC)
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
        printf '\n[all]\n# USB device (gadget) mode for the CouchLink keyboard\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CONFIG_TXT"
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
# USB keyboard gadget for CouchLink. Safe to run more than once.
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
echo "CouchLink"          > strings/0x409/manufacturer
echo "CouchLink Keyboard" > strings/0x409/product
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
Description=CouchLink USB keyboard gadget
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
    say "Updated ${OLD_VER:-?} -> ${NEW_VER:-?}. Your settings were kept."
    if [ "$CONFIG_UPDATED" = 1 ]; then
        echo "Your config was brought up to date for the new version. To see what changed:"
        echo "  diff /etc/couchlink/config.toml.bak /etc/couchlink/config.toml"
    else
        echo "Your config needed no changes for the new version."
    fi
    if [ "$REBOOT_NEEDED" = 1 ]; then
        warn "Reboot needed for the USB keyboard gadget: sudo reboot"
    fi
    exit 0
fi

# ---------------------------------------------------------------- config check
say "Checking the config"
if python3 "$BIN" --config "$CONF" --check-config > "$CHECK_LOG" 2>&1; then
    echo "Config OK"
else
    if have_tty && [ "$TEST_MODE" != 1 ]; then
        say "Basic settings"
        echo "CouchLink can ask for the PC's address and a few other settings now."
        echo "The CEC addresses come later, with the TV on."
        read -r -p "Fill in the basic settings now? [Y/n] " a </dev/tty
        if [ "$a" != n ] && [ "$a" != N ]; then
            python3 "$BIN" --config "$CONF" --setup </dev/tty || warn "Setup did not finish. Run it again with: sudo couchlink --setup"
        fi
    else
        grep CONFIG "$CHECK_LOG" || cat "$CHECK_LOG"
        echo "(Expected on a fresh install: fill in the placeholders, see below.)"
    fi
fi

# ---------------------------------------------------------------- next steps
say "Done. Next steps"
DOCS="https://github.com/blackhat840/CouchLink/blob/main/docs"
cat <<STEPS
1. Fill in the PC's settings, if you skipped it above:
     sudo couchlink --setup
2. Find your CEC addresses and add them to the config, then check it.
   Guide: $DOCS/configuration.md
     sudo systemctl stop couchlink.service 2>/dev/null; echo "scan" | cec-client -s -d 1
     sudo nano /etc/couchlink/config.toml
     sudo couchlink --check-config
3. Set up the PC so the Pi can wake it and put it to sleep.
   Guide: $DOCS/preparing-the-pc.md
4. Give root on the Pi an SSH key the PC trusts. sudo couchlink --setup
   offers to do this. By hand:
     sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519
     sudo ssh-copy-id deck@<pc-ip>
     sudo ssh deck@<pc-ip> "echo ok"
5. Try a dry run. It changes nothing. Press Ctrl+C to stop:
     sudo couchlink --dry-run
   Status page: http://$(hostname 2>/dev/null || echo '<pi>').local:8080
6. Go live:
     sudo systemctl enable --now couchlink.service
     sudo journalctl -u couchlink.service -f
7. Later, to update to the latest release, run this from the same folder:
     sudo bash install.sh --update
STEPS
if [ "$REBOOT_NEEDED" = 1 ]; then
    warn "Reboot needed for the USB keyboard gadget: sudo reboot"
fi
