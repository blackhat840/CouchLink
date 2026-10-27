# Status page and logs

Following what Couchlink does, on the status page or in the system log.

## Status page

Open `http://<pi>:8080` on a phone or computer on the same network. It updates
every 2 seconds.

![The Couchlink status page with the PC online on HDMI 1.2](dashboard.png)

It shows:

- The PC's name and state (online, waking up, going to sleep, asleep), and the
  path from the TV to the PC. The name comes from `pc_name`, or from the PC's
  own hostname if that is not set.
- TV power and input, whether the PC answers on the network, Wake-on-LAN and
  suspend attempts, the input guard, the sleep check and uptime.
- Recent events and raw CEC traffic, each in its own tab.
- Links to the raw JSON and to this repository.

The page loads nothing from the internet.

`http://<pi>:8080/api` returns the same information as JSON. There is no
login, so keep the page on a trusted network.

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
| `WATCHER` | The PC becoming reachable or unreachable, and what Couchlink did about it |
| `RECOVER` | Automatic input switches being undone |
| `TV` | Standby messages and TV power status |
| `CEC-TX` | Every command Couchlink sends, with the reason |
| `CEC` | Raw traffic, when `raw_debug` is on |
| `KEY` | Remote buttons forwarded to the PC |
| `DRY-RUN` | What would have happened |
| `CONFIG`, `STARTUP`, `CONTROL` | Setup problems and unexpected errors |

---

Back to the [Documentation](README.md) or the [README](../README.md).
