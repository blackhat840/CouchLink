# SPDX-License-Identifier: GPL-3.0-or-later
"""Running as the couchlink user: every ssh call uses CouchLink's own key and known_hosts,
and files that root writes, from sudo couchlink, go to the couchlink user."""

import glob
import os
import queue
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import HERE
from couchlink import actions, common, config, setup_pc

KEY_OPTIONS = ["-i", "/var/lib/couchlink/.ssh/id_ed25519", "-o", "UserKnownHostsFile=/var/lib/couchlink/.ssh/known_hosts"]


def has_key_options(test, command):
    test.assertEqual(command[0], "ssh")
    test.assertEqual(command[1:5], KEY_OPTIONS, command)
    test.assertIn("BatchMode=yes", command)


class SshPathTests(unittest.TestCase):
    def test_the_paths(self):
        self.assertEqual(common.SSH_KEY, "/var/lib/couchlink/.ssh/id_ed25519")
        self.assertEqual(common.KNOWN_HOSTS, "/var/lib/couchlink/.ssh/known_hosts")
        has_key_options(self, common.ssh_command("deck@192.168.1.50", "echo ok"))

    def test_suspend_and_the_name_lookup_use_the_key(self):
        cfg = SimpleNamespace(ssh_user="deck", pc_host="192.168.1.50", suspend_command="sudo -n systemctl suspend")
        events, calls = queue.Queue(), []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "gaming-pc\n", "")
        with mock.patch.object(actions.subprocess, "run", run):
            act = actions.RealActions(cfg, None, events)
            act.suspend()
            events.get(timeout=5)
            act.fetch_pc_name()
            events.get(timeout=5)
        self.assertEqual(len(calls), 2)
        for command in calls:
            has_key_options(self, command)
        self.assertEqual(calls[0][-2:], ["deck@192.168.1.50", "sudo -n systemctl suspend"])

    def test_check_ssh_uses_the_key(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "ok\n", "")
        with mock.patch.object(setup_pc.subprocess, "run", run):
            self.assertTrue(setup_pc.check_ssh("192.168.1.50", "deck")[0])
        has_key_options(self, calls[0])

    def test_setup_ssh_makes_and_copies_the_key_in_the_state_folder(self):
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state)
        key, known = os.path.join(state, ".ssh", "id_ed25519"), os.path.join(state, ".ssh", "known_hosts")
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "ok\n", "")
        with mock.patch.multiple(setup_pc, SSH_KEY=key, KNOWN_HOSTS=known, STATE_DIR=state), \
                mock.patch.object(setup_pc.subprocess, "run", run), \
                mock.patch.object(setup_pc, "ssh_command", lambda *a: ["ssh", "-i", key, *a]):
            self.assertTrue(setup_pc.setup_ssh_key("192.168.1.50", "deck", lambda line: None))
        self.assertTrue(os.path.isdir(os.path.dirname(key)))
        keygen, copy_id = calls[0], calls[1]
        self.assertEqual(keygen[0], "ssh-keygen")
        self.assertEqual(keygen[-1], key)
        self.assertEqual(copy_id[:3], ["ssh-copy-id", "-i", key + ".pub"])
        self.assertIn(f"UserKnownHostsFile={known}", copy_id)

    def test_nothing_runs_ssh_without_the_key(self):
        """Every ssh command line is built by common.ssh_command."""
        for path in glob.glob(os.path.join(HERE, "couchlink", "*.py")):
            if path.endswith("common.py"):
                continue
            with open(path) as f:
                text = f.read()
            self.assertNotIn('["ssh",', text, path)
            self.assertNotIn("/root/.ssh", text, path)


class OwnershipTests(unittest.TestCase):
    """sudo couchlink writes as root. The files keep the folder's owner, the couchlink user."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.path = os.path.join(self.folder, "config.toml")
        with open(self.path, "w") as f:
            f.write("raw_debug = true\n")
        self.chowns = []
        folder_stat = os.stat(self.folder)
        self.owner = (folder_stat.st_uid, folder_stat.st_gid)

    def as_root(self):
        return mock.patch.multiple(os, create=True, geteuid=lambda: 0,
                                   chown=lambda path, uid, gid: self.chowns.append((os.path.basename(path), uid, gid)))

    def test_a_save_as_root_keeps_the_folders_owner(self):
        with self.as_root():
            config.save_config(self.path, "raw_debug = true\n", "raw_debug = false\n")
        self.assertEqual(len(self.chowns), 2)       # the .bak and the config, before they take their place
        for _, uid, gid in self.chowns:
            self.assertEqual((uid, gid), self.owner)

    def test_the_lock_file_too(self):
        with self.as_root():
            with config.config_lock(self.path):
                pass
        self.assertIn(("config.toml.lock",) + self.owner, self.chowns)

    def test_not_as_another_user(self):
        with mock.patch.multiple(os, create=True, geteuid=lambda: 1000,
                                 chown=lambda *a: self.chowns.append(a)):
            config.save_config(self.path, "a = 1\n", "a = 2\n")
        self.assertEqual(self.chowns, [])


if __name__ == "__main__":
    unittest.main()
