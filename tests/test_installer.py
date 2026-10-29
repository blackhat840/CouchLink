# SPDX-License-Identifier: GPL-3.0-or-later
"""install.sh against a local stand-in for GitHub."""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import HERE, OLD_CONFIG, QUICK, assert_mode, assert_private, out
import couchlink
from couchlink import config


def release_archive(tag, files):
    """A .tar.gz shaped like GitHub's source archives: one top folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name if name.startswith("..") else f"CouchLink-{tag.lstrip('v')}/{name}")
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


STUB_INSTALLER = '#!/bin/bash\necho "ran $COUCHLINK_RELEASE_TAG $*" > "$ROOT/stub-ran"\n'


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the installer tests")
@unittest.skipIf(QUICK, "tests install.sh itself (skipped with --quick)")
class InstallerTests(unittest.TestCase):
    """install.sh against a local stand-in for GitHub. The downloaded installers
    are stubs that record how they were run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.src = os.path.join(self.tmp, "src")           # no test file here, so no recursion
        os.makedirs(self.src)
        for name in ("install.sh", "couchlink.example.toml"):
            shutil.copy(os.path.join(HERE, name), self.src)
        shutil.copytree(os.path.join(HERE, "couchlink"), os.path.join(self.src, "couchlink"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(self.root, "usr/local/bin"))
        os.makedirs(os.path.join(self.root, "etc/couchlink"))
        with open(os.path.join(self.root, "usr/local/bin/couchlink"), "w") as f:
            f.write('VERSION = "2.1"\n')                  # 2.6 and older: the whole program in one file
        with open(os.path.join(self.root, "etc/couchlink/config.toml"), "w") as f:
            f.write('pc_host = "192.168.1.50"\npc_mac = "aa:bb:cc:dd:ee:01"\n'
                    'pc_addresses = ["12:00"]\npi_address = "40:00"\n')
        self.releases = [
            {"tag_name": "v2.3", "name": "CouchLink 2.3", "published_at": "2026-11-01T00:00:00Z"},
            {"tag_name": "v2.2", "name": "CouchLink 2.2", "published_at": "2026-10-27T00:00:00Z"},
            {"tag_name": "v2.4", "name": "Unfinished", "draft": True},
            {"tag_name": "v2.0", "name": "CouchLink 2.0", "published_at": "2026-09-01T00:00:00Z"},
        ]
        self.latest = "v2.3"
        self.archives = {r["tag_name"]: release_archive(r["tag_name"], {"install.sh": STUB_INSTALLER})
                         for r in self.releases}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/releases/latest"):
                    body = json.dumps({"tag_name": test.latest}).encode()
                elif self.path.startswith("/api/releases"):
                    body = json.dumps(test.releases).encode()
                elif self.path.startswith("/archive/") and self.path[9:-7] in test.archives:
                    body = test.archives[self.path[9:-7]]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        # shutdown() waits up to half a second for the server to notice, so don't wait
        self.addCleanup(lambda: threading.Thread(target=self.server.shutdown, daemon=True).start())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def install(self, *args, api=None, **extra_env):
        # These tests run inside a real update too, so drop anything the
        # installer that started them may have set.
        env = {k: v for k, v in os.environ.items()
               if "proxy" not in k.lower() and not k.startswith("COUCHLINK_")}
        env.update(ROOT=self.root, INSTALL_TEST="1", COUCHLINK_API=api or f"{self.base}/api",
                   COUCHLINK_ARCHIVE=f"{self.base}/archive/{{tag}}.tar.gz", **extra_env)
        r = subprocess.run(["bash", os.path.join(self.src, "install.sh"), *args], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=120, start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def stub_ran(self):
        path = os.path.join(self.root, "stub-ran")
        return open(path).read().strip() if os.path.exists(path) else None

    def test_update_installs_latest_release(self):
        code, out = self.install("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.3 --update")

    def test_update_passes_with_hid_on(self):
        code, out = self.install("--update", "--with-hid")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.3 --update --with-hid")

    def test_setup_flags_use_the_installed_copy_and_need_a_terminal(self):
        for flag in ("--setup", "--setup-cec"):
            code, out = self.install(flag)
            self.assertNotEqual(code, 0)
            self.assertIn(f"{flag} asks questions, so run it from a terminal", out)
            self.assertIsNone(self.stub_ran())                     # nothing downloaded or installed

    def test_setup_flags_need_an_install(self):
        os.remove(os.path.join(self.root, "usr/local/bin/couchlink"))
        code, out = self.install("--setup-cec")
        self.assertNotEqual(code, 0)
        self.assertIn("isn't installed yet", out)

    def test_already_on_latest_does_nothing(self):
        self.latest = "v2.1"
        code, out = self.install("--update")
        self.assertEqual(code, 0, out)
        self.assertIn("already the latest release", out)
        self.assertIsNone(self.stub_ran())

    def test_chosen_release(self):
        code, out = self.install("--update", "--release", "v2.2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.2 --update")

    def test_older_release_needs_confirmation(self):
        code, out = self.install("--release=v2.0")
        self.assertNotEqual(code, 0)
        self.assertIn("older than the installed 2.1", out)
        self.assertIsNone(self.stub_ran())

    def test_picking_needs_a_terminal(self):
        code, out = self.install("--update", "--release")
        self.assertNotEqual(code, 0)
        self.assertIn("No terminal", out)

    def test_list_releases_skips_drafts(self):
        code, out = self.install("--list-releases")
        self.assertEqual(code, 0, out)
        self.assertIn("v2.3", out)
        self.assertIn("v2.0", out)
        self.assertNotIn("v2.4", out)

    def test_unsafe_archive_rejected(self):
        self.archives["v2.3"] = release_archive("v2.3", {"install.sh": STUB_INSTALLER,
                                                         "../../evil.sh": "boom"})
        code, out = self.install("--update")
        self.assertNotEqual(code, 0)
        self.assertIn("Unsafe entry", out)
        self.assertIsNone(self.stub_ran())

    def test_github_unreachable(self):
        code, out = self.install("--update", api="http://127.0.0.1:9/api")
        self.assertNotEqual(code, 0)
        self.assertIn("Could not reach GitHub", out)

    def lone_script(self):
        """Only install.sh, as if it was downloaded on its own."""
        shutil.rmtree(os.path.join(self.src, "couchlink"))
        os.remove(os.path.join(self.src, "couchlink.example.toml"))

    def not_installed(self):
        os.remove(os.path.join(self.root, "usr/local/bin/couchlink"))
        os.remove(os.path.join(self.root, "etc/couchlink/config.toml"))

    def test_lone_script_installs_latest_release(self):
        self.lone_script()
        self.not_installed()
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.3")         # a first install, not --update

    def test_lone_script_installs_chosen_release(self):
        self.lone_script()
        self.not_installed()
        code, out = self.install("--release", "v2.2", "--with-hid")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.2 --with-hid")

    def test_lone_script_updates_an_existing_install(self):
        self.lone_script()
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn("already installed, so this updates it", out)
        self.assertEqual(self.stub_ran(), "ran v2.3 --update")

    def test_update_without_an_install_explains(self):
        self.not_installed()
        code, out = self.install("--update")
        self.assertNotEqual(code, 0)
        self.assertIn("Run without --update to install CouchLink", out)
        self.assertIsNone(self.stub_ran())

    def test_local_without_the_files_explains(self):
        self.lone_script()
        code, out = self.install("--local")
        self.assertNotEqual(code, 0)
        self.assertIn("Run without --local to download it", out)
        self.assertIsNone(self.stub_ran())

    def test_update_moves_the_old_api_key(self):
        conf = os.path.join(self.root, "etc/couchlink/config.toml")
        old = "sha256:" + "a" * 64
        with open(conf, "a") as f:
            f.write(f'api_key_hash = "{old}"\n')
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        self.assertIn("Moved api_key_hash to [api_keys] as the key named default", out)
        self.assertIn("Moved the API key default to secrets.toml", out)
        settings, problems = config.load_config(conf)
        self.assertEqual((problems, settings["api_keys"]), ([], {"default": old}))
        secrets = config.secrets_path(conf)
        with open(conf) as f:
            self.assertNotIn(old, f.read())
        with open(secrets) as f:
            self.assertIn(old, f.read())
        assert_mode(self, conf, 0o644)
        assert_private(self, secrets)
        code, out = self.install("--update", "--local")         # a second update has nothing to move
        self.assertEqual(code, 0, out)
        self.assertNotIn("secrets.toml", out.split("Checking your config")[-1].split("Installing")[0])

    def test_update_runs_couchlink_as_its_own_user(self):
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        conf_dir = os.path.join(self.root, "etc/couchlink")
        state = os.path.join(self.root, "var/lib/couchlink")
        self.assertIn("skipped: useradd --system --user-group --home-dir /var/lib/couchlink --no-create-home "
                      "--shell /usr/sbin/nologin couchlink", out)
        self.assertIn("skipped: usermod -aG video couchlink", out)
        owned = next(line for line in out.splitlines() if "skipped: chown couchlink:couchlink" in line)
        for name in ("", "/config.toml", "/config.toml.bak", "/secrets.toml"):
            self.assertIn(conf_dir + name + " ", owned + " ")
        self.assertIn(f"skipped: chown -R couchlink:couchlink {state}", out)
        assert_mode(self, conf_dir, 0o755)
        assert_mode(self, os.path.join(conf_dir, "config.toml"), 0o644)
        assert_private(self, os.path.join(conf_dir, "secrets.toml"))
        for folder in (state, os.path.join(state, ".ssh")):
            self.assertTrue(os.path.isdir(folder), folder)
            assert_mode(self, folder, 0o700)
        with open(os.path.join(self.root, "etc/systemd/system/couchlink.service")) as f:
            unit = f.read()
        for line in ("User=couchlink", "Group=couchlink", "SupplementaryGroups=video", "NoNewPrivileges=yes",
                     "ProtectSystem=strict", "ReadWritePaths=/etc/couchlink", "StateDirectory=couchlink",
                     "ProtectHome=yes", "PrivateTmp=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
                     "ProtectControlGroups=yes", "AmbientCapabilities=CAP_NET_RAW",
                     "CapabilityBoundingSet=CAP_NET_RAW",
                     "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK"):
            self.assertIn(line + "\n", unit)
        # These would block /dev/cec0 or the 32-bit Python on a 64-bit kernel
        for risky in ("DeviceAllow", "ProtectClock", "PrivateDevices", "SystemCallArchitectures"):
            self.assertNotIn(risky, unit)
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/udev/rules.d/70-couchlink-hidg.rules")))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is needed")
    def test_update_copies_roots_ssh_key_and_the_pcs_known_hosts_entry(self):
        old = os.path.join(self.root, "root/.ssh")
        os.makedirs(old)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(old, "id_ed25519")],
                       check=True)
        with open(os.path.join(old, "id_ed25519.pub")) as f:
            host_key = " ".join(f.read().split()[:2])          # any valid key works as the PC's host key
        with open(os.path.join(old, "known_hosts"), "w") as f:
            f.write(f"192.168.1.50 {host_key}\n192.168.1.99 {host_key}\n")
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        new = os.path.join(self.root, "var/lib/couchlink/.ssh")
        for name in ("id_ed25519", "id_ed25519.pub"):
            with open(os.path.join(old, name)) as a, open(os.path.join(new, name)) as b:
                self.assertEqual(a.read(), b.read())
        assert_private(self, os.path.join(new, "id_ed25519"))
        self.assertTrue(os.path.exists(os.path.join(old, "id_ed25519")))          # root's key stays
        with open(os.path.join(new, "known_hosts")) as f:
            known = f.read()
        self.assertIn("192.168.1.50 ", known)
        self.assertNotIn("192.168.1.99", known)                                      # only the PC's entry
        self.assertIn("Copied root's SSH key to /var/lib/couchlink/.ssh", out)
        self.assertIn("Copied the PC's entry (192.168.1.50) from root's known_hosts", out)
        code, out = self.install("--update", "--local")                            # already there
        self.assertEqual(code, 0, out)
        self.assertNotIn("Copied root's SSH key", out)
        with open(os.path.join(new, "known_hosts")) as f:
            self.assertEqual(f.read(), known)

    def test_update_gives_an_existing_keyboard_to_couchlink(self):
        units = os.path.join(self.root, "etc/systemd/system")
        os.makedirs(units, exist_ok=True)
        with open(os.path.join(units, "couchlink-hid.service"), "w") as f:
            f.write("[Service]\n")                     # set up with --with-hid by an older version
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.root, "etc/udev/rules.d/70-couchlink-hidg.rules")) as f:
            self.assertIn('KERNEL=="hidg[0-9]*", GROUP="couchlink", MODE="0660"', f.read())
        self.assertIn("Installed a udev rule that gives the couchlink user the USB keyboard", out)
        code, out = self.install("--update", "--local")
        self.assertNotIn("Installed a udev rule", out)

    def test_update_brings_an_old_config_up_to_date(self):
        conf = os.path.join(self.root, "etc/couchlink/config.toml")
        with open(conf, "w") as f:
            f.write(OLD_CONFIG)
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        self.assertIn("Renamed pc_adresses to pc_addresses", out)
        self.assertIn("Brought", out)
        self.assertIn("Your config was brought up to date for the new version", out)
        self.assertNotIn("config.example.toml\n", out.split("Updated")[-1])
        with open(conf + ".bak") as f:
            self.assertEqual(f.read(), OLD_CONFIG)
        assert_mode(self, conf, 0o644)                  # no hashes, so anyone can read it
        assert_private(self, conf + ".bak")
        with open(conf) as f:
            self.assertIn("watch_pc_power = true", f.read())

    def test_update_says_when_the_config_needed_no_changes(self):
        conf = os.path.join(self.root, "etc/couchlink/config.toml")
        shutil.copy(os.path.join(HERE, "couchlink.example.toml"), conf)
        for key, value in (("pc_host", "192.168.1.50"), ("pc_mac", "aa:bb:cc:dd:ee:01"),
                           ("pi_address", "40:00")):
            with open(conf) as f:
                text = config.set_config_value(f.read(), key, value)
            with open(conf, "w") as f:
                f.write(text.replace("pc_addresses = []", 'pc_addresses = ["12:00"]'))
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        self.assertIn("Your config needed no changes for the new version", out)
        self.assertFalse(os.path.exists(conf + ".bak"))

    def test_update_warns_when_the_tag_and_version_differ(self):
        version = couchlink.VERSION
        code, out = self.install("--update", "--local", COUCHLINK_RELEASE_TAG=f"v{version}")
        self.assertNotIn("calls itself version", out)
        code, out = self.install("--update", "--local", COUCHLINK_RELEASE_TAG="v9.9")
        self.assertIn(f"Release v9.9 calls itself version {version}", out)

    def test_failed_update_leaves_the_config_alone(self):
        conf = os.path.join(self.root, "etc/couchlink/config.toml")
        with open(conf, "w") as f:
            f.write('pc_host = "192.168.1.50"\nold_thing = 1\n')     # pc_mac and more missing
        code, out = self.install("--update", "--local")
        self.assertNotEqual(code, 0)
        self.assertIn("nothing was changed", out)
        with open(conf) as f:
            self.assertEqual(f.read(), 'pc_host = "192.168.1.50"\nold_thing = 1\n')

    def test_release_marker_not_passed_on_to_the_tests(self):
        probe = os.path.join(self.root, "probe")
        with open(os.path.join(self.src, "test_couchlink.py"), "w") as f:
            f.write("import os\n"
                    f"open({probe!r}, 'w').write(os.environ.get('COUCHLINK_RELEASE_TAG', 'unset'))\n"
                    "print('Ran 1 test')\nprint('OK')\n")
        code, out = self.install("--update", "--test", COUCHLINK_RELEASE_TAG="v9.9")
        self.assertEqual(code, 0, out)
        self.assertIn("(release v9.9)", out)
        self.assertIsNone(self.stub_ran())               # installed locally, nothing downloaded
        with open(probe) as f:
            self.assertEqual(f.read(), "unset")

    def test_tests_run_only_with_test(self):
        with open(os.path.join(self.src, "test_couchlink.py"), "w") as f:
            f.write("import sys\nprint('Ran 1 test with', sys.argv[1:])\nprint('OK')\n")
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Running offline tests", out)
        code, out = self.install("--update", "--local", "--test")
        self.assertEqual(code, 0, out)
        self.assertIn("Ran 1 test with ['--quick']", out)

    def test_test_flag_is_passed_to_a_downloaded_release(self):
        code, out = self.install("--update", "--test")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stub_ran(), "ran v2.3 --update --test")

    def test_local_update_installs_files_next_to_the_script(self):
        code, out = self.install("--update", "--local")
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.stub_ran())
        with open(os.path.join(self.root, "usr/local/lib/couchlink/couchlink/__init__.py")) as f:
            self.assertIn(f'VERSION = "{couchlink.VERSION}"', f.read())
        # The old one-file program is replaced by the command that runs the package
        launcher = os.path.join(self.root, "usr/local/bin/couchlink")
        with open(launcher) as f:
            self.assertIn("from couchlink.cli import main", f.read())
        r = subprocess.run([sys.executable, launcher, "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--setup-cec", r.stdout)
        # Compiled once at install, so the first start on a Pi Zero is quicker
        self.assertTrue(os.path.isdir(os.path.join(self.root, "usr/local/lib/couchlink/couchlink/__pycache__")))
        # Logs stay inside the test root, so they never clash with a real install's.
        self.assertTrue(os.path.exists(os.path.join(self.root, "tmp/couchlink-check.log")))
