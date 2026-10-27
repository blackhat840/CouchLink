(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var tab = "log";
  var last = null;
  var lastOk = null;      // when the page last heard from CouchLink
  var failures = 0;
  var drawn = "";         // what the log box shows, so it is only redrawn when that changes

  function dur(s) {
    if (s === null || s === undefined) return "-";
    s = Math.max(0, Math.floor(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    var days = Math.floor(h / 24);
    if (days) return days + "d " + (h % 24) + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + x + "s";
    return x + "s";
  }
  function lastSent(s) { return (s === null || s === undefined) ? "None sent yet" : "Last sent " + dur(s) + " ago"; }
  function every(s) {
    if (!s) return "Checked at startup";
    if (s % 60 === 0) return s === 60 ? "Checked every minute" : "Checked every " + s / 60 + " minutes";
    return "Checked every " + s + "s";
  }
  // Only touch text that changed, so screen readers don't repeat the page every 2 seconds.
  function put(id, text) {
    var el = $(id);
    if (el.textContent !== text) el.textContent = text;
  }
  function set(id, text, cls) {
    put(id, text);
    var el = $(id);
    if (cls !== undefined && el.className !== "value " + cls) el.className = "value " + cls;
  }
  function accent(tileId, color) { $(tileId).style.setProperty("--tile-accent", color || "transparent"); }

  function view(d) {
    var want = d.pc_should_be, up = d.pc_reachable, power = d.pc_power;
    var tvOff = d.tv_power === "standby" || d.tv_input_address === "standby";
    var waiting = want === "awake" ? "The TV is still on the PC's input"
                : tvOff ? "The TV is off" : "Waiting for the TV to switch to it";
    if (want === "awake" && up) return ["online", "Online", "Ready to play"];
    var session = d.remote_session_seconds;
    if (session !== null && session !== undefined && want !== "awake") {
      if (up) return ["online", "Remote session", "Ready to stream. Started " + dur(session) + " ago"];
      if (power === "restarting") return ["waking", "Restarting", "Remote session. Waiting for the PC to come back"];
      return ["waking", "Waking up", "Starting a remote session"];
    }
    if (want === "asleep" && up) return ["sleeping", "Going to sleep", tvOff ? "The TV is off" : "The TV is on another input"];
    if (power === "restarting") return ["waking", "Restarting", "Waiting for the PC to come back"];
    if (want === "awake" && d.wol_attempts) return ["waking", "Waking up", "Sending Wake-on-LAN to the PC"];
    if (power === "off") return ["off", "Off", waiting];
    if (power === "asleep") return ["asleep", "Asleep", waiting];
    if (want === "awake") return ["waking", "Waking up", "Sending Wake-on-LAN to the PC"];
    if (want === "asleep") return ["asleep", "Asleep or off", waiting];
    return ["unknown", "Standing by", "Waiting to hear which input the TV is on"];
  }

  function render(d) {
    last = d;
    var v = view(d);
    document.body.setAttribute("data-state", v[0]);
    put("state", v[1]);
    put("kicker", d.pc_name || "Gaming PC");
    var title = v[1] + " - " + (d.pc_name || "Gaming PC") + " | CouchLink";
    if (document.title !== title) document.title = title;
    put("subText", v[2]);
    put("subHost", d.pc_host || "");

    put("version", "v" + d.version);
    put("verFoot", "CouchLink " + d.version);
    $("dry").hidden = !d.dry_run;

    var tvOn = d.tv_power === "on";
    var onPc = d.pc_should_be === "awake";
    $("nTv").className = "node" + (tvOn ? " on" : "");
    $("nIn").className = "node" + (tvOn && d.tv_input_address ? " on" : "");
    put("nInText", d.tv_input_address === "standby" ? "Off" : d.tv_input_address ? d.tv_input : "Input");
    $("nPc").className = "node" + (up(d) ? " on" : "");
    $("l1").className = "link" + (tvOn ? " on" : "");
    $("l2").className = "link" + (onPc && d.pc_reachable ? " on" : "");

    var power = d.tv_power === "on" ? ["On", "good"] : d.tv_power === "standby" ? ["Standby", "calm"] : ["Unknown", "dim"];
    set("vTv", power[0], power[1]);
    put("hTv", every(d.power_poll_interval));
    accent("tTv", d.tv_power === "on" ? "var(--green)" : null);

    if (d.tv_input_address === "standby") {
      set("vIn", "None", "dim");
      put("hIn", "TV off for " + dur(d.tv_input_for_seconds));
    } else if (d.tv_input_address) {
      set("vIn", d.tv_input, onPc ? "good" : "");
      put("hIn", "for " + dur(d.tv_input_for_seconds));
    } else {
      set("vIn", "Unknown", "dim");
      put("hIn", "Waiting for the TV");
    }
    accent("tIn", onPc ? "var(--green)" : null);

    var offline = {asleep: "Asleep", off: "Off", restarting: "Restarting"}[d.pc_power] || "Offline";
    set("vNet", d.pc_reachable === null ? "Checking" : d.pc_reachable ? "On" : offline,
        d.pc_reachable ? "good" : d.pc_reachable === false ? "calm" : "dim");
    put("hNet", d.pc_state_seconds === null || d.pc_state_seconds === undefined ? "-"
        : (d.pc_state_since_start ? "for at least " : "for ") + dur(d.pc_state_seconds));
    accent("tNet", d.pc_reachable ? "var(--green)" : null);

    set("vWol", d.wol_attempts ? d.wol_attempts + " sent" : "Idle", d.wol_attempts ? "cool" : "dim");
    put("hWol", lastSent(d.last_wol_seconds_ago));
    accent("tWol", d.wol_attempts ? "var(--cyan)" : null);

    set("vSus", d.suspend_attempts ? d.suspend_attempts + " sent" : "Idle", d.suspend_attempts ? "warn" : "dim");
    put("hSus", lastSent(d.last_suspend_seconds_ago));
    accent("tSus", d.suspend_attempts ? "var(--amber)" : null);

    set("vGuard", d.guard_active ? "Armed" : "Idle", d.guard_active ? "cool" : "dim");
    put("hGuard", "Undoes stray switches");
    accent("tGuard", d.guard_active ? "var(--cyan)" : null);

    var left = d.pc_sleep_check_seconds_left;
    var then = {switch: "Then: TV to " + (d.on_pc_sleep_target || "another input"),
                standby: "Then: TV off"}[d.on_pc_sleep] || "Then: TV left alone";
    var counting = left !== null && left !== undefined;
    if (counting && d.pc_power === "restarting") {
      set("vSleep", "On hold", "calm");     // a restart doesn't count as going to sleep
      put("hSleep", "PC is restarting");
      accent("tSleep", null);
    } else {
      set("vSleep", counting ? left + "s" : "Idle", counting ? "warn" : "dim");
      put("hSleep", then);
      accent("tSleep", counting ? "var(--amber)" : null);
    }

    set("vUp", dur(d.uptime_seconds), "");
    var started = new Date(Date.now() - d.uptime_seconds * 1000);
    var at = started.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.uptime_seconds >= 86400) at = started.toLocaleDateString([], { month: "short", day: "numeric" }) + ", " + at;
    put("hUp", "Since " + at);

    controls(d);

    put("cLog", String((d.recent_log || []).length));
    put("cCec", String((d.recent_traffic || []).length));
    drawLines(false);

    put("updated", "Updated " + new Date().toLocaleTimeString());
  }
  function up(d) { return d.pc_reachable === true; }

  // Wake and sleep buttons. Each shows only when it would do something.
  var noteUntil = 0;
  function note(text, hold) {
    put("ctlNote", text);
    noteUntil = hold ? Date.now() + 8000 : 0;
  }
  function controls(d) {
    var allowed = !!d.actions_enabled;
    $("controls").hidden = !allowed;
    if (!allowed) return;
    var onPc = d.pc_should_be === "awake";
    var session = d.remote_session_seconds !== null && d.remote_session_seconds !== undefined;
    $("bWake").hidden = onPc || session;
    $("bWakeTv").hidden = onPc && up(d);
    $("bEnd").hidden = !session;
    $("bSleep").hidden = !up(d);
    if (Date.now() < noteUntil) return;
    note(d.dry_run ? "Dry run: the buttons only log what they would do." : "");
  }
  function act(btn, path, body, ask) {
    if (ask && !window.confirm(ask)) return;
    btn.disabled = true;
    fetch(path, { method: "POST", cache: "no-store", body: JSON.stringify(body || {}),
                  headers: { "Content-Type": "application/json" } })
      .then(function (r) {
        return r.json().catch(function () { return { message: "CouchLink answered " + r.status }; });
      })
      .then(function (j) { note(j.message || "Done", true); })
      .catch(function () { note("Could not reach CouchLink", true); })
      .then(function () { btn.disabled = false; poll(); });
  }
  $("bWake").addEventListener("click", function () { act(this, "/api/wake", { tv: false }); });
  $("bWakeTv").addEventListener("click", function () { act(this, "/api/wake", { tv: true }); });
  $("bEnd").addEventListener("click", function () { act(this, "/api/session/end"); });
  $("bSleep").addEventListener("click", function () { act(this, "/api/sleep", {}, "Put the PC to sleep?"); });

  var LOG_RE = /^(\d\d:\d\d:\d\d) \[(\w+)\] \[([\w-]+)\] (.*)$/;
  var CEC_RE = /^(\d\d:\d\d:\d\d) .*?(>>|<<)\s*(.*)$/;

  function span(cls, text) {
    var s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  }

  function drawLines(force) {
    if (!last) return;
    var box = $("lines");
    var items = tab === "log" ? (last.recent_log || []) : (last.recent_traffic || []);
    var key = tab + "|" + items.length + "|" + (items[items.length - 1] || "");
    if (!force && key === drawn) return;
    drawn = key;
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    var frag = document.createDocumentFragment();
    items.forEach(function (text) {
      var row = document.createElement("div");
      row.className = "line";
      var m;
      if (tab === "log" && (m = LOG_RE.exec(text))) {
        row.appendChild(span("t", m[1]));
        if (m[2] !== "INFO") row.appendChild(span("lv " + m[2], m[2]));
        row.appendChild(span("c", m[3]));
        row.appendChild(span("m", m[4]));
      } else if (tab === "cec" && (m = CEC_RE.exec(text))) {
        row.appendChild(span("t", m[1]));
        row.appendChild(span("dir " + (m[2] === ">>" ? "rx" : "tx"), m[2] === ">>" ? "RX" : "TX"));
        row.appendChild(span("m", m[3]));
      } else {
        row.appendChild(span("m", text));
      }
      frag.appendChild(row);
    });
    box.textContent = "";
    if (!items.length) box.appendChild(span("empty", tab === "log" ? "No events yet" : "No CEC traffic yet"));
    box.appendChild(frag);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function selectTab(name) {
    tab = name;
    $("tabLog").setAttribute("aria-selected", name === "log");
    $("tabCec").setAttribute("aria-selected", name === "cec");
    drawLines(true);
    var box = $("lines");
    box.scrollTop = box.scrollHeight;
  }
  $("tabLog").addEventListener("click", function () { selectTab("log"); });
  $("tabCec").addEventListener("click", function () { selectTab("cec"); });

  function setLive(ok) {
    failures = ok ? 0 : failures + 1;
    var lost = failures >= 2;               // one missed refresh can be a blip
    $("live").className = "live" + (lost ? " off" : "");
    put("liveText", lost ? "No contact" : "Live");
    if (lost && !last) {
      put("state", "No connection");
      put("subText", "CouchLink is not answering");
    }
    var stale = lost && last !== null;
    document.body.classList.toggle("stale", stale);
    $("lost").hidden = !stale;
    if (stale) {
      put("lostText", "Showing what it last reported at " +
          lastOk.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) +
          ". Check that the Pi is on and on the network.");
    }
  }

  function poll() {
    fetch("/api", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { lastOk = new Date(); render(d); setLive(true); })
      .catch(function () { setLive(false); });
  }
  poll();
  setInterval(poll, 2000);
})();
