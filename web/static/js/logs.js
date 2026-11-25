/* Hilleye // SierraBay12 Log Viewer client script (CSP-safe external script) */
(function () {
  "use strict";

  function buildCleanFilterParams(filterForm) {
    var raw = new URLSearchParams(new FormData(filterForm));
    var clean = new URLSearchParams();
    raw.forEach(function (val, key) {
      if (key === "view" || key === "page") return;
      clean.append(key, val);
    });
    return clean;
  }

  function syncHeaderActionLinks(filterForm, query) {
    if (!filterForm) return;
    var cleanStr = query || buildCleanFilterParams(filterForm).toString();
    var streamLink = document.getElementById("logs-link-stream");
    if (streamLink && streamLink.dataset.baseHref) {
      var sFocus = streamLink.dataset.focusLine || "";
      var sHref = streamLink.dataset.baseHref + "?" + cleanStr + "&view=stream";
      if (sFocus) {
        sHref += "&focus=" + encodeURIComponent(sFocus) + "#L" + encodeURIComponent(sFocus);
      }
      streamLink.setAttribute("href", sHref);
    }

    var mapLink = document.getElementById("logs-link-map");
    if (mapLink && mapLink.dataset.baseHref) {
      var mHref = mapLink.dataset.baseHref + "?" + cleanStr + "&view=map";
      var mFocus = mapLink.dataset.focusLine || "";
      var mz = mapLink.dataset.targetZ || "";
      var mx = mapLink.dataset.targetX || "";
      var my = mapLink.dataset.targetY || "";
      if (mFocus) mHref += "&focus=" + encodeURIComponent(mFocus);
      if (mz) mHref += "&z=" + encodeURIComponent(mz);
      if (mx) mHref += "&tx=" + encodeURIComponent(mx);
      if (my) mHref += "&ty=" + encodeURIComponent(my);
      mapLink.setAttribute("href", mHref);
    }

    var exportLink = document.getElementById("logs-link-export");
    if (exportLink && exportLink.dataset.baseHref) {
      exportLink.setAttribute("href", exportLink.dataset.baseHref + "?mode=filtered&" + cleanStr);
    }

    var rawLink = document.getElementById("logs-link-raw");
    if (rawLink && rawLink.dataset.baseHref) {
      var rawFile = rawLink.dataset.selectedFile || "game.log";
      var maskCb = filterForm.querySelector('input[name="mask_ips"]');
      var maskFlag = maskCb && maskCb.checked ? "1" : "0";
      rawLink.setAttribute(
        "href",
        rawLink.dataset.baseHref + "?file=" + encodeURIComponent(rawFile) + "&mode=raw&mask_ips=" + maskFlag
      );
    }
  }

  function syncToolbarActiveStates(filterForm) {
    if (!filterForm) return;
    var presetField = document.getElementById("logs-preset-input");
    var anyCatChecked = false;
    filterForm.querySelectorAll('input[name="cat"]').forEach(function (cb) {
      if (cb.checked) anyCatChecked = true;
    });

    var activePreset = presetField ? presetField.value : "";
    var activePresetBtn = null;
    document.querySelectorAll("[data-preset-id]").forEach(function (btn) {
      var isActive = !anyCatChecked && btn.dataset.presetId === activePreset;
      btn.classList.toggle("is-active", isActive);
      if (btn.dataset.presetId === activePreset) {
        activePresetBtn = btn;
      }
    });

    var includeSet = {};
    var excludeSet = {};
    var hasInclude = false;
    var hasExclude = false;
    if (activePresetBtn) {
      var incRaw = activePresetBtn.dataset.presetInclude || "";
      var excRaw = activePresetBtn.dataset.presetExclude || "";
      if (incRaw) {
        hasInclude = true;
        incRaw.split(",").forEach(function (k) {
          if (k) includeSet[k] = true;
        });
      }
      if (excRaw) {
        hasExclude = true;
        excRaw.split(",").forEach(function (k) {
          if (k) excludeSet[k] = true;
        });
      }
    }

    filterForm.querySelectorAll('input[name="cat"]').forEach(function (cb) {
      var chip = cb.closest(".logs-cat-chip");
      if (!chip) return;
      chip.classList.toggle("is-selected", cb.checked);
      var catName = (cb.value || "").toUpperCase();
      var included = true;
      if (anyCatChecked) {
        included = cb.checked;
      } else if (hasInclude) {
        included = Boolean(includeSet[catName]);
      } else if (hasExclude) {
        included = !excludeSet[catName];
      }
      chip.classList.toggle("is-excluded", !included);
    });

    var htmlInput = document.getElementById("logs-html-mode");
    var rawHtmlCb = document.getElementById("logs-raw-html-cb");
    if (htmlInput && rawHtmlCb) {
      htmlInput.value = rawHtmlCb.checked ? "0" : "1";
    }

    var ckeyInput = filterForm.elements.namedItem("ckey");
    var radiusSelect = filterForm.elements.namedItem("radius");
    if (ckeyInput && radiusSelect) {
      var ckey = ckeyInput.value;
      radiusSelect.disabled = ckey.length > 128 || !/[a-z0-9]/.test(ckey.toLowerCase());
      if (radiusSelect.disabled) radiusSelect.value = "0";
    }

    var resetBtn = document.getElementById("logs-filter-reset");
    if (resetBtn) {
      var qVal = (filterForm.querySelector('input[name="q"]') || {}).value || "";
      var ckVal = (filterForm.querySelector('input[name="ckey"]') || {}).value || "";
      var radVal = Number((filterForm.querySelector('select[name="radius"]') || {}).value || 0);
      var tfVal = (filterForm.querySelector('input[name="time_from"]') || {}).value || "";
      var ttVal = (filterForm.querySelector('input[name="time_to"]') || {}).value || "";
      var rxCb = filterForm.querySelector('input[name="regex"]');
      var mkCb = filterForm.querySelector('input[name="mask_ips"]');
      var advCount =
        (radVal > 0 ? 1 : 0) +
        (tfVal.trim() ? 1 : 0) +
        (ttVal.trim() ? 1 : 0) +
        (rxCb && rxCb.checked ? 1 : 0) +
        (mkCb && mkCb.checked ? 1 : 0) +
        (rawHtmlCb && rawHtmlCb.checked ? 1 : 0) +
        filterForm.querySelectorAll('input[name="cat"]:checked').length;

      var drawerBadge = document.getElementById("logs-filter-drawer-badge");
      if (drawerBadge) {
        drawerBadge.textContent = String(advCount);
        drawerBadge.hidden = advCount === 0;
      }

      var hasActiveFilters =
        anyCatChecked ||
        (activePreset && activePreset !== "gameplay") ||
        Boolean(qVal.trim()) ||
        Boolean(ckVal.trim()) ||
        advCount > 0;
      resetBtn.hidden = !hasActiveFilters;
    }
  }

  var liveSearchTimer;
  var streamRequest;
  var streamLocation;
  var streamObserver;

  function observeStreamEnd(container) {
    if (!streamObserver) return;
    streamObserver.disconnect();
    var end = container.querySelector('[data-stream-boundary="next"]');
    if (end) streamObserver.observe(end);
  }

  function insertStreamRows(container, boundary, fragment, results, url) {
    var direction = boundary.dataset.streamBoundary;
    var nextBoundary = fragment.querySelector('[data-stream-boundary="' + direction + '"]');
    var incoming = fragment.querySelector("[data-stream-rows]");
    var current = container.querySelector("[data-stream-results]");
    var expectedPage = new URL(url).searchParams.get("page");
    if (!incoming || results.dataset.page !== expectedPage || results.dataset.query !== current.dataset.query) {
      throw new Error("Лог изменился. Обновите страницу, чтобы продолжить чтение.");
    }

    Array.from(incoming.children).forEach(function (row) {
      if (document.getElementById(row.id)) row.remove();
    });
    var anchor = direction === "previous" ? boundary.nextElementSibling : null;
    var oldTop = anchor && anchor.getBoundingClientRect().top;
    var hadFocus = boundary.contains(document.activeElement);
    boundary.replaceWith(incoming);
    if (nextBoundary) incoming.insertAdjacentElement(direction === "next" ? "afterend" : "beforebegin", nextBoundary);
    if (anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - oldTop);
    if (hadFocus) {
      var focusLink = (nextBoundary || incoming).querySelector("a");
      if (focusLink) focusLink.focus({ preventScroll: true });
    }
    current.querySelector("[data-stream-count]").textContent =
      results.querySelector("[data-stream-count]").textContent;
    bindStreamHelpers(incoming);
  }

  function applyStreamFilters(form, results) {
    var params = new URLSearchParams(results.dataset.query);
    Array.from(form.elements).forEach(function (field) {
      var name = field.name;
      if (!name || name === "view" || name === "page") return;
      if (field.type === "checkbox") {
        field.checked = params.getAll(name).includes(field.value);
      } else {
        field.value = params.get(name) || (name === "radius" ? "0" : "");
      }
    });
    document.getElementById("logs-raw-html-cb").checked = params.get("html") === "0";
    var invalidTimeFields = results.dataset.invalidTimeFields.split(" ");
    ["time_from", "time_to"].forEach(function (name) {
      var field = form.elements.namedItem(name);
      var invalid = invalidTimeFields.includes(name);
      field.setAttribute("aria-invalid", String(invalid));
      document.getElementById(field.getAttribute("aria-errormessage")).hidden = !invalid;
    });
    if (results.dataset.invalidTimeFields && document.getElementById("logs-filter-drawer").hidden) {
      document.getElementById("logs-filter-drawer-toggle").click();
    }
    var fileLineCount = document.getElementById("logs-open-file-line-count");
    if (fileLineCount) fileLineCount.textContent = results.dataset.totalLines;

    var streamLink = document.getElementById("logs-link-stream");
    var mapLink = document.getElementById("logs-link-map");
    streamLink.dataset.focusLine = "";
    ["focusLine", "targetX", "targetY", "targetZ"].forEach(function (key) {
      mapLink.dataset[key] = "";
    });
    syncHeaderActionLinks(form, results.dataset.query);
  }

  async function loadStreamPage(url, form, container, boundary) {
    if (boundary && (streamRequest || liveSearchTimer || !container.contains(boundary))) return;
    clearTimeout(liveSearchTimer);
    liveSearchTimer = null;
    if (streamRequest && streamRequest.url === url && !streamRequest.controller.signal.aborted) return;
    if (streamRequest) streamRequest.controller.abort();
    var controller = new AbortController();
    var request = { url: url, controller: controller };
    streamRequest = request;
    var errorBox = document.getElementById("logs-stream-request-error");
    errorBox.hidden = true;
    container.setAttribute("aria-busy", "true");
    var updated = false;
    try {
      var response = await fetch(url, {
        headers: { "HX-Request": "true" },
        signal: controller.signal,
      });
      var fragment = document.createElement("template");
      fragment.innerHTML = await response.text();
      if (streamRequest !== request || controller.signal.aborted) return;
      if (response.redirected || response.status === 401 || response.status === 403) {
        window.location.assign(url);
        return;
      }
      if (!response.ok) {
        var alert = fragment.content.querySelector("[role=alert]");
        throw new Error(alert ? alert.textContent : "");
      }
      var results = fragment.content.querySelector("[data-stream-results]");
      if (!results) throw new Error();
      if (boundary) {
        insertStreamRows(container, boundary, fragment.content, results, url);
        updateStreamCategoryCounts(form, results);
        updated = true;
        return;
      }

      var next = new URL(url, window.location.href);
      next.search = results.dataset.query + "&page=" + results.dataset.page;
      var nextLocation = next.pathname + next.search;
      if (nextLocation !== streamLocation || window.location.hash) {
        window.history.pushState(null, "", nextLocation);
        streamLocation = nextLocation;
      }
      applyStreamFilters(form, results);
      updateStreamCategoryCounts(form, results);
      container.replaceChildren(fragment.content);
      bindStreamHelpers();
      window.scrollTo(0, 0);
      updated = true;
    } catch (error) {
      if (streamRequest !== request || controller.signal.aborted) return;
      errorBox.textContent = error.message || "Не удалось обновить ленту. Повторите попытку.";
      errorBox.hidden = false;
    } finally {
      if (streamRequest === request) {
        streamRequest = null;
        container.removeAttribute("aria-busy");
        if (updated) observeStreamEnd(container);
      }
    }
  }

  function updateStreamCategoryCounts(form, results) {
    var counts = JSON.parse(results.dataset.categoryCounts);
    form.querySelectorAll("[data-cat-toggle]").forEach(function (chip) {
      chip.querySelector("[data-category-count]").textContent = counts[chip.dataset.catToggle] || 0;
    });
  }

  function initStreamNavigation() {
    var container = document.getElementById("logs-stream-container");
    if (!container) return;
    var form = document.getElementById("logs-filter-form");
    streamLocation = window.location.pathname + window.location.search;
    container.addEventListener("click", function (ev) {
      var link = ev.target.closest("[data-stream-boundary] a");
      if (!link || ev.defaultPrevented || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      loadStreamPage(link.href, form, container, link.parentElement);
    });
    if ("IntersectionObserver" in window) {
      streamObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            loadStreamPage(entry.target.querySelector("a").href, form, container, entry.target);
          }
        });
      }, { rootMargin: "0px 0px 350px 0px" });
      observeStreamEnd(container);
    }
    window.addEventListener("popstate", function () {
      if (window.location.pathname + window.location.search !== streamLocation) {
        clearTimeout(liveSearchTimer);
        if (streamRequest) streamRequest.controller.abort();
        window.location.reload();
      }
    });
  }

  function submitFilterForm(filterForm) {
    if (!filterForm) return;
    clearTimeout(liveSearchTimer);
    syncToolbarActiveStates(filterForm);

    var streamContainer = document.getElementById("logs-stream-container");
    if (streamContainer) {
      var action = new URL(filterForm.action);
      action.search = new URLSearchParams(new FormData(filterForm));
      loadStreamPage(action.href, filterForm, streamContainer);
      return;
    }

    syncHeaderActionLinks(filterForm);
    if (typeof reloadLogMap === "function") {
      var mapParams = new URLSearchParams(new FormData(filterForm));
      var mapAction = filterForm.getAttribute("action") || window.location.pathname;
      var mapUrl = mapAction + "?" + mapParams.toString();
      window.history.replaceState({}, "", mapUrl);
      reloadLogMap(filterForm, buildCleanFilterParams(filterForm).toString());
      return;
    }

    filterForm.submit();
  }

  function initRoundFilterHelpers(root) {
    root = root || document;
    var filterForm = document.getElementById("logs-filter-form");
    var ckeyInput = document.getElementById("logs-ckey-input");
    var presetField = document.getElementById("logs-preset-input");

    if (filterForm) {
      syncToolbarActiveStates(filterForm);
      if (!document.getElementById("logs-stream-container")) syncHeaderActionLinks(filterForm);
    }

    if (filterForm && !filterForm.dataset.htmxBound) {
      filterForm.dataset.htmxBound = "1";
      filterForm.addEventListener("submit", function (ev) {
        var streamContainer = document.getElementById("logs-stream-container");
        var isAjaxStream = Boolean(streamContainer);
        var isAjaxMap = typeof reloadLogMap === "function";
        if (isAjaxStream || isAjaxMap) {
          ev.preventDefault();
          var pageInput = filterForm.querySelector('input[name="page"]');
          if (pageInput) pageInput.value = "1";
          submitFilterForm(filterForm);
        }
      });

      filterForm.querySelectorAll('select, input[name="regex"], input[name="mask_ips"], #logs-raw-html-cb, input[name="time_from"], input[name="time_to"]').forEach(function (ctrl) {
        if (ctrl.dataset.autoBound) return;
        ctrl.dataset.autoBound = "1";
        ctrl.addEventListener("change", function () {
          var pageInput = filterForm.querySelector('input[name="page"]');
          if (pageInput) pageInput.value = "1";
          submitFilterForm(filterForm);
        });
      });

      var drawerToggle = document.getElementById("logs-filter-drawer-toggle");
      var drawerEl = document.getElementById("logs-filter-drawer");
      if (drawerToggle && drawerEl && !drawerToggle.dataset.bound) {
        drawerToggle.dataset.bound = "1";
        drawerToggle.addEventListener("click", function () {
          var willOpen = drawerEl.hidden;
          drawerEl.hidden = !willOpen;
          drawerToggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
          drawerToggle.classList.toggle("is-active", willOpen);
        });
      }

      filterForm.querySelectorAll('input[type="text"]').forEach(function (inp) {
        if (inp.dataset.resetBound) return;
        inp.dataset.resetBound = "1";
        inp.addEventListener("input", function () {
          syncToolbarActiveStates(filterForm);
          if (!document.getElementById("logs-stream-container")) return;
          if (streamRequest) streamRequest.controller.abort();
          if (inp.id === "logs-q-input" || inp.id === "logs-ckey-input") {
            clearTimeout(liveSearchTimer);
            liveSearchTimer = setTimeout(function () {
              var pageInput = filterForm.querySelector('input[name="page"]');
              if (pageInput) pageInput.value = "1";
              submitFilterForm(filterForm);
            }, 320);
          }
        });
      });
    }

    // Preset buttons
    root.querySelectorAll("[data-preset-id]").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        if (!filterForm || !presetField) return;
        presetField.value = btn.dataset.presetId || "gameplay";
        // Clear explicit category checkboxes when switching preset
        filterForm.querySelectorAll('input[name="cat"]').forEach(function (cb) {
          cb.checked = false;
        });
        var pageInput = filterForm.querySelector('input[name="page"]');
        if (pageInput) pageInput.value = "1";
        submitFilterForm(filterForm);
      });
    });

    // Category chip toggle (native <label> toggles checkbox once; submit inside round viewer, toggle class outside)
    root.querySelectorAll('.logs-cat-chip input[name="cat"]').forEach(function (cb) {
      if (cb.dataset.bound) return;
      cb.dataset.bound = "1";
      cb.addEventListener("change", function () {
        var chip = cb.closest(".logs-cat-chip");
        if (chip) {
          chip.classList.toggle("is-selected", cb.checked);
        }
        if (filterForm && filterForm.contains(cb)) {
          var pageInput = filterForm.querySelector('input[name="page"]');
          if (pageInput) pageInput.value = "1";
          submitFilterForm(filterForm);
        }
      });
    });

    // Clickable ckey filter inside log lines
    root.querySelectorAll(".log-ckey-btn[data-ckey]").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        if (!filterForm || !ckeyInput) return;
        ckeyInput.value = btn.dataset.ckey || "";
        var pageInput = filterForm.querySelector('input[name="page"]');
        if (pageInput) pageInput.value = "1";
        submitFilterForm(filterForm);
      });
    });

    // Clear active ckey pill button
    root.querySelectorAll("[data-clear-ckey]").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        if (!filterForm || !ckeyInput) return;
        ckeyInput.value = "";
        var radSelect = document.getElementById("logs-radius-select");
        if (radSelect) radSelect.value = "0";
        var pageInput = filterForm.querySelector('input[name="page"]');
        if (pageInput) pageInput.value = "1";
        submitFilterForm(filterForm);
      });
    });
  }

  function closeAllInlineContexts() {
    document.querySelectorAll(".log-inline-context").forEach(function (el) {
      el.remove();
    });
    document.querySelectorAll(".log-row.has-context-open").forEach(function (row) {
      row.classList.remove("has-context-open");
    });
  }

  function toggleInlineContext(row, url, lineNo) {
    if (!row || !url) return;
    var existing = row.nextElementSibling;
    if (existing && existing.classList.contains("log-inline-context")) {
      existing.remove();
      row.classList.remove("has-context-open");
      return;
    }

    var drawer = document.createElement("div");
    drawer.className = "log-inline-context";

    var loadingBar = document.createElement("div");
    loadingBar.className = "logs-context-bar";
    var loadingText = document.createElement("span");
    loadingText.className = "mono-val cell-sub";
    loadingText.textContent = "Загрузка контекста" + (lineNo ? " #L" + lineNo : "") + "...";
    var loadingClose = document.createElement("button");
    loadingClose.type = "button";
    loadingClose.className = "log-mini-btn";
    loadingClose.textContent = "Скрыть";
    loadingClose.addEventListener("click", function () {
      drawer.remove();
      row.classList.remove("has-context-open");
    });
    loadingBar.appendChild(loadingText);
    loadingBar.appendChild(loadingClose);
    drawer.appendChild(loadingBar);

    row.classList.add("has-context-open");
    row.insertAdjacentElement("afterend", drawer);

    var fetchUrl = url.indexOf("inline=") === -1
      ? url + (url.indexOf("?") === -1 ? "?" : "&") + "inline=1"
      : url;
    fetch(fetchUrl, { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (html) {
        drawer.innerHTML = html;
        initCopyButtons();
        var closeBtn = drawer.querySelector(".js-close-inline-context");
        if (closeBtn) {
          closeBtn.addEventListener("click", function () {
            drawer.remove();
            row.classList.remove("has-context-open");
          });
        }
      })
      .catch(function () {
        drawer.innerHTML = "";
        var errWrap = document.createElement("div");
        errWrap.className = "logs-context-bar";
        var errMsg = document.createElement("span");
        errMsg.className = "logs-alert-error";
        errMsg.textContent = "Не удалось загрузить контекст.";
        var errClose = document.createElement("button");
        errClose.type = "button";
        errClose.className = "log-mini-btn";
        errClose.textContent = "Закрыть";
        errClose.addEventListener("click", function () {
          drawer.remove();
          row.classList.remove("has-context-open");
        });
        errWrap.appendChild(errMsg);
        errWrap.appendChild(errClose);
        drawer.appendChild(errWrap);
      });
  }

  var contextRequest = null;

  function closeLogContextModal() {
    if (contextRequest) contextRequest.abort();
    contextRequest = null;
    var modal = document.getElementById("log-context-modal");
    var modalBody = document.getElementById("log-context-modal-body");
    if (modal) modal.hidden = true;
    if (modalBody) modalBody.innerHTML = "";
  }

  function openLogContextModal(url, lineNo) {
    var modal = document.getElementById("log-context-modal");
    var modalBody = document.getElementById("log-context-modal-body");
    var modalTitle = document.getElementById("log-context-modal-title");
    if (!modal || !modalBody || !url) return;
    if (modalTitle) {
      modalTitle.textContent = "Контекст строки #L" + (lineNo || "");
    }
    modalBody.innerHTML = '<div class="logs-panel-body cell-sub">Загрузка контекста...</div>';
    modal.hidden = false;
    if (contextRequest) contextRequest.abort();
    var controller = new AbortController();
    contextRequest = controller;
    fetch(url, { credentials: "same-origin", signal: controller.signal })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (html) {
        if (contextRequest === controller) modalBody.innerHTML = html;
      })
      .catch(function () {
        if (contextRequest === controller) {
          modalBody.innerHTML = '<div class="logs-panel-body logs-alert-error">Не удалось загрузить контекст.</div>';
        }
      });
  }

  function initContextModal(root) {
    var modal = document.getElementById("log-context-modal");
    var closeBtn = document.getElementById("log-context-modal-close");

    if (modal && modalBody) {
      if (closeBtn && !closeBtn.dataset.bound) {
        closeBtn.dataset.bound = "1";
        closeBtn.addEventListener("click", closeLogContextModal);
      }

      if (!modal.dataset.bound) {
        modal.dataset.bound = "1";
        modal.addEventListener("click", function (ev) {
          if (ev.target === modal) closeLogContextModal();
        });
      }
    }

    (root || document).querySelectorAll("[data-context-url]").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        var row = btn.closest(".log-row");
        if (row) {
          toggleInlineContext(row, btn.dataset.contextUrl, btn.dataset.lineNo || "");
        } else {
          openLogContextModal(btn.dataset.contextUrl, btn.dataset.lineNo || "");
        }
      });
    });
  }

  function initKeyboardShortcuts() {
    if (document.body.dataset.logsKeysBound) return;
    document.body.dataset.logsKeysBound = "1";
    document.addEventListener("keydown", function (ev) {
      var active = document.activeElement;
      var tag = active ? active.tagName.toLowerCase() : "";
      var isTyping = tag === "input" || tag === "textarea" || tag === "select" || (active && active.isContentEditable);

      if (ev.key === "/" && !isTyping && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        var searchInput =
          document.getElementById("logs-q-input") ||
          document.getElementById("gsearch-q") ||
          document.getElementById("quick-search-q") ||
          document.getElementById("qdel-q") ||
          document.getElementById("admin-audit-q");
        if (searchInput) {
          ev.preventDefault();
          searchInput.focus();
          searchInput.select();
        }
        return;
      }

      if ((ev.key === "f" || ev.key === "F" || ev.key === "\u0430" || ev.key === "\u0410") && !isTyping && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        var drawerBtn = document.getElementById("logs-filter-drawer-toggle");
        if (drawerBtn) {
          ev.preventDefault();
          drawerBtn.click();
          return;
        }
      }

      if (ev.key === "Escape") {
        var modal = document.getElementById("log-context-modal");
        if (modal && !modal.hidden) {
          closeLogContextModal();
          return;
        }
        if (document.querySelector(".log-inline-context")) {
          closeAllInlineContexts();
          return;
        }
        if (isTyping && active) {
          active.blur();
        }
      }
    });
  }

  function initCopyButtons(root) {
    (root || document).querySelectorAll("[data-copy-text]").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        var raw = btn.dataset.copyText || "";
        var text = raw.charAt(0) === "/" ? window.location.origin + raw : raw;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            var old = btn.textContent;
            btn.textContent = "Скопировано";
            setTimeout(function () {
              btn.textContent = old;
            }, 1400);
          });
        }
      });
    });
  }

  function scrollFocusedLine() {
    var focused = document.querySelector(".log-row.is-focused");
    if (focused) {
      focused.scrollIntoView({ block: "center" });
    }
  }

  function initLogsBootOverlay() {
    var overlay = document.getElementById("logs-boot-overlay");
    if (!overlay) return;

    var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion) {
      overlay.remove();
      return;
    }

    var storageKey = "hilleye_logs_boot_v1";
    try {
      if (window.sessionStorage.getItem(storageKey) === "1") {
        overlay.remove();
        return;
      }
      window.sessionStorage.setItem(storageKey, "1");
    } catch (_err) {
      overlay.remove();
      return;
    }

    overlay.hidden = false;
    var closed = false;
    var timer = null;

    function dismiss() {
      if (closed) return;
      closed = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("keydown", onKey);
      overlay.classList.add("is-leaving");
      setTimeout(function () {
        overlay.remove();
      }, 260);
    }

    function onKey(ev) {
      if (ev.key === "Escape" || ev.key === "Enter" || ev.key === " ") {
        dismiss();
      }
    }

    overlay.addEventListener("click", dismiss);
    document.addEventListener("keydown", onKey);
    timer = setTimeout(dismiss, 900);
  }

  function bindStreamHelpers(root) {
    initRoundFilterHelpers(root);
    initContextModal(root);
    initCopyButtons(root);
    if (!root) scrollFocusedLine();
  }

  var reloadLogMap = null;

  function initAll() {
    initLogsBootOverlay();
    if (window.HilleyeLogCatalog) window.HilleyeLogCatalog.init();
    if (window.HilleyeLogMap) {
      reloadLogMap = window.HilleyeLogMap.init(buildCleanFilterParams, submitFilterForm, openLogContextModal);
    }
    initStreamNavigation();
    bindStreamHelpers();
    initKeyboardShortcuts();
    if (window.HilleyeLogAdmin) window.HilleyeLogAdmin.init(initCopyButtons);
  }

  document.addEventListener("DOMContentLoaded", initAll);
})();
