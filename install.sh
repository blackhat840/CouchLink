#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# CouchLink installer and updater for Raspberry Pi OS.
#
# install.sh works on its own. If the couchlink folder is not next to it, it downloads
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
# Setting up an installed CouchLink:
#
#     --setup                    answer questions to fill in the config, all but
#                                the CEC addresses
#     --setup-cec                find the CEC addresses with the TV on (asks
#                                whether you have a soundbar or AV receiver)
#     --setup-ssh                give the Pi an SSH key the PC trusts
#
# All of these can also be done on the settings page, http://<pi>:8080/admin,
# except the SSH key.
#
# More update options:
#
#     --release v2.3.1           install that release
#     --release                  list the releases and ask which one to install
#     --list-releases            list the releases
#     --local                    install the files next to this script, never
#                                download (after git pull, or files copied by hand)
#     --test                     run the offline tests before installing. They
#                                check the code, not your Pi, and each release
#                                already passed them, so they are off by default
#
# First install:
#   1. Installs cec-utils and ping
#   2. With --test, runs the offline tests, without the installer's own tests
#   3. Installs the program in /usr/local/lib/couchlink, with the couchlink
#      command in /usr/local/bin
#   4. Creates /etc/couchlink/config.toml from the example (never overwrites yours)
#   5. Installs couchlink.service and starts it. Until the settings are complete,
#      it runs only the settings page, where the setup is finished in a browser
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
RUN_TESTS=0
LIST=0
PICK=0
SETUP=""
RELEASE=""
ALL_ARGS="$*"
while [ $# -gt 0 ]; do
    case "$1" in
        --with-hid) WITH_HID=1 ;;
        --update) UPDATE=1 ;;
        --local) LOCAL=1 ;;
        --test) RUN_TESTS=1 ;;
        --list-releases) LIST=1 ;;
        --setup) SETUP=setup ;;
        --setup-cec) SETUP=setup-cec ;;
        --setup-ssh) SETUP=setup-ssh ;;
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
# The installed version. Up to 2.6, all of CouchLink was the one file /usr/local/bin/couchlink.
installed_version() {
    local v
    v="$(version_of "$LIB/couchlink/__init__.py")"
    [ -n "$v" ] || v="$(version_of "$BIN")"
    printf '%s' "$v"
}
# Runs the couchlink package next to this script, for the checks before it is installed.
new_couchlink() { PYTHONPATH="$HERE" python3 -m couchlink "$@"; }
# 0600 for a config that still holds a password or key hash, even in a comment, else 0644
config_mode() {
    if PYTHONPATH="$HERE" python3 -c 'import sys; from couchlink.config import has_secrets; sys.exit(not has_secrets(open(sys.argv[1]).read()))' "$1"; then
        echo 0600
    else
        echo 0644
    fi
}
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
LIB="$ROOT/usr/local/lib/couchlink"
CONF_DIR="$ROOT/etc/couchlink"
CONF="$CONF_DIR/config.toml"
SECRETS="$CONF_DIR/secrets.toml"
STATE_DIR="$ROOT/var/lib/couchlink"
UNIT_DIR="$ROOT/etc/systemd/system"
OLD_VER=""
# Under a test root the logs go there too, so two installers never share one.
LOG_DIR="$ROOT/tmp"
mkdir -p "$LOG_DIR"
TEST_LOG="$LOG_DIR/couchlink-test.log"
CHECK_LOG="$LOG_DIR/couchlink-check.log"

# ---------------------------------------------------------------- setup of an installed CouchLink
if [ -n "$SETUP" ]; then
    [ -f "$BIN" ] && [ -f "$CONF" ] || die "CouchLink isn't installed yet. Run: sudo bash install.sh"
    have_tty || die "--$SETUP asks questions, so run it from a terminal"
    python3 "$BIN" --config "$CONF" "--$SETUP" </dev/tty
    exit $?
fi

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
[ -f "$HERE/couchlink/__init__.py" ] && [ -f "$HERE/couchlink.example.toml" ] && HAVE_FILES=1
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
    [ "$INSTALLED" = 0 ] || OLD_VER="$(installed_version)"
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
    [ "$RUN_TESTS" = 0 ] || ARGS+=(--test)
    COUCHLINK_RELEASE_TAG="$RELEASE" bash "$SRC/install.sh" "${ARGS[@]}"
    exit 0
fi

