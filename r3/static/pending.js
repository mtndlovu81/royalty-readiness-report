/* Status page poller.
 *
 * Polls the build's own status endpoint every 3 seconds and moves the bar to
 * whatever the worker has actually reported. It never advances the bar on its
 * own: a progress bar that drifts forward while nothing is happening is a lie,
 * and this page exists precisely for the case where a build is slow.
 *
 * Without JavaScript the page still shows the phase and percentage the server
 * rendered — reloading it is the manual equivalent.
 */
(function () {
  "use strict";

  var root = document.getElementById("pending");
  if (!root) return;

  var id = root.getAttribute("data-request-id");
  if (!id) return;

  var bar = document.getElementById("progress-bar");
  var meter = document.getElementById("progress");
  var label = document.getElementById("progress-label");
  var stalled = document.getElementById("stalled-notice");
  var lede = document.getElementById("pending-lede");
  var title = document.getElementById("pending-title");

  var INTERVAL = 3000;
  var failures = 0;

  function render(data) {
    if (data.done && data.slug) {
      /* Replace rather than push: the status page shouldn't sit in history
       * between the search results and the profile. */
      window.location.replace("/artist/" + data.slug);
      return true;
    }

    if (data.status === "failed") {
      window.location.reload();
      return true;
    }

    if (bar && typeof data.progress_pct === "number") {
      bar.style.width = data.progress_pct + "%";
      if (meter) meter.setAttribute("aria-valuenow", String(data.progress_pct));
    }

    if (label) {
      if (data.stalled) label.textContent = "Not started yet";
      else if (data.progress) label.textContent = data.progress;
    }

    if (stalled) {
      stalled.hidden = !data.stalled;
      /* Once we know it hasn't started, the reassuring copy is misleading —
       * swap the heading rather than showing both messages at once. */
      if (data.stalled) {
        if (lede) lede.hidden = true;
        if (title) title.textContent = "This is taking longer than usual";
      } else {
        if (lede) lede.hidden = false;
        if (title) title.textContent = "We're building this profile";
      }
    }

    return false;
  }

  function poll() {
    fetch("/api/pending/" + encodeURIComponent(id), {
      headers: { "Accept": "application/json" }
    })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) {
        failures = 0;
        if (!render(data)) window.setTimeout(poll, INTERVAL);
      })
      .catch(function () {
        /* Back off rather than hammering a server that may be struggling.
         * Give up after a while and leave the last known state on screen. */
        failures += 1;
        if (failures < 5) window.setTimeout(poll, INTERVAL * failures);
      });
  }

  window.setTimeout(poll, INTERVAL);
})();
