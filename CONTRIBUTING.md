# Contributing to CouchLink

These rules apply to everyone who changes this repository, people and AI
assistants alike.

## Writing

- Plain ASCII only. No em dashes, no ellipses and no curly quotes.
- Do not join sentences with semicolons.
- Write plainly, with no filler.
- This applies to code, comments, docs, commit messages, release notes,
  pull requests and comments.

## Attribution

- Do not credit or mention AI assistants or the tools that run them anywhere:
  not in files, commit messages, commit trailers, branch names, pull
  requests, release notes or comments. This includes co-author lines,
  "generated with" lines and session links.
- Commits use the maintainer's identity.
- Do not add personal names, account names or copyright lines to files. The
  license is in [LICENSE](LICENSE).

## Privacy

- Never commit `config.toml`.
- Never commit a real IP address or MAC address. Use the documentation
  examples, such as `192.168.1.50` and `aa:bb:cc:dd:ee:ff`.

## Tests

- `python3 test_couchlink.py` must pass before every push.
- New behavior needs new tests.

## Changes and releases

- Work on a branch and open a pull request.
- Nothing is merged into `main` without the maintainer's approval.
- Release notes are shown to the maintainer before a release is published.
