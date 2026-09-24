/* The settings page: edit the buttons, and - once signed in - the rest.

   Every save carries the catalog revision it was loaded with, so a page left
   open while streams.json is edited over SSH gets a refusal rather than
   quietly overwriting the change. */

(function () {
  "use strict";

  var flash = document.getElementById("flash");
  var list = document.getElementById("streamList");
  var addForm = document.getElementById("addForm");
  var addBox = document.getElementById("addBox");
  var revision = "";
  var streams = [];
  var editing = null;

  // -- plumbing ---------------------------------------------------------

  function api(method, url, body) {
    return fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json", "X-Requested-With": "stream-panel" },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || (method + " " + url + " failed"));
        return data;
      });
    });
  }

  function say(message, kind) {
    flash.textContent = message;
    flash.className = "flash " + (kind || "ok");
    flash.hidden = false;
    if (kind !== "bad") setTimeout(function () { flash.hidden = true; }, 4000);
  }

  function fail(err) { say(err.message || String(err), "bad"); }

  // -- tabs -------------------------------------------------------------

  function showTab(name) {
    if (name !== "advanced") name = "streams";
    document.querySelectorAll(".tab").forEach(function (t) {
      t.classList.toggle("on", t.dataset.tab === name);
    });
    document.getElementById("tab-streams").hidden = name !== "streams";
    document.getElementById("tab-advanced").hidden = name !== "advanced";
    if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
    if (name === "advanced") refreshAdmin();
  }

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () { showTab(tab.dataset.tab); });
  });
  window.addEventListener("hashchange", function () { showTab(location.hash.slice(1)); });

  // -- the stream list --------------------------------------------------

  function field(name, value, attrs) {
    var input = document.createElement("input");
    input.name = name;
    input.value = value == null ? "" : value;
    Object.keys(attrs || {}).forEach(function (k) { input.setAttribute(k, attrs[k]); });
    return input;
  }

  function area(name, value, attrs) {
    var el = document.createElement("textarea");
    el.name = name;
    el.value = value == null ? "" : value;
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    return el;
  }

  function labelled(text, control) {
    var label = document.createElement("label");
    label.append(text + " ", control);
    return label;
  }

  function select(name, options, value) {
    var el = document.createElement("select");
    el.name = name;
    options.forEach(function (opt) {
      var o = document.createElement("option");
      o.value = opt[0];
      o.textContent = opt[1];
      if (String(value == null ? "" : value) === opt[0]) o.selected = true;
      el.append(o);
    });
    return el;
  }

  function row(stream, index) {
    var li = document.createElement("li");
    li.className = "item";

    if (editing === stream.id) {
      li.append(editorFor(stream));
      return li;
    }

    var main = document.createElement("div");
    main.className = "itemmain";
    var title = document.createElement("div");
    title.className = "itemlabel";
    title.textContent = stream.label;
    var links = stream.urls || [stream.url];
    var url = document.createElement("div");
    url.className = "itemurl";
    // The first link, and how many more are behind it. Listing them all would
    // push everything else off a phone screen.
    url.textContent = links.length > 1
      ? links[0] + "  +" + (links.length - 1) + " more"
      : links[0];
    main.append(title, url);

    var meta = document.createElement("div");
    meta.className = "meta";
    [
      stream.group,
      stream.note,
      links.length > 1 ? links.length + " links" : "",
      // Distinct site classes, since one button's links may span several.
      (stream.handlers || [stream.handler]).filter(function (h, i, all) {
        return h && all.indexOf(h) === i;
      }).join(", ") || "unsupported",
      stream.effective && !stream.effective.autoplay ? "manual start" : ""
    ].filter(Boolean).forEach(function (text) {
      var badge = document.createElement("span");
      badge.className = "badge" + (text === "unsupported" ? " bad" : "");
      badge.textContent = text;
      meta.append(badge);
    });
    main.append(meta);

    var buttons = document.createElement("div");
    buttons.className = "itembuttons";
    buttons.append(
      button("↑", "ghost small", function () { move(index, -1); }, index === 0),
      button("↓", "ghost small", function () { move(index, 1); }, index === streams.length - 1),
      button("Edit", "ghost small", function () { editing = stream.id; render(); }),
      button("Delete", "danger small", function () { remove(stream); })
    );

    li.append(main, buttons);
    return li;
  }

  function button(text, className, onClick, disabled) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = className;
    b.textContent = text;
    b.disabled = !!disabled;
    b.addEventListener("click", onClick);
    return b;
  }

  function editorFor(stream) {
    var form = document.createElement("form");
    form.className = "form";
    form.append(
      labelled("Label", field("label", stream.label)),
      labelled("Links (one per line, tap the button again for the next)",
        area("urls", (stream.urls || [stream.url]).join("\n"),
          { required: "required", rows: "3", inputmode: "url" })),
      labelled("Group", field("group", stream.group, { list: "groups" })),
      labelled("Note", field("note", stream.note))
    );

    var sub = document.createElement("div");
    sub.className = "subgrid";
    sub.append(
      labelled("Site handler", select("site", [["", "match from the URL"]].concat(
        window.PANEL.sites.map(function (s) { return [s, s]; })), stream.site || "")),
      labelled("Autoplay", select("autoplay", [
        ["", "inherit the default"], ["true", "start the video"], ["false", "leave it to me"]
      ], stream.autoplay === null || stream.autoplay === undefined ? "" : String(stream.autoplay))),
      labelled("Retries", field("retries", stream.retries, { placeholder: "inherit", inputmode: "numeric" })),
      labelled("Extra Firefox args", field("firefox_args", (stream.firefox_args || []).join(" "), { placeholder: "inherit" })),
      labelled("Id", field("id", stream.id))
    );
    form.append(sub);

    var buttons = document.createElement("div");
    buttons.className = "row";
    var save = document.createElement("button");
    save.className = "primary";
    save.type = "submit";
    save.textContent = "Save";
    buttons.append(save, button("Cancel", "ghost", function () { editing = null; render(); }));
    form.append(buttons);

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      api("PUT", "api/streams/" + encodeURIComponent(stream.id), payload(form))
        .then(function (data) {
          editing = null;
          adopt(data.catalog);
          say("saved");
        })
        .catch(fail);
    });
    return form;
  }

  function payload(form) {
    var data = new FormData(form);
    var autoplay = data.get("autoplay");
    var args = (data.get("firefox_args") || "").trim();
    var links = (data.get("urls") || "").trim();
    return {
      id: (data.get("id") || "").trim() || undefined,
      label: data.get("label"),
      // Split on whitespace rather than commas: a comma is legal in a URL, a
      // space is not, and from_form rejects one anyway.
      urls: links ? links.split(/\s+/) : [],
      group: data.get("group"),
      note: data.get("note"),
      site: data.get("site") || null,
      autoplay: autoplay === "" || autoplay === null ? null : autoplay === "true",
      retries: (data.get("retries") || "").trim(),
      firefox_args: args ? args.split(/\s+/) : [],
      revision: revision
    };
  }

  function adopt(catalog) {
    streams = catalog.streams || [];
    revision = catalog.revision || "";
    (catalog.warnings || []).forEach(function (w) { say(w, "bad"); });
    render();
  }

  function render() {
    list.textContent = "";
    if (!streams.length) {
      var empty = document.createElement("li");
      empty.className = "hint";
      empty.textContent = "No streams yet — add one above.";
      list.append(empty);
      return;
    }
    streams.forEach(function (stream, index) { list.append(row(stream, index)); });
  }

  function move(index, delta) {
    var next = index + delta;
    if (next < 0 || next >= streams.length) return;
    var order = streams.map(function (s) { return s.id; });
    var moved = order.splice(index, 1)[0];
    order.splice(next, 0, moved);
    api("POST", "api/streams/reorder", { order: order, revision: revision })
      .then(function (data) { adopt(data.catalog); })
      .catch(fail);
  }

  function remove(stream) {
    if (!confirm("Delete “" + stream.label + "”?")) return;
    api("DELETE", "api/streams/" + encodeURIComponent(stream.id), { revision: revision })
      .then(function (data) { adopt(data.catalog); say("deleted"); })
      .catch(fail);
  }

  addForm.addEventListener("submit", function (event) {
    event.preventDefault();
    api("POST", "api/streams", payload(addForm))
      .then(function (data) {
        addForm.reset();
        addBox.open = false;
        adopt(data.catalog);
        say("added");
      })
      .catch(fail);
  });

  // -- advanced tab -----------------------------------------------------

  var loginForm = document.getElementById("loginForm");
  var advancedBody = document.getElementById("advancedBody");
  var advancedLocked = document.getElementById("advancedLocked");
  var defaultsForm = document.getElementById("defaultsForm");
  var output = document.getElementById("actionOutput");

  var unknownList = document.getElementById("unknownList");
  var unknownPath = document.getElementById("unknownPath");
  var clearUnknown = document.getElementById("clearUnknown");

  function showAdmin(state) {
    advancedLocked.hidden = state.configured;
    loginForm.hidden = !state.configured || state.admin;
    advancedBody.hidden = !state.admin;
    if (state.admin && state.defaults) fillDefaults(state.defaults);
    if (state.admin) refreshUnknown();
  }

  // -- links the direct-link bar refused --------------------------------

  function renderUnknown(data) {
    if (unknownPath) unknownPath.textContent = data.path || "";
    unknownList.textContent = "";

    var found = data.entries || [];
    clearUnknown.disabled = found.length === 0;
    if (!found.length) {
      var empty = document.createElement("li");
      empty.className = "hint";
      empty.textContent = "Nothing waiting.";
      unknownList.append(empty);
      return;
    }

    found.forEach(function (entry) {
      var li = document.createElement("li");
      li.className = "item";

      var main = document.createElement("div");
      main.className = "itemmain";

      var host = document.createElement("span");
      host.className = "itemlabel";
      host.textContent = entry.host || entry.url;

      var url = document.createElement("span");
      url.className = "itemurl";
      url.textContent = entry.url;

      main.append(host, url);
      if (entry.seen) {
        var seen = document.createElement("span");
        seen.className = "itemurl";
        seen.textContent = "first seen " + entry.seen.replace("T", " ");
        main.append(seen);
      }

      li.append(main);
      unknownList.append(li);
    });
  }

  function refreshUnknown() {
    if (!unknownList) return;
    api("GET", "api/admin/unknown")
      .then(renderUnknown)
      .catch(function () { /* signed out mid-poll; the pane just stays as it is */ });
  }

  if (clearUnknown) {
    clearUnknown.addEventListener("click", function () {
      api("DELETE", "api/admin/unknown")
        .then(function (data) {
          say(data.message);
          renderUnknown(data);
        })
        .catch(fail);
    });
  }

  function refreshAdmin() {
    fetch("api/admin/state", { headers: { "X-Requested-With": "stream-panel" } })
      .then(function (r) { return r.json(); })
      .then(showAdmin)
      .catch(fail);
  }

  function fillDefaults(defaults) {
    defaultsForm.elements.autoplay.value = String(defaults.autoplay);
    defaultsForm.elements.retries.value = defaults.retries == null ? "" : defaults.retries;
    defaultsForm.elements.backend.value = defaults.backend || "auto";
    defaultsForm.elements.profile.value = defaults.profile || "";
    defaultsForm.elements.firefox_args.value = (defaults.firefox_args || []).join(" ");
  }

  loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var data = new FormData(loginForm);
    api("POST", "api/login", {
      username: data.get("username"),
      password: data.get("password")
    })
      .then(function () {
        loginForm.reset();
        say("signed in");
        refreshAdmin();
      })
      .catch(fail);
  });

  document.getElementById("logout").addEventListener("click", function () {
    api("POST", "api/logout").then(function () { say("signed out"); refreshAdmin(); }).catch(fail);
  });

  defaultsForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var data = new FormData(defaultsForm);
    var args = (data.get("firefox_args") || "").trim();
    api("PUT", "api/admin/config", {
      defaults: {
        autoplay: data.get("autoplay") === "true",
        retries: (data.get("retries") || "").trim() === "" ? -1 : data.get("retries"),
        backend: data.get("backend"),
        profile: (data.get("profile") || "").trim(),
        firefox_args: args ? args.split(/\s+/) : []
      }
    })
      .then(function (state) { say("defaults saved"); fillDefaults(state.defaults); })
      .catch(fail);
  });

  function print(text) {
    output.textContent = text;
    output.hidden = false;
  }

  document.querySelectorAll(".action").forEach(function (button) {
    button.addEventListener("click", function () {
      var name = button.dataset.action;
      var body = {};
      if (button.dataset.confirm) {
        var typed = prompt('This will "' + button.textContent + '". Type ' + name + ' to confirm:');
        if (typed !== name) { say("cancelled"); return; }
        body.confirm = true;
      }
      button.disabled = true;
      print("running " + name + "…");
      api("POST", "api/admin/action/" + encodeURIComponent(name), body)
        .then(function (data) { print(data.message); })
        .catch(function (err) { print(err.message); fail(err); })
        .finally(function () { button.disabled = false; });
    });
  });

  document.getElementById("runDiagnostics").addEventListener("click", function () {
    print("collecting…");
    fetch("api/admin/diagnostics", { headers: { "X-Requested-With": "stream-panel" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) throw new Error(data.error);
        print((data.panes || []).map(function (p) {
          return "=== " + p.title + " ===\n" + p.output;
        }).join("\n\n"));
      })
      .catch(function (err) { print(err.message); });
  });

  // -- start ------------------------------------------------------------

  fetch("api/streams", { headers: { "X-Requested-With": "stream-panel" } })
    .then(function (r) { return r.json(); })
    .then(adopt)
    .catch(fail);

  showTab(location.hash.slice(1));
})();
