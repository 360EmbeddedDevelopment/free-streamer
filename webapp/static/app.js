/* Tap a stream, poll the status, show the log. That is the whole front end.

   Playing is not instant - Firefox has to start, the page has to load, and the
   video has to be found and clicked - so the button stays disabled and the bar
   says "starting" until the child logs that it is actually playing. */

(function () {
  "use strict";

  var POLL_MS = 2000;
  // What the server plays a pasted link under; it matches no button, so the
  // direct-link field lights up instead of a tile. Kept in step with
  // views.DIRECT_ID.
  var DIRECT_ID = "__direct__";

  var root = document.documentElement;
  var dot = document.getElementById("dot");
  var statusText = document.getElementById("statusText");
  var stopButton = document.getElementById("stop");
  var logBox = document.getElementById("logbox");
  var logToggle = document.getElementById("logToggle");
  var log = document.getElementById("log");
  var directForm = document.getElementById("directForm");
  var directField = document.getElementById("directField");
  var directUrl = document.getElementById("directUrl");
  var directPlay = document.getElementById("directPlay");
  var directError = document.getElementById("directError");
  var buttons = Array.prototype.slice.call(document.querySelectorAll(".stream"));
  var busy = false;
  var timer = null;

  function showLog(open) {
    if (!logBox) return;
    logBox.open = open;
    if (logToggle) logToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function post(url, opts) {
    if (busy) return;
    opts = opts || {};
    busy = true;
    setBusy(true);

    var init = { method: "POST" };
    if (opts.body !== undefined) {
      // Routes that write to disk require the header a cross-site form cannot
      // send; see auth.require_fetch.
      init.headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "stream-panel"
      };
      init.body = JSON.stringify(opts.body);
    }

    fetch(url, init)
      .then(function (r) {
        return r.json()
          .catch(function () { return {}; })   // an error page is not JSON
          .then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
      })
      .then(function (res) {
        if (!res.ok) {
          // A handler returning true has already shown the problem its own way.
          if (opts.onError && opts.onError(res.body, res.status)) return;
          render({ state: "failed", message: res.body.error || "request failed", log: [] });
          return;
        }
        render(res.body);
      })
      .catch(function (err) {
        statusText.textContent = "lost contact with the Pi (" + err.message + ")";
      })
      .finally(function () {
        busy = false;
        setBusy(false);
        schedule(300); // a fresh status shortly after, once the child has moved on
      });
  }

  function setBusy(on) {
    buttons.forEach(function (b) { b.disabled = on; });
    stopButton.disabled = on;
    if (directPlay) directPlay.disabled = on;
  }

  function showDirectError(message) {
    if (!directError) return;
    directError.textContent = message || "";
    directError.hidden = !message;
  }

  function render(status) {
    root.setAttribute("data-status", status.state);
    statusText.textContent = status.message || labelFor(status.state);
    stopButton.disabled = busy || status.state === "idle" || status.state === "failed";

    buttons.forEach(function (b) {
      b.classList.toggle("live", b.dataset.id === status.stream_id);

      // Which link the next tap on this button will play. The server owns the
      // position, so it is read from the status rather than counted here - two
      // phones tapping share one cycle, and a reload does not lose it.
      var cycle = b.querySelector("[data-cycle]");
      if (cycle) {
        var at = b.dataset.id === status.cycle_id ? status.cycle_index : 0;
        cycle.textContent = (at + 1) + "/" + b.dataset.links;
      }
    });

    // The pasted link gets the same treatment a button does, by the same test.
    if (directField) {
      directField.classList.toggle("live", status.stream_id === DIRECT_ID);
    }

    if (status.log && log) {
      var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
      var text = status.log.join("\n");
      if (text !== log.textContent) {
        log.textContent = text;
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
    }
  }

  function labelFor(state) {
    return state === "idle" ? "nothing playing" : state;
  }

  function poll() {
    fetch("api/status", { headers: { "Cache-Control": "no-cache" } })
      .then(function (r) { return r.json(); })
      .then(function (status) {
        if (!busy) render(status);
        // Nothing is moving once it is settled, so ease off the polling.
        schedule(status.state === "starting" || status.state === "stopping" ? 1000 : POLL_MS);
      })
      .catch(function () {
        statusText.textContent = "lost contact with the Pi";
        schedule(5000);
      });
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(poll, ms);
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      if (button.dataset.unhandled &&
          !confirm("No site class claims this URL, so nothing will drive the page. Open it anyway?")) {
        return;
      }
      statusText.textContent = "starting…";
      root.setAttribute("data-status", "starting");
      // The log stays shut unless you ask for it with the header button; the
      // status line already says what is happening.
      post("api/play/" + encodeURIComponent(button.dataset.id));
    });
  });

  stopButton.addEventListener("click", function () { post("api/stop"); });

  if (logToggle) {
    logToggle.addEventListener("click", function () { showLog(!logBox.open); });
  }

  // A link to watch once. Nothing is saved: an unsupported domain is refused
  // here rather than failing slowly inside stream.py, and gets parked for the
  // Advanced tab to show.
  if (directForm) {
    directForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var url = directUrl.value.trim();
      if (!url) {
        showDirectError("Paste a link first.");
        directUrl.focus();
        return;
      }
      showDirectError("");
      post("api/play/direct", {
        body: { url: url },
        onError: function (body, status) {
          if (status === 422) {
            showDirectError(
              (body.error || "unknown domain") +
              (body.queued
                ? " — parked for review, nothing can drive it yet."
                : " — already on the review list.")
            );
          } else {
            showDirectError(body.error || "that link was refused");
          }
          return true;
        }
      });
    });

    directUrl.addEventListener("input", function () { showDirectError(""); });
  }

  // A phone locks its screen and freezes timers; catch up on wake.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) schedule(0);
  });

  schedule(POLL_MS);
})();
