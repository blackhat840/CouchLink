(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  function show(which) {
    $("message").hidden = which !== "message";
    $("setup").hidden = which !== "setup";
    $("login").hidden = which !== "login";
    $("panels").hidden = which !== "panels";
    $("logout").hidden = which !== "panels";
  }
  function message(text) {
    $("messageText").textContent = text;
    show("message");
  }

  // Every change is a POST from this page. The server only accepts them from here.
  function post(path, body) {
    return fetch(path, {
      method: "POST", cache: "no-store", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: "CouchLink answered " + r.status }; })
        .then(function (j) { j.status = r.status; return j; });
    });
  }

  function load() {
    fetch("/admin/api/state", { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (j) { j.status = r.status; return j; }); })
      .then(function (j) {
        if (j.result === "login_needed") { show("login"); $("password").focus(); return; }
        if (j.result === "setup") { show("setup"); $("setupCode").focus(); return; }
        if (!j.ok) { message(j.message || "CouchLink answered " + j.status); return; }
        drawSettings(j.fields);
        drawKeys(j.keys);
        drawTodo(j.todo || []);
        piLogical = j.pi_logical_address;
        $("finder").hidden = !j.cec;
        show("panels");
      })
      .catch(function () { message("Could not reach CouchLink. Check that the Pi is on."); });
  }

  // ---- settings
  var fields = [];
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function drawSettings(list) {
    fields = list;
    var form = $("settings");
    form.textContent = "";
    var group = null, box = null;
    list.forEach(function (f) {
      if (f.group !== group) {
        group = f.group;
        box = el("fieldset");
        box.appendChild(el("legend", {}, group));
        form.appendChild(box);
      }
      var id = "f_" + f.key, input;
      var wrap = el("div", { "class": f.kind === "bool" ? "field check" : "field" });
      if (f.kind === "select") {
        input = el("select", { id: id });
        f.options.forEach(function (o) {
          var opt = el("option", { value: o[0] }, o[1]);
          if (o[0] === f.value) opt.selected = true;
          input.appendChild(opt);
        });
      } else if (f.kind === "bool") {
        input = el("input", { id: id, type: "checkbox" });
        input.checked = !!f.value;
      } else {
        input = el("input", { id: id, type: f.kind === "number" ? "number" : "text", spellcheck: "false",
                              autocomplete: "off" });
        if (f.kind === "number") input.step = "any";
        input.value = f.value === null || f.value === undefined ? "" : String(f.value);
        if (f.blank) input.placeholder = f.blank;
      }
      var label = el("label", { "for": id }, f.label);
      if (f.kind === "bool") { wrap.appendChild(input); wrap.appendChild(label); }
      else { wrap.appendChild(label); wrap.appendChild(input); }
      if (f.key === "pc_mac") wrap.appendChild(findMacRow());
      box.appendChild(wrap);
    });
  }
  function values() {
    var out = {};
    fields.forEach(function (f) {
      var input = $("f_" + f.key);
      if (f.kind === "bool") out[f.key] = input.checked;
      else if (f.kind === "number") out[f.key] = input.value === "" ? null : Number(input.value);
      else out[f.key] = input.value;
    });
    return out;
  }
  $("settings").addEventListener("submit", function (e) {
    e.preventDefault();
    var problems = $("problems");
    problems.hidden = true;
    $("save").disabled = true;
    $("saveNote").textContent = "Saving";
    post("/admin/settings", { values: values() }).then(function (j) {
      $("save").disabled = false;
      $("saveNote").textContent = j.message || "";
      if (j.result === "login_needed") { show("login"); return; }
      if (j.problems && j.problems.length) {
        problems.textContent = "";
        j.problems.forEach(function (p) { problems.appendChild(el("li", {}, p)); });
        problems.hidden = false;
      }
      if (j.result === "saved") setTimeout(waitForRestart, 2500);
    }).catch(function () {
      $("save").disabled = false;
      $("saveNote").textContent = "Could not reach CouchLink";
    });
  });
  function waitForRestart() {
    fetch("/api", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(); $("saveNote").textContent = "Saved. CouchLink restarted with the new settings"; load(); })
      .catch(function () { setTimeout(waitForRestart, 1500); });
  }

  // ---- what still has to be filled in, on a first setup
  function drawTodo(problems) {
    var list = $("todoList");
    list.textContent = "";
    problems.forEach(function (p) { list.appendChild(el("li", {}, p)); });
    $("todo").hidden = !problems.length;
  }

  // ---- the MAC address, read from the Pi's network table
  function findMacRow() {
    var row = el("div", { "class": "row" });
    var b = el("button", { "class": "btn", type: "button" }, "Find it from the IP address");
    var note = el("span", { "class": "note", role: "status" });
    b.addEventListener("click", function () {
      note.textContent = "Looking";
      post("/admin/find-mac", { host: $("f_pc_host").value }).then(function (j) {
        note.textContent = j.message || "";
        if (j.ok) $("f_pc_mac").value = j.mac;
      });
    });
    row.appendChild(b);
    row.appendChild(note);
    return row;
  }

  // ---- finding the HDMI inputs: scan the bus, then listen for the switch to the PC
  var piLogical = 1, found = null, listening = false;
  function setField(key, value) {
    var input = $("f_" + key);
    if (input) input.value = value;
  }
  // One tile per device, like the status page's. A lit tile is one CouchLink will use.
  function tile(label, value, hint, color) {
    var t = el("div", { "class": "panel tile" });
    if (color) t.style.setProperty("--tile-accent", color);
    t.appendChild(el("div", { "class": "label" }, label));
    t.appendChild(el("div", { "class": "value" }, value));
    t.appendChild(el("div", { "class": "hint" }, hint || "-"));
    return t;
  }
  function busy(on) { $("finderOrb").classList.toggle("busy", on); }
  $("scan").addEventListener("click", function () {
    var note = $("finderNote"), list = $("devices");
    $("scan").disabled = true;
    $("listen").disabled = true;
    busy(true);
    note.textContent = "Asking every HDMI device who it is. This can take up to a minute";
    post("/admin/cec/scan").then(function (j) {
      $("scan").disabled = false;
      busy(false);
      if (!j.ok) { note.textContent = j.message || "The scan didn't work"; return; }
      var layout = $("layout").value;
      var pi = j.devices.filter(function (d) { return d.logical === piLogical; })[0];
      var bar = layout === "tv" ? null : j.devices.filter(function (d) { return d.logical === 5; })[0];
      found = { known: j.devices.map(function (d) { return d.address; }), pi: pi, bar: bar };
      list.textContent = "";
      j.devices.forEach(function (d) {
        var label = d === pi ? "This Pi" : d === bar ? "Soundbar or receiver" : d.name;
        var color = d === pi ? "var(--cyan)" : d === bar ? "var(--violet)" : "";
        list.appendChild(tile(label, d.dotted, d === pi || d === bar ? d.name : (d.osd || d.vendor), color));
      });
      list.hidden = false;
      $("f_use_tv").checked = true;
      if (pi) setField("pi_address", pi.address);
      setField("soundbar_address", bar ? bar.address : "");
      note.textContent = (pi ? "Found the Pi" : "The Pi didn't answer, so type its input below") +
        (layout === "tv" ? "" : bar ? " and the soundbar" : ". No soundbar answered, so type its input below") +
        ". Now press 2, then switch the TV to the PC";
      $("listen").disabled = false;
    }).catch(function () { $("scan").disabled = false; busy(false); note.textContent = "Could not reach CouchLink"; });
  });
  $("listen").addEventListener("click", function () {
    if (listening || !found) return;
    var note = $("finderNote"), end = Date.now() + 60000;
    var skip = found.known.concat(["00:00"]);
    listening = true;
    $("listen").disabled = true;
    busy(true);
    note.textContent = "Listening. Switch the TV to the PC now. If the PC is already on screen, switch away and back";
    post("/admin/cec/heard").then(function (start) {
      function poll() {
        post("/admin/cec/heard", { since: start.since }).then(function (j) {
          var pc = (j.heard || []).filter(function (h) { return skip.indexOf(h.address) < 0; })[0];
          if (pc) {
            listening = false;
            $("listen").disabled = false;
            busy(false);
            var t = tile("The PC", pc.dotted, "Heard when you switched", "var(--green)");
            t.id = "pcTile";
            if ($("pcTile")) $("pcTile").remove();
            $("devices").insertBefore(t, $("devices").firstChild);
            setField("pc_addresses", pc.address);
            note.textContent = "Heard a switch to " + pc.dotted + ". It's filled in as the PC's input. " +
              "Check the inputs below, then save";
            return;
          }
          if (Date.now() > end) {
            listening = false;
            $("listen").disabled = false;
            busy(false);
            note.textContent = "Didn't hear a switch. Your TV may not announce it. Type the PC's input below, " +
              "such as 12:00 for input 2 of a soundbar on TV input 1";
            return;
          }
          setTimeout(poll, 1500);
        });
      }
      poll();
    });
  });

  // ---- the first visit: setup code and a password
  $("setupForm").addEventListener("submit", function (e) {
    e.preventDefault();
    if ($("setupPassword").value !== $("setupPassword2").value) {
      $("setupNote").textContent = "The two passwords don't match";
      return;
    }
    post("/admin/setup", { code: $("setupCode").value, password: $("setupPassword").value }).then(function (j) {
      $("setupNote").textContent = j.ok ? "" : (j.message || "");
      if (j.ok) load();
    });
  });

  $("checkSsh").addEventListener("click", function () {
    $("saveNote").textContent = "Checking";
    post("/admin/check-ssh").then(function (j) { $("saveNote").textContent = j.message || ""; });
  });

  // ---- API keys
  function drawKeys(names) {
    var list = $("keys");
    list.textContent = "";
    if (!names.length) list.appendChild(el("li", { "class": "note" }, "No keys yet."));
    names.forEach(function (name) {
      var li = el("li");
      li.appendChild(el("code", {}, name));
      var b = el("button", { "class": "btn", type: "button" }, "Revoke");
      b.addEventListener("click", function () {
        if (!window.confirm("Revoke the key " + name + "? Anything that uses it stops working.")) return;
        post("/admin/keys/revoke", { name: name }).then(function (j) {
          $("keyNote").textContent = j.message || "";
          if (j.ok) load();
        });
      });
      li.appendChild(b);
      list.appendChild(li);
    });
  }
  $("newKey").addEventListener("submit", function (e) {
    e.preventDefault();
    var name = $("keyName").value.trim();
    post("/admin/keys/new", { name: name }).then(function (j) {
      $("keyNote").textContent = j.ok ? "" : (j.message || "");
      if (!j.ok) return;
      $("keyName").value = "";
      $("newKeyName").textContent = j.name;
      $("newKeyValue").textContent = j.key;
      $("newKeyBox").hidden = false;
      load();
    });
  });
  $("copyKey").addEventListener("click", function () {
    var text = $("newKeyValue").textContent;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { $("keyNote").textContent = "Copied"; });
    } else {
      var range = document.createRange();          // plain http: select it so it can be copied
      range.selectNodeContents($("newKeyValue"));
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      $("keyNote").textContent = "Selected. Copy it with your device's copy command";
    }
  });

  // ---- password, login and logout
  $("passwordForm").addEventListener("submit", function (e) {
    e.preventDefault();
    post("/admin/password", { current: $("current").value, "new": $("newPassword").value }).then(function (j) {
      $("passwordNote").textContent = j.message || "";
      if (j.ok) { $("current").value = ""; $("newPassword").value = ""; }
    });
  });
  $("loginForm").addEventListener("submit", function (e) {
    e.preventDefault();
    $("loginNote").textContent = "Checking";
    post("/admin/login", { password: $("password").value }).then(function (j) {
      $("loginNote").textContent = j.ok ? "" : (j.message || "");
      if (j.ok) { $("password").value = ""; load(); }
    });
  });
  $("logout").addEventListener("click", function () {
    post("/admin/logout").then(function () { show("login"); });
  });

  load();
})();
