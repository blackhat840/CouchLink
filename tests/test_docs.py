# SPDX-License-Identifier: GPL-3.0-or-later
"""The README and docs: spelling and links."""

import os
import unittest

from helpers import HERE


class DocsTests(unittest.TestCase):
    def test_name_is_spelled_couchlink_with_a_capital_l(self):
        wrong = "Couch" + "link"
        names = ["README.md", "CONTRIBUTING.md", "install.sh", "couchlink.example.toml"]
        for folder in ("couchlink", os.path.join("couchlink", "static"), "tests"):
            names += [os.path.join(folder, n) for n in os.listdir(os.path.join(HERE, folder))
                      if n.endswith((".py", ".html", ".css", ".js"))]
        docs = os.path.join(HERE, "docs")
        if os.path.isdir(docs):
            names += [os.path.join("docs", n) for n in os.listdir(docs) if n.endswith(".md")]
        for name in names:
            path = os.path.join(HERE, name)
            if os.path.exists(path):
                with open(path) as f:
                    self.assertNotIn(wrong, f.read(), f"{name} spells it {wrong}, use CouchLink")

    def test_links_between_pages_resolve(self):
        """Every relative link in the README and docs/ points to a file that
        exists, and every #anchor to a heading in it."""
        import re

        def anchors(path):
            with open(path) as f:
                heads = re.findall(r"^#+ (.+)$", f.read(), re.M)
            return {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in heads}

        pages = [os.path.join(HERE, "README.md")]
        docs = os.path.join(HERE, "docs")
        if os.path.isdir(docs):
            pages += [os.path.join(docs, n) for n in sorted(os.listdir(docs)) if n.endswith(".md")]
        for page in pages:
            with open(page) as f:
                text = re.sub(r"```.*?```", "", f.read(), flags=re.S)
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"[a-z]+:", target):
                    continue
                path, _, anchor = target.partition("#")
                full = os.path.normpath(os.path.join(os.path.dirname(page), path)) if path else page
                self.assertTrue(os.path.exists(full), f"{page}: broken link {target}")
                if anchor:
                    self.assertIn(anchor, anchors(full), f"{page}: no heading for {target}")