[ -f "$HERE/couchlink/__init__.py" ] || die "The couchlink folder is not next to this script. Run without --local to download it."
[ -f "$HERE/couchlink.example.toml" ] || die "couchlink.example.toml not found next to this script"
NEW_VER="$(version_of "$HERE/couchlink/__init__.py")"

if [ "$UPDATE" = 1 ]; then
    [ -f "$BIN" ] && [ -f "$CONF" ] || die "No existing install found. Run without --update first."
    OLD_VER="$(installed_version)"
    if [ "$TEST_MODE" != 1 ]; then
        command -v cec-client >/dev/null || die "cec-client is missing. Run without --update to reinstall packages."
    fi
    say "Updating CouchLink ${OLD_VER:-?} -> ${NEW_VER:-?}${RELEASE_TAG:+ (release $RELEASE_TAG)}"
fi
if [ -n "$RELEASE_TAG" ] && [ "${RELEASE_TAG#[vV]}" != "$NEW_VER" ]; then
    warn "Release $RELEASE_TAG calls itself version ${NEW_VER:-?}. It was tagged before its version number was changed."
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
if [ "$RUN_TESTS" = 1 ] && [ -f "$HERE/test_couchlink.py" ]; then
    say "Running offline tests"
    if ( cd "$HERE" && python3 test_couchlink.py --quick ) > "$TEST_LOG" 2>&1; then
        grep -aE "^Ran|^OK" "$TEST_LOG"
    else
        tail -25 "$TEST_LOG"
        die "Offline tests failed, so nothing was installed. Full log: $TEST_LOG"
    fi
elif [ "$RUN_TESTS" = 1 ]; then
    warn "test_couchlink.py not found, skipping offline tests"
fi

# ---------------------------------------------------------------- update pre-check
MIGRATED=""
CONFIG_UPDATED=0
if [ "$UPDATE" = 1 ]; then
    say "Checking your config against the new version"
    # Bring copies up to date. Yours are only replaced once the new version is installed.
    # This also moves the password hash and API keys from config.toml to secrets.toml.
    MIG_DIR="$(mktemp -d)"
    MIGRATED="$MIG_DIR/config.toml"
    install -m 0600 "$CONF" "$MIGRATED"
    [ ! -f "$SECRETS" ] || install -m 0600 "$SECRETS" "$MIG_DIR/secrets.toml"
    new_couchlink --config "$MIGRATED" --migrate-config \
        | sed "s|$MIG_DIR|$CONF_DIR|g" \
        || die "Couldn't bring your config up to date, so nothing was changed."
    rm -f "$MIGRATED.bak"
    if new_couchlink --config "$MIGRATED" --check-config > "$CHECK_LOG" 2>&1; then
        echo "Config OK with ${NEW_VER:-the new version}"
    else
        grep CONFIG "$CHECK_LOG" || cat "$CHECK_LOG"
        die "Your config doesn't pass with the new version, so nothing was changed. Fix it (compare with couchlink.example.toml) and run --update again."
    fi
fi

# ---------------------------------------------------------------- program
say "Installing CouchLink"
install -d "$ROOT/usr/local/bin" "$CONF_DIR" "$UNIT_DIR"
rm -rf "$LIB"
install -d "$LIB"
cp -R "$HERE/couchlink" "$LIB/"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
# Compile once now, so the first start on a Pi Zero doesn't have to.
python3 -m compileall -q "$LIB" >/dev/null || true
cat > "$BIN.new" <<LAUNCHER
#!/usr/bin/python3
# The couchlink command. The program is in ${LIB#$ROOT}.
import sys
sys.path.insert(0, "$LIB")
from couchlink.cli import main
main()
LAUNCHER
chmod 0755 "$BIN.new"
mv "$BIN.new" "$BIN"
echo "Installed $LIB (${NEW_VER:-unknown version}) and the couchlink command"

# secrets.toml holds the settings page's password hash and the API key hashes, so only its
# owner can read it. config.toml holds no secrets, so anyone can read it, unless it still has hashes
# from before they moved. The .bak may be from before, so only its owner can read it.
if [ -n "$MIGRATED" ] && [ -f "$MIG_DIR/secrets.toml" ] && ! cmp -s "$MIG_DIR/secrets.toml" "$SECRETS"; then
    install -m 0600 "$MIG_DIR/secrets.toml" "$SECRETS"
