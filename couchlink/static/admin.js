(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  function show(which) {
    $("message").hidden = which !== "message";
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
        if (!j.ok) { message(j.message || "CouchLink answered " + j.status); return; }
        drawSettings(j.fields);
        drawKeys(j.keys);
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
