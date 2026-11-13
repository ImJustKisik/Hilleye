/* Hilleye client interactions (CSP-safe external script) */
(function () {
  "use strict";

  function initDirectoryControls() {
    var list = document.getElementById("servers-grid-container");
    var filterInput = document.getElementById("server-filter-input");
    var clearBtn = document.getElementById("server-filter-clear");
    var populatedOnly = document.getElementById("server-populated-only");
    var sortSelect = document.getElementById("server-sort-select");
    var counter = document.getElementById("server-visible-count");
    var emptyState = document.getElementById("server-filter-empty");
    var toggleBtn = document.getElementById("servers-toggle-btn");
    var dirBody = document.getElementById("servers-directory-body");
    var toggleLabel = document.getElementById("servers-toggle-label");

    if (toggleBtn && dirBody && !toggleBtn.dataset.bound) {
      toggleBtn.dataset.bound = "1";
      toggleBtn.addEventListener("click", function () {
        var isOpen = dirBody.classList.toggle("is-open");
        toggleBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
        if (toggleLabel) {
          toggleLabel.textContent = isOpen ? "Hide servers" : "Show servers";
        }
      });
    }

    if (!list || list.dataset.bound) return;
    list.dataset.bound = "1";

    var items = Array.prototype.slice.call(list.children);
    var totalServers = items.length;

    function applyFilterAndSort() {
      var rawQuery = filterInput ? filterInput.value : "";
      var query = rawQuery.trim().toLowerCase();
      var onlyPopulated = populatedOnly ? populatedOnly.checked : false;
      var sortBy = sortSelect ? sortSelect.value : "players";

      var sorted = items.slice().sort(function (a, b) {
        var linkA = a.querySelector(".sector-row");
        var linkB = b.querySelector(".sector-row");
        if (!linkA || !linkB) return 0;

        if (sortBy === "name") {
          var tA = (linkA.dataset.title || "").toLowerCase();
          var tB = (linkB.dataset.title || "").toLowerCase();
          return tA.localeCompare(tB);
        }
        if (sortBy === "duration") {
          var dA = parseInt(linkA.dataset.duration || "-1", 10);
          var dB = parseInt(linkB.dataset.duration || "-1", 10);
          if (dB !== dA) return dB - dA;
        }
        var pA = parseInt(linkA.dataset.players || "-1", 10);
        var pB = parseInt(linkB.dataset.players || "-1", 10);
        if (pB !== pA) return pB - pA;
        var idxA = parseInt(linkA.dataset.index || "0", 10);
        var idxB = parseInt(linkB.dataset.index || "0", 10);
        return idxA - idxB;
      });

      var visible = 0;
      sorted.forEach(function (li) {
        var row = li.querySelector(".sector-row");
        if (!row) return;
        var hay = (row.getAttribute("data-search") || row.textContent || "").toLowerCase();
        var players = parseInt(row.dataset.players || "0", 10);
        var matchesQuery = !query || hay.indexOf(query) !== -1;
        var matchesPopulated = !onlyPopulated || players > 0;
        var show = matchesQuery && matchesPopulated;
        li.hidden = !show;
        if (show) visible += 1;
        list.appendChild(li);
      });

      if (counter) {
        counter.textContent = visible + " / " + totalServers;
      }
      if (clearBtn) {
        clearBtn.hidden = !rawQuery.length;
      }
      if (emptyState) {
        emptyState.hidden = visible > 0;
      }
    }

    if (filterInput) {
      filterInput.addEventListener("input", applyFilterAndSort);
    }
    if (populatedOnly) {
      populatedOnly.addEventListener("change", applyFilterAndSort);
    }
    if (sortSelect) {
      sortSelect.addEventListener("change", applyFilterAndSort);
    }
    if (clearBtn && filterInput) {
      clearBtn.addEventListener("click", function () {
        filterInput.value = "";
        applyFilterAndSort();
        filterInput.focus();
      });
    }
  }

  function initRosterFilter() {
    var input = document.getElementById("roster-filter-input");
    var table = document.getElementById("online-roster-table");
    var counter = document.getElementById("roster-visible-count");
    if (!input || !table || input.dataset.bound) return;
    input.dataset.bound = "1";

    var tbody = table.querySelector("tbody");
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
    var total = rows.length;

    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var visible = 0;
      rows.forEach(function (tr) {
        var ckey = (tr.dataset.ckey || tr.textContent || "").toLowerCase();
        var match = !q || ckey.indexOf(q) !== -1;
        tr.hidden = !match;
        if (match) visible += 1;
      });
      if (counter) {
        counter.textContent = q ? visible + " / " + total + " online" : total + " online";
      }
    });
  }

  function initCopyButtons() {
    document.addEventListener("click", function (event) {
      var btn = event.target.closest(".copy-link-btn");
      if (!btn) return;
      var path = btn.getAttribute("data-copy-path") || window.location.pathname;
      var url = new URL(path, window.location.origin).toString();
      var originalText = btn.textContent;

      function markCopied() {
        btn.textContent = "Copied";
        btn.classList.add("is-copied");
        window.setTimeout(function () {
          btn.textContent = originalText;
          btn.classList.remove("is-copied");
        }, 1600);
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(markCopied).catch(function () {});
      } else {
        var temp = document.createElement("input");
        temp.value = url;
        document.body.appendChild(temp);
        temp.select();
        try {
          document.execCommand("copy");
          markCopied();
        } catch (err) {
          // Ignore clipboard fallback errors
        }
        document.body.removeChild(temp);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initDirectoryControls();
    initRosterFilter();
    initCopyButtons();
  });

  document.addEventListener("htmx:historyRestore", function () {
    initDirectoryControls();
    initRosterFilter();
  });
})();
