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
      var chip = event.target.closest(".not-found-suggestions .quick-player-chip");
      if (chip) {
        var input = document.getElementById("ckey-input");
        var textSpan = chip.querySelector(".mono-val");
        if (input && textSpan) {
          input.value = textSpan.textContent.trim();
        }
      }

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

  function initPlayerTypeahead() {
    var input = document.getElementById("ckey-input");
    var list = document.getElementById("ckey-suggest-list");
    var form = input ? input.closest("form") : null;
    if (!input || !list || !form || input.dataset.typeaheadBound) return;
    input.dataset.typeaheadBound = "1";

    var debounceTimer = null;
    var activeController = null;
    var items = [];
    var activeIndex = -1;

    function normalizeKey(str) {
      return (str || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    }

    function closeDropdown() {
      list.hidden = true;
      list.innerHTML = "";
      items = [];
      activeIndex = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function selectSuggestion(ckey) {
      input.value = ckey;
      closeDropdown();
      if (window.htmx && typeof window.htmx.trigger === "function") {
        window.htmx.trigger(form, "submit");
      } else if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        form.submit();
      }
    }

    function setActiveIndex(idx) {
      activeIndex = idx;
      var nodes = list.querySelectorAll(".ckey-suggest-item");
      nodes.forEach(function (node, i) {
        var isSel = i === activeIndex;
        node.classList.toggle("is-active", isSel);
        node.setAttribute("aria-selected", isSel ? "true" : "false");
        if (isSel) {
          input.setAttribute("aria-activedescendant", node.id);
        }
      });
      if (activeIndex < 0) {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function renderSuggestions(suggestions) {
      list.innerHTML = "";
      items = suggestions || [];
      activeIndex = -1;

      if (!items.length) {
        closeDropdown();
        return;
      }

      items.forEach(function (item, idx) {
        var li = document.createElement("li");
        li.id = "ckey-suggest-opt-" + idx;
        li.className = "ckey-suggest-item";
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        li.dataset.ckey = item.ckey;

        var left = document.createElement("div");
        left.className = "ckey-suggest-main";

        var dot = document.createElement("span");
        dot.className = "status-dot " + (item.is_online ? "online" : "offline");
        dot.setAttribute("aria-hidden", "true");
        left.appendChild(dot);

        var nameSpan = document.createElement("span");
        nameSpan.className = "ckey-suggest-name mono-val";
        nameSpan.textContent = item.ckey;
        left.appendChild(nameSpan);

        if (item.match_kind === "fuzzy") {
          var badge = document.createElement("span");
          badge.className = "ckey-suggest-badge";
          badge.textContent = "similar";
          left.appendChild(badge);
        }

        var right = document.createElement("div");
        right.className = "ckey-suggest-meta mono-val";
        if (item.is_online) {
          var srvText = item.current_server ? "Online \u00b7 " + item.current_server : "Online";
          right.textContent = srvText + " \u00b7 " + item.total_hours + "h";
        } else {
          right.textContent = item.total_hours + "h \u00b7 " + (item.last_seen ? item.last_seen.slice(0, 10) : "Offline");
        }

        li.appendChild(left);
        li.appendChild(right);

        li.addEventListener("mousedown", function (e) {
          // Prevent input blur before selection fires
          e.preventDefault();
          selectSuggestion(item.ckey);
        });

        li.addEventListener("mouseenter", function () {
          setActiveIndex(idx);
        });

        list.appendChild(li);
      });

      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function fetchSuggestions(rawVal) {
      var norm = normalizeKey(rawVal);
      if (norm.length < 2) {
        if (activeController) {
          activeController.abort();
          activeController = null;
        }
        closeDropdown();
        return;
      }

      if (activeController) {
        activeController.abort();
      }
      activeController = typeof AbortController !== "undefined" ? new AbortController() : null;
      var fetchOpts = { headers: { Accept: "application/json" } };
      if (activeController) {
        fetchOpts.signal = activeController.signal;
      }

      fetch("/api/v1/players/suggest?q=" + encodeURIComponent(norm) + "&limit=8", fetchOpts)
        .then(function (resp) {
          if (!resp.ok) throw new Error("Suggest failed");
          return resp.json();
        })
        .then(function (data) {
          if (normalizeKey(input.value) !== norm) return;
          renderSuggestions(data.suggestions || []);
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return;
          closeDropdown();
        });
    }

    input.addEventListener("input", function () {
      if (debounceTimer) window.clearTimeout(debounceTimer);
      var val = input.value;
      debounceTimer = window.setTimeout(function () {
        fetchSuggestions(val);
      }, 140);
    });

    input.addEventListener("keydown", function (e) {
      if (list.hidden || !items.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        var next = activeIndex + 1 >= items.length ? 0 : activeIndex + 1;
        setActiveIndex(next);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        var prev = activeIndex - 1 < 0 ? items.length - 1 : activeIndex - 1;
        setActiveIndex(prev);
      } else if (e.key === "Enter" && activeIndex >= 0 && items[activeIndex]) {
        e.preventDefault();
        selectSuggestion(items[activeIndex].ckey);
      } else if (e.key === "Escape") {
        e.preventDefault();
        closeDropdown();
      }
    });

    input.addEventListener("blur", function () {
      window.setTimeout(closeDropdown, 150);
    });

    form.addEventListener("submit", function () {
      if (debounceTimer) window.clearTimeout(debounceTimer);
      if (activeController) {
        activeController.abort();
        activeController = null;
      }
      closeDropdown();
    });
  }

  function initServerActivityChart() {
    var stages = document.querySelectorAll(".sparkline-stage[data-chart-points]");
    stages.forEach(function (stage) {
      if (stage.dataset.chartBound) return;
      stage.dataset.chartBound = "1";

      var points = [];
      try {
        points = JSON.parse(stage.dataset.chartPoints || "[]");
      } catch (err) {
        return;
      }
      if (!Array.isArray(points) || !points.length) return;

      var svg = stage.querySelector(".sparkline-svg");
      var hoverLine = stage.querySelector(".sparkline-hover-line");
      var marker = stage.querySelector(".sparkline-hover-marker");
      var tooltip = stage.querySelector(".sparkline-tooltip");
      if (!svg || !tooltip) return;

      function findNearestPoint(svgX) {
        var lo = 0;
        var hi = points.length - 1;
        while (lo < hi) {
          var mid = (lo + hi) >> 1;
          if (points[mid].x < svgX) {
            lo = mid + 1;
          } else {
            hi = mid;
          }
        }
        if (lo > 0 && Math.abs(points[lo - 1].x - svgX) < Math.abs(points[lo].x - svgX)) {
          return points[lo - 1];
        }
        return points[lo];
      }

      function hideHover() {
        if (hoverLine) hoverLine.setAttribute("hidden", "");
        if (marker) marker.hidden = true;
        tooltip.hidden = true;
      }

      function renderTooltip(pt) {
        tooltip.textContent = "";

        var valLine = document.createElement("div");
        valLine.className = "sparkline-tooltip-val";
        var countNum = Number(pt.c);
        valLine.textContent = countNum + (countNum === 1 ? " player" : " players");
        tooltip.appendChild(valLine);

        var timeLine = document.createElement("div");
        timeLine.className = "sparkline-tooltip-time";
        timeLine.textContent = (pt.t || "") + " UTC";
        tooltip.appendChild(timeLine);

        var hasRange = pt.n && pt.n > 1 && pt.min !== pt.max;
        if (hasRange || pt.m) {
          var subLine = document.createElement("div");
          subLine.className = "sparkline-tooltip-sub";
          var parts = [];
          if (hasRange) {
            parts.push("Range " + pt.min + "\u2013" + pt.max + " (avg " + pt.avg + ")");
          }
          if (pt.m) {
            parts.push(pt.m);
          }
          subLine.textContent = parts.join(" \u00b7 ");
          tooltip.appendChild(subLine);
        }
      }

      function updateAtClientX(clientX) {
        var rect = svg.getBoundingClientRect();
        if (!rect.width) return;
        var relX = Math.max(0, Math.min(rect.width, clientX - rect.left));
        var svgX = (relX / rect.width) * 600.0;
        var pt = findNearestPoint(svgX);
        if (!pt) return;

        if (hoverLine) {
          hoverLine.setAttribute("x1", String(pt.x));
          hoverLine.setAttribute("x2", String(pt.x));
          hoverLine.removeAttribute("hidden");
        }

        var leftPct = (pt.x / 600.0) * 100.0;
        var topPct = (pt.y / 120.0) * 100.0;
        if (marker) {
          marker.style.left = leftPct.toFixed(2) + "%";
          marker.style.top = topPct.toFixed(2) + "%";
          marker.hidden = false;
        }

        renderTooltip(pt);
        tooltip.hidden = false;

        var pxX = (pt.x / 600.0) * rect.width;
        var tipWidth = tooltip.offsetWidth || 140;
        var offset = 10;
        var leftPx = pxX + offset;
        if (leftPx + tipWidth > rect.width - 4) {
          leftPx = Math.max(4, pxX - tipWidth - offset);
        }
        tooltip.style.left = Math.round(leftPx) + "px";
      }

      stage.addEventListener("pointermove", function (ev) {
        updateAtClientX(ev.clientX);
      });

      stage.addEventListener("pointerdown", function (ev) {
        updateAtClientX(ev.clientX);
      });

      stage.addEventListener("pointerleave", hideHover);
      stage.addEventListener("pointercancel", hideHover);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initDirectoryControls();
    initRosterFilter();
    initCopyButtons();
    initPlayerTypeahead();
    initServerActivityChart();
  });

  document.addEventListener("htmx:afterSettle", function () {
    initServerActivityChart();
  });

  document.addEventListener("htmx:historyRestore", function () {
    initDirectoryControls();
    initRosterFilter();
    initPlayerTypeahead();
    initServerActivityChart();
  });
})();