fi
if [ -n "$MIGRATED" ] && ! cmp -s "$MIGRATED" "$CONF"; then
    install -m 0600 "$CONF" "$CONF.bak"
    install -m "$(config_mode "$MIGRATED")" "$MIGRATED" "$CONF"
    echo "Brought $CONF up to date (the previous version is in $CONF.bak)"
    CONFIG_UPDATED=1
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF_DIR/config.example.toml"
elif [ -f "$CONF" ]; then
    chmod "$(config_mode "$CONF")" "$CONF"
    [ ! -f "$CONF.bak" ] || chmod 0600 "$CONF.bak"
    echo "Kept your existing $CONF"
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF_DIR/config.example.toml"
    echo "Latest example saved as $CONF_DIR/config.example.toml for comparison"
else
    install -m 0644 "$HERE/couchlink.example.toml" "$CONF"
    echo "Created $CONF from the example"
fi
if [ ! -f "$SECRETS" ]; then
    PYTHONPATH="$HERE" python3 -c 'import sys; from couchlink.config import SECRETS_HEADER; sys.stdout.write(SECRETS_HEADER)' \
        > "$SECRETS.new"
    chmod 0600 "$SECRETS.new"
    mv "$SECRETS.new" "$SECRETS"
    echo "Created $SECRETS for the settings page's password and the API keys, readable by its owner only"
fi
chmod 0600 "$SECRETS"
[ -z "$MIGRATED" ] || rm -rf "$MIG_DIR"

# ---------------------------------------------------------------- the couchlink user
# CouchLink runs as its own system user, not root, so a bug in its web pages can't reach the
# rest of the Pi. The video group owns the HDMI-CEC device, /dev/cec0.
say "Setting up the couchlink user"
if [ "$TEST_MODE" != 1 ] && getent passwd couchlink >/dev/null; then
    echo "The couchlink user is already there"
else
    run useradd --system --user-group --home-dir /var/lib/couchlink --no-create-home \
        --shell /usr/sbin/nologin couchlink
    echo "Created the couchlink system user, with no login shell"
fi
run usermod -aG video couchlink
install -d -m 0700 "$STATE_DIR" "$STATE_DIR/.ssh"

# Older versions ran as root, with root's SSH key. Copy it and the PC's known_hosts entry, so
# the PC doesn't have to be told about a new key. Root's own key is left where it is.
OLD_KEY="$ROOT/root/.ssh/id_ed25519"
NEW_KEY="$STATE_DIR/.ssh/id_ed25519"
if [ -f "$OLD_KEY" ] && [ ! -f "$NEW_KEY" ]; then
    install -m 0600 "$OLD_KEY" "$NEW_KEY"
    [ ! -f "$OLD_KEY.pub" ] || install -m 0644 "$OLD_KEY.pub" "$NEW_KEY.pub"
    echo "Copied root's SSH key to /var/lib/couchlink/.ssh. Root's key was left in place"
    PC_HOST="$(PYTHONPATH="$HERE" python3 -c 'import sys; from couchlink.config import load_config; print((load_config(sys.argv[1])[0] or {}).get("pc_host") or "")' "$CONF" 2>/dev/null || true)"
    if [ -n "$PC_HOST" ] && [ -f "$ROOT/root/.ssh/known_hosts" ] && command -v ssh-keygen >/dev/null; then
        ssh-keygen -F "$PC_HOST" -f "$ROOT/root/.ssh/known_hosts" | grep -v '^#' >> "$STATE_DIR/.ssh/known_hosts" || true
        if [ -s "$STATE_DIR/.ssh/known_hosts" ]; then
            chmod 0644 "$STATE_DIR/.ssh/known_hosts"
            echo "Copied the PC's entry ($PC_HOST) from root's known_hosts"
        fi
    fi
fi

# The service owns its files. Anyone can read config.toml, and secrets.toml stays readable by
# its owner only. Files that sudo couchlink writes later keep this owner.
chmod 0755 "$CONF_DIR"
OWNED="$CONF_DIR"
for f in "$CONF" "$CONF.bak" "$CONF.lock" "$SECRETS" "$CONF_DIR/setup-code"; do
    [ ! -e "$f" ] || OWNED="$OWNED $f"
done
# shellcheck disable=SC2086
run chown couchlink:couchlink $OWNED
run chown -R couchlink:couchlink "$STATE_DIR"
echo "/etc/couchlink and /var/lib/couchlink now belong to the couchlink user"

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

# Run as the couchlink user. video owns the HDMI-CEC device. The couchlink group owns the
# USB keyboard device, through a udev rule.
User=couchlink
Group=couchlink
SupplementaryGroups=video
StateDirectory=couchlink
StateDirectoryMode=0700
UMask=0077

