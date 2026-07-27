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

  /* ---- filter and sort -------------------------------------------------
   *
   * Both are pure DOM work: no request, no re-render from the server. The
   * server sorts too, so the control still works with this file absent and a
   * ?sort= URL can be shared.
   */

  var controls = document.getElementById("controls");
  if (!controls) return;

  var filterInput = document.getElementById("filter");
  var sortSelect = document.getElementById("sort");
  var status = document.getElementById("filter-status");
  var submit = document.getElementById("controls-submit");

  /* With scripting available the round trip is unnecessary. */
  if (submit) submit.hidden = true;

  function bodies() {
    return Array.prototype.slice.call(document.querySelectorAll(".songs tbody"));
  }

  /* Rows come in pairs — the row and its diagnostic panel — and the two must
   * never be separated by a sort. */
  function pairs(tbody) {
    var out = [];
    var rows = tbody.querySelectorAll("tr.song-row");
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var panel = row.nextElementSibling;
      out.push({
        row: row,
        panel: panel && panel.classList.contains("song-panel") ? panel : null
      });
    }
    return out;
  }

  var comparators = {
    worst: function (a, b) {
      var d = num(a.row, "severity") - num(b.row, "severity");
      return d || text(a.row, "title").localeCompare(text(b.row, "title"));
    },
    title: function (a, b) {
      return text(a.row, "title").localeCompare(text(b.row, "title"));
    },
    newest: function (a, b) {
      var x = text(a.row, "released");
      var y = text(b.row, "released");
      /* Undated sorts last rather than pretending to be the oldest thing. */
      if (!x && !y) return text(a.row, "title").localeCompare(text(b.row, "title"));
      if (!x) return 1;
      if (!y) return -1;
      return y.localeCompare(x);
    }
  };

  function num(el, name) { return parseInt(el.getAttribute("data-" + name), 10) || 0; }
  function text(el, name) { return el.getAttribute("data-" + name) || ""; }

  function applySort() {
    var how = comparators[sortSelect ? sortSelect.value : "worst"];
    if (!how) return;

    bodies().forEach(function (tbody) {
      var items = pairs(tbody);
      items.sort(how);
      /* One fragment per table, so the browser lays out once rather than
       * once per row — this runs over 284 rows on a large profile. */
      var fragment = document.createDocumentFragment();
      items.forEach(function (item) {
        fragment.appendChild(item.row);
        if (item.panel) fragment.appendChild(item.panel);
      });
      tbody.appendChild(fragment);
    });
  }

  function applyFilter() {
    var term = (filterInput ? filterInput.value : "").trim().toLowerCase();
    var shown = 0;
    var total = 0;

    bodies().forEach(function (tbody) {
      pairs(tbody).forEach(function (item) {
        total += 1;
        var match = !term || text(item.row, "search").indexOf(term) !== -1;
        item.row.hidden = !match;
        if (item.panel && !match) {
          /* A hidden row must not leave its panel behind. */
          item.panel.hidden = true;
          item.row.classList.remove("is-open");
          var toggle = item.row.querySelector(".song-toggle");
          if (toggle) toggle.setAttribute("aria-expanded", "false");
        }
        if (match) shown += 1;
      });
    });

    if (status) {
      status.textContent = term
        ? shown + " of " + total + " songs match “" + term + "”"
        : "";
    }
  }

  if (filterInput) {
    filterInput.addEventListener("input", applyFilter);
    /* Enter would submit the form and cost a round trip for work already done. */
    controls.addEventListener("submit", function (event) { event.preventDefault(); });
  }

  if (sortSelect) {
    sortSelect.addEventListener("change", function () {
      applySort();
      /* Keep the URL shareable without navigating. */
      if (window.history && window.history.replaceState) {
        var url = new URL(window.location.href);
        url.searchParams.set("sort", sortSelect.value);
        window.history.replaceState({}, "", url);
      }
    });
  }
})();
