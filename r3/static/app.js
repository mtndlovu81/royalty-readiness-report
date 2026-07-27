/* Royalty Readiness Report — client behaviour.
 *
 * The server renders everything. This only toggles what is already on the
 * page, so the profile is complete and readable with JavaScript disabled;
 * expansion is the one interactive extra, and the song page (a real URL) shows
 * the same detail without it.
 *
 * No framework, no build step.
 */
(function () {
  "use strict";

  var table = document.querySelector("main");
  if (!table) return;

  function panelFor(row) {
    var id = row.getAttribute("data-panel");
    return id ? document.getElementById(id) : null;
  }

  function setExpanded(row, open) {
    var panel = panelFor(row);
    var toggle = row.querySelector(".song-toggle");
    if (!panel || !toggle) return;

    panel.hidden = !open;
    row.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function toggleRow(row) {
    var panel = panelFor(row);
    if (!panel) return;
    setExpanded(row, panel.hidden);
  }

  /* Delegated, so rows added later (filtering in M4-A) keep working, and so a
   * 300-song profile costs one listener rather than three hundred. */
  table.addEventListener("click", function (event) {
    /* Let real links and controls inside a row behave normally. */
    if (event.target.closest("a, input, select, textarea")) return;

    var row = event.target.closest(".song-row");
    if (!row) return;

    /* The <button> fires its own click; without this the row handler would
     * immediately toggle it back shut. */
    if (event.target.closest(".song-toggle")) {
      event.preventDefault();
    }

    toggleRow(row);
  });

  /* The button is a real button, so Enter and Space already activate it and
   * arrive as clicks above. Nothing further needed for the keyboard. */
})();