# ping on Bookworm needs CAP_NET_RAW, and NoNewPrivileges keeps it from getting it from the
# ping binary, so the service holds it. It's the only capability it has.
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
NoNewPrivileges=yes

# Read-only everywhere except its own config and state
ProtectSystem=strict
ReadWritePaths=/etc/couchlink
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectHostname=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes

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

# The keyboard device, /dev/hidg0, belongs to root. A udev rule gives it to the couchlink group.
# This also covers installs that set up the keyboard before CouchLink ran as its own user.
if [ "$WITH_HID" = 1 ] || [ -f "$UNIT_DIR/couchlink-hid.service" ]; then
    install -d "$ROOT/etc/udev/rules.d"
    HID_RULE="$ROOT/etc/udev/rules.d/70-couchlink-hidg.rules"
    printf '%s\n' "# Lets the couchlink service type on the USB keyboard gadget" \
        'KERNEL=="hidg[0-9]*", GROUP="couchlink", MODE="0660"' > "$HID_RULE.new"
    if cmp -s "$HID_RULE.new" "$HID_RULE"; then
        rm -f "$HID_RULE.new"
    else
        mv "$HID_RULE.new" "$HID_RULE"
        echo "Installed a udev rule that gives the couchlink user the USB keyboard"
        run udevadm control --reload
        if [ -e "$ROOT/dev/hidg0" ]; then
            run chgrp couchlink /dev/hidg0
            run chmod 0660 /dev/hidg0
        fi
    fi
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

# ---------------------------------------------------------------- settings
DOCS="https://github.com/blackhat840/CouchLink/blob/main/docs"
say "Checking the config"
if python3 "$BIN" --config "$CONF" --check-config > "$CHECK_LOG" 2>&1; then
    echo "Config OK"
elif have_tty && [ "$TEST_MODE" != 1 ]; then
    echo "CouchLink still needs a few settings: the PC's address and, with a TV, the HDMI inputs."
    echo "You can fill them in on the settings page in a browser, or answer questions here."
    read -r -p "Set up in the browser? [Y/n] " a </dev/tty
    if [ "$a" = n ] || [ "$a" = N ]; then
        python3 "$BIN" --config "$CONF" --setup </dev/tty || warn "Setup did not finish. Run it again with: sudo bash install.sh --setup"
        if ! python3 "$BIN" --config "$CONF" --check-config > "$CHECK_LOG" 2>&1; then
            say "CEC addresses"
            echo "Next, CouchLink finds the HDMI addresses of the PC, the Pi and any soundbar or"
            echo "AV receiver. The TV must be on, with the PC plugged in and turned on."
            read -r -p "Find them now? [Y/n] " a </dev/tty
            if [ "$a" != n ] && [ "$a" != N ]; then
                python3 "$BIN" --config "$CONF" --setup-cec </dev/tty || warn "Not finished. Run it again with: sudo bash install.sh --setup-cec"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------- start it
# Until the settings are complete, the service runs only the settings page, so the
# setup can be finished in a browser.
say "Starting CouchLink"
run systemctl enable --now couchlink.service
PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PI_NAME="$(hostname 2>/dev/null || echo pi)"
PAGE="http://${PI_IP:-$PI_NAME.local}:8080"
CODE=""
# --setup-code only prints a code if the settings page has no password yet
CODE="$(python3 "$BIN" --config "$CONF" --setup-code 2>/dev/null | sed -n 's/^Setup code: //p')"

say "Done. Next steps"
if [ -n "$CODE" ]; then
    cat <<SETUP
1. Open the settings page in a browser on the same network:
     $PAGE/admin
   Enter this setup code, then choose a password:
     $CODE
   Then fill in the PC's settings and, with a TV, find the HDMI inputs there.
   To see the code again: sudo couchlink --setup-code
SETUP
else
    cat <<SETUP
1. The settings page is at $PAGE/admin
SETUP
fi
cat <<STEPS
2. Set up the PC so the Pi can wake it and put it to sleep.
   Guide: $DOCS/preparing-the-pc.md
3. Give the Pi an SSH key the PC trusts, so it can put the PC to sleep:
     sudo bash install.sh --setup-ssh
   The settings page has a button to check that it works.
4. Watch it work on the status page, $PAGE, or in the log:
     sudo journalctl -u couchlink.service -f
5. Later, to update to the latest release, run this from the same folder:
     sudo bash install.sh --update
STEPS
if [ "$REBOOT_NEEDED" = 1 ]; then
    warn "Reboot needed for the USB keyboard gadget: sudo reboot"
fi
