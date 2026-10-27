# Status page and logs

Following what CouchLink does, on the status page or in the system log.

## Status page

Open `http://<pi>:8080` on a phone or computer on the same network. It updates
every 2 seconds.

![The CouchLink status page with the PC online on HDMI 1.2](dashboard.png)

It shows:

- The PC's name and state (online, waking up, going to sleep, asleep, off,
  restarting), and the path from the TV to the PC. See
  [Sleep, power-off and restarts](how-it-works.md#sleep-power-off-and-restarts)
  for how CouchLink tells them apart. The name comes from `pc_name`, or from the PC's
  own hostname if that is not set.
- TV power and input, and how long the TV has been on that input.
- Whether the PC is on, asleep, off or restarting, and for how long. If it was
  already like that when CouchLink started, it says "for at least".
- Wake-on-LAN and suspend attempts, the input guard, the sleep check and what
  happens after it, and how long CouchLink has been running. While the PC
  restarts, the sleep check shows "On hold".
- The wake and sleep buttons, if they are turned on. See below.
- Recent events and raw CEC traffic, each in its own tab.
- Links to the raw JSON and to this repository.

If the page loses contact with CouchLink, for example because the Pi is off,
it dims everything and says when it last heard from it. It comes back by
itself when CouchLink answers again.

The browser tab's title starts with the PC's state, so you can see it from
another tab. The page loads nothing from the internet, and it works with
screen readers: the status is read out when it changes, not on every
refresh.

`http://<pi>:8080/api` returns the same information as JSON. There is no
login, so keep the page on a trusted network.

## Wake and sleep buttons

The status page can wake the PC and put it to sleep. The buttons and the API
behind them need an API key, and are off until you generate one:

```bash
sudo couchlink --new-api-key
sudo systemctl restart couchlink
```

It prints the key once. Copy it somewhere safe, such as a password manager.
Only a hash of the key is saved in the config, so it can't be shown again.
Running the command again generates a new key, and the old one stops working.

The status page asks for the key once on each phone or computer and
remembers it there. "Forget the key on this device" removes it.

Each button shows only when it would do something:

| Button | What it does |
|---|---|
| Wake PC | Starts a [remote session](how-it-works.md#remote-sessions). The PC wakes and stays awake, and the TV is left alone. |
| Wake PC and TV | Turns the TV on, switches it to the PC and wakes the PC, like waking it with a controller. |
| End remote session | Ends the session. If the TV is on another input, the PC is suspended after `away_debounce`. |
| Put PC to sleep | Suspends the PC over SSH and ends any session. If the TV is on the PC, `on_pc_sleep` runs as if the PC went to sleep by itself. |

In a dry run, the buttons only log what they would do.

## API

The buttons use these requests. Scripts, phone shortcuts and Home Assistant
can use them too.

| Request | Does |
|---|---|
| `POST /api/wake` with `{"tv": false}` | Wake PC |
| `POST /api/wake` with `{"tv": true}` | Wake PC and TV |
| `POST /api/sleep` | Put PC to sleep |
| `POST /api/session/end` | End remote session |

Send the key in an `Authorization` header. The body is optional, and when there
is one it must be a JSON object:

```bash
curl -X POST -H "Authorization: Bearer <key>" -d '{"tv": false}' http://<pi>:8080/api/wake
```

Each answer is JSON, such as:

```json
{"ok": true, "result": "session_started", "message": "Remote session started: keeping the PC awake for 4h without the TV"}
```

| `result` | Meaning |
|---|---|
| `session_started`, `session_extended` | Wake PC started a remote session, or reset its time |
| `not_needed` | Wake PC did nothing, because the TV is on the PC |
| `waking_pc_and_tv` | Wake PC and TV is under way |
| `suspending` | The PC is being put to sleep |
| `already_asleep` | The PC is not reachable, so nothing was sent |
| `session_ended`, `no_session` | The session ended, or none was running |
| `too_soon` | HTTP 429. Only one wake, and one sleep, is accepted every 10 seconds |
| `bad_key` | HTTP 401. The key is missing or wrong |
| `locked_out` | HTTP 429. 5 wrong keys from one address lock it out for a minute |
| `actions_off` | HTTP 403. No API key has been generated yet |
| `refused` | HTTP 403. The request came from a page on another web site |

`GET /api` only reads, and needs no key. Every request is logged with the
address it came from, for example `[ACTION] Wake PC requested from 192.168.1.20`.
Wrong keys are logged as warnings.

CouchLink serves plain HTTP, so the key crosses the network unencrypted. On a
home network that is usually fine. To reach the Pi from outside the home, use a
VPN or a reverse proxy that adds HTTPS.

## Logs

Every line is `[LEVEL] [COMPONENT] message`. journald adds the time.

```bash
sudo journalctl -u couchlink.service -f                  # live
sudo journalctl -u couchlink.service -f | grep -v DEBUG  # without raw CEC traffic
sudo journalctl -u couchlink.service | grep -E "\[(INPUT|SUSPEND|WOL)\]"
```

| Component | What it logs |
|---|---|
| `INPUT` | Every input change, with the message and sender that caused it |
| `WOL` | Wake-on-LAN packets |
| `SUSPEND` | Suspend attempts and results |
| `WATCHER` | The PC becoming reachable or unreachable, and what CouchLink did about it |
| `ACTION` | Buttons and API requests, and remote sessions starting and ending |
| `RECOVER` | Automatic input switches being undone |
| `TV` | Standby messages and TV power status |
| `CEC-TX` | Every command CouchLink sends, with the reason |
| `CEC` | Raw traffic, when `raw_debug` is on |
| `KEY` | Remote buttons forwarded to the PC |
| `DRY-RUN` | What would have happened |
| `CONFIG`, `STARTUP`, `CONTROL` | Setup problems and unexpected errors |

---

Back to the [Documentation](README.md) or the [README](../README.md).
