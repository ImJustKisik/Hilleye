/* Hilleye // SierraBay12 Log Viewer client script (CSP-safe external script) */
(function () {
  "use strict";

  function initDragAndDropUpload() {
    var dropzone = document.getElementById("logs-dropzone");
    var fileInput = document.getElementById("logs-upload-file");
    var form = document.getElementById("logs-upload-form");
    var statusBox = document.getElementById("logs-upload-status");
    var dzTitle = document.getElementById("logs-dropzone-title");
    var dzSub = document.getElementById("logs-dropzone-sub");
    if (!dropzone || !fileInput || !form || dropzone.dataset.bound) return;
    dropzone.dataset.bound = "1";

    var defaultTitle = dzTitle ? dzTitle.textContent : "Перетащите файл или нажмите для выбора";
    var defaultSub = dzSub ? dzSub.textContent : "game.log, qdel.log, .txt, .zip, .tar.gz";

    function formatFileSize(bytes) {
      if (!bytes || bytes < 1024) return (bytes || 0) + " Б";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " КБ";
      return (bytes / (1024 * 1024)).toFixed(2) + " МБ";
    }

    function updateSelectedFilePreview() {
      var f = fileInput.files && fileInput.files[0];
      if (!f) {
        if (dzTitle) dzTitle.textContent = defaultTitle;
        if (dzSub) dzSub.textContent = defaultSub;
        return;
      }
      if (dzTitle) dzTitle.textContent = f.name;
      if (dzSub) dzSub.textContent = "Готово к загрузке (" + formatFileSize(f.size) + ")";
    }

    function showStatus(msg, isError) {
      if (!statusBox) return;
      statusBox.hidden = false;
      statusBox.className = "logs-alert " + (isError ? "logs-alert-error" : "logs-alert-info");
      statusBox.textContent = msg;
    }

    dropzone.addEventListener("click", function (ev) {
      if (ev.target !== fileInput) {
        fileInput.click();
      }
    });

    dropzone.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        fileInput.click();
      }
    });

    fileInput.addEventListener("change", updateSelectedFilePreview);

    dropzone.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      dropzone.classList.add("is-dragover");
    });

    dropzone.addEventListener("dragleave", function () {
      dropzone.classList.remove("is-dragover");
    });

    dropzone.addEventListener("drop", function (ev) {
      ev.preventDefault();
      dropzone.classList.remove("is-dragover");
      if (ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files.length > 0) {
        fileInput.files = ev.dataTransfer.files;
        updateSelectedFilePreview();
        uploadSelectedFile();
      }
    });

    function uploadSelectedFile() {
      if (!fileInput.files || fileInput.files.length === 0) {
        showStatus("Выберите файл .log, .txt, .zip или .tar.gz.", true);
        return;
      }
      var maxUploadMb = Number(form.dataset.maxUploadMb);
      if (maxUploadMb && fileInput.files[0].size > maxUploadMb * 1024 * 1024) {
        showStatus("Файл превышает максимальный размер (" + maxUploadMb + " МБ).", true);
        return;
      }
      var data = new FormData(form);
      var csrfInput = form.querySelector('input[name="csrf_token"]');
      var headers = { "X-Requested-With": "fetch" };
      if (csrfInput && csrfInput.value) {
        headers["X-CSRF-Token"] = csrfInput.value;
      }
      showStatus("Загрузка и индексация файла...", false);
      fetch(form.action, {
        method: "POST",
        body: data,
        headers: headers,
        credentials: "same-origin"
      })
        .then(function (resp) {
          return resp.json().then(function (body) {
            return { ok: resp.ok, status: resp.status, body: body };
          }).catch(function () {
            return { ok: resp.ok, status: resp.status, body: {} };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            showStatus((res.body && res.body.detail) || ("Ошибка загрузки (HTTP " + res.status + ")"), true);
            return;
          }
          if (res.body && res.body.rounds && res.body.rounds.length === 1 && res.body.rounds[0].url) {
            window.location.href = res.body.rounds[0].url;
            return;
          }
          window.location.href = "/logs";
        })
        .catch(function () {
          showStatus("Сетевая ошибка при загрузке файла.", true);
        });
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      uploadSelectedFile();
    });
  }

  function buildCleanFilterParams(filterForm) {
    var raw = new URLSearchParams(new FormData(filterForm));
    var clean = new URLSearchParams();
    raw.forEach(function (val, key) {
      if (key === "view" || key === "page") return;
      clean.append(key, val);
    });
    return clean;
  }

  function syncHeaderActionLinks(filterForm) {
    if (!filterForm) return;
    var cleanStr = buildCleanFilterParams(filterForm).toString();
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

  function submitFilterForm(filterForm) {
    if (!filterForm) return;
    syncToolbarActiveStates(filterForm);
    syncHeaderActionLinks(filterForm);

    var streamContainer = document.getElementById("logs-stream-container");
    if (streamContainer && window.htmx && typeof window.htmx.ajax === "function") {
      var params = new URLSearchParams(new FormData(filterForm));
      var action = filterForm.getAttribute("action") || window.location.pathname;
      var url = action + "?" + params.toString();
      window.history.pushState({}, "", url);
      window.htmx.ajax("GET", url, { target: "#logs-stream-container", swap: "innerHTML" });
      return;
    }

    if (typeof window.__logsMapReload === "function") {
      var mapParams = new URLSearchParams(new FormData(filterForm));
      var mapAction = filterForm.getAttribute("action") || window.location.pathname;
      var mapUrl = mapAction + "?" + mapParams.toString();
      window.history.replaceState({}, "", mapUrl);
      window.__logsMapReload(filterForm, buildCleanFilterParams(filterForm).toString());
      return;
    }

    filterForm.submit();
  }

  function initRoundFilterHelpers() {
    var filterForm = document.getElementById("logs-filter-form");
    var ckeyInput = document.getElementById("logs-ckey-input");
    var presetField = document.getElementById("logs-preset-input");

    if (filterForm) {
      syncToolbarActiveStates(filterForm);
      syncHeaderActionLinks(filterForm);
    }

    if (filterForm && !filterForm.dataset.htmxBound) {
      filterForm.dataset.htmxBound = "1";
      filterForm.addEventListener("submit", function (ev) {
        var streamContainer = document.getElementById("logs-stream-container");
        var isAjaxStream = Boolean(streamContainer && window.htmx);
        var isAjaxMap = typeof window.__logsMapReload === "function";
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

      var liveSearchTimer = null;
      filterForm.querySelectorAll('input[type="text"]').forEach(function (inp) {
        if (inp.dataset.resetBound) return;
        inp.dataset.resetBound = "1";
        inp.addEventListener("input", function () {
          syncToolbarActiveStates(filterForm);
          if (
            (inp.id === "logs-q-input" || inp.id === "logs-ckey-input") &&
            document.getElementById("logs-stream-container") &&
            window.htmx
          ) {
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
    document.querySelectorAll("[data-preset-id]").forEach(function (btn) {
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
    document.querySelectorAll('.logs-cat-chip input[name="cat"]').forEach(function (cb) {
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
    document.querySelectorAll(".log-ckey-btn[data-ckey]").forEach(function (btn) {
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
    document.querySelectorAll("[data-clear-ckey]").forEach(function (btn) {
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
    fetch(url, { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (html) {
        modalBody.innerHTML = html;
      })
      .catch(function () {
        modalBody.innerHTML = '<div class="logs-panel-body logs-alert-error">Не удалось загрузить контекст.</div>';
      });
  }

  function initContextModal() {
    var modal = document.getElementById("log-context-modal");
    var modalBody = document.getElementById("log-context-modal-body");
    var closeBtn = document.getElementById("log-context-modal-close");

    function closeModal() {
      if (!modal || !modalBody) return;
      modal.hidden = true;
      modalBody.innerHTML = "";
    }

    if (modal && modalBody) {
      if (closeBtn && !closeBtn.dataset.bound) {
        closeBtn.dataset.bound = "1";
        closeBtn.addEventListener("click", closeModal);
      }

      if (!modal.dataset.bound) {
        modal.dataset.bound = "1";
        modal.addEventListener("click", function (ev) {
          if (ev.target === modal) closeModal();
        });
      }
    }

    document.querySelectorAll("[data-context-url]").forEach(function (btn) {
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
        var modalBody = document.getElementById("log-context-modal-body");
        if (modal && !modal.hidden) {
          modal.hidden = true;
          if (modalBody) modalBody.innerHTML = "";
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

  function initCopyButtons() {
    document.querySelectorAll("[data-copy-text]").forEach(function (btn) {
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

  function categoryPinColor(category) {
    var cat = (category || "").toUpperCase();
    if (cat === "ATTACK") return "#f87171";
    if (cat === "SAY" || cat === "WHISPER" || cat === "RADIO" || cat === "OOC") return "#34d399";
    if (cat === "ADMIN" || cat === "ADMINPM" || cat === "ADMINPRIVATE" || cat === "ASAY") return "#fbbf24";
    if (cat === "ACCESS") return "#60a5fa";
    if (cat === "GAME" || cat === "EMOTE" || cat === "EMOTE_MECH" || cat === "VOTE") return "#c084fc";
    return "#94a3b8";
  }

  function decodeRleAreas(payload) {
    var w = Number(payload && payload.width) || 200;
    var h = Number(payload && payload.height) || 200;
    var palette = Array.isArray(payload && payload.palette) ? payload.palette : [];
    var rle = Array.isArray(payload && payload.rle) ? payload.rle : [];
    var grid = new Uint16Array(w * h);
    var pos = 0;
    for (var i = 0; i < rle.length; i++) {
      var pair = rle[i];
      if (!Array.isArray(pair) || pair.length < 2) continue;
      var cnt = Number(pair[0]) || 0;
      var val = Number(pair[1]) || 0;
      var end = Math.min(grid.length, pos + cnt);
      for (var j = pos; j < end; j++) {
        grid[j] = val;
      }
      pos = end;
      if (pos >= grid.length) break;
    }
    return { width: w, height: h, palette: palette, grid: grid };
  }

  function normalizeCkeyClient(val) {
    return String(val || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  function initRoundMapViewer() {
    window.__logsMapReload = null;
    var root = document.getElementById("logs-map-root");
    var canvas = document.getElementById("logs-map-canvas");
    if (!root || !canvas || !canvas.getContext) return;

    var ctx = canvas.getContext("2d");
    var deckTabs = document.getElementById("logs-map-deck-tabs");
    var sectorSelect = document.getElementById("logs-map-sector-select");
    var statusEl = document.getElementById("logs-map-status");
    var errorEl = document.getElementById("logs-map-error");
    var hudCoords = document.getElementById("logs-map-hud-coords");
    var hudArea = document.getElementById("logs-map-hud-area");
    var zoomLabel = document.getElementById("logs-map-zoom-label");
    var modePinsBtn = document.getElementById("logs-map-mode-pins");
    var modeHeatBtn = document.getElementById("logs-map-mode-heat");
    var dimBtn = document.getElementById("logs-map-dim-btn");
    var clusterCb = document.getElementById("logs-map-cluster-cb");
    var trajCb = document.getElementById("logs-map-traj-cb");
    var zoomInBtn = document.getElementById("logs-map-zoom-in");
    var zoomOutBtn = document.getElementById("logs-map-zoom-out");
    var fitBtn = document.getElementById("logs-map-fit");
    var centerBtn = document.getElementById("logs-map-center-target");
    var showAllDeckBtn = document.getElementById("logs-map-show-all-deck");
    var inspectorTitle = document.getElementById("logs-map-inspector-title");
    var inspectorSub = document.getElementById("logs-map-inspector-sub");
    var inspectorList = document.getElementById("logs-map-inspector-list");
    var timelineEl = document.getElementById("logs-map-timeline");
    var timeStartInput = document.getElementById("logs-map-time-start");
    var timeEndInput = document.getElementById("logs-map-time-end");
    var timeReadout = document.getElementById("logs-map-time-readout");
    var timeFromLbl = document.getElementById("logs-map-time-from-lbl");
    var timeToLbl = document.getElementById("logs-map-time-to-lbl");
    var timeCount = document.getElementById("logs-map-time-count");
    var timeResetBtn = document.getElementById("logs-map-time-reset");

    var ds = root.dataset;
    var eventsUrl = ds.eventsUrl || "";
    var roundUid = ds.roundUid || "";
    var selectedFile = ds.selectedFile || "game.log";
    var baseQuery = ds.baseQuery || "";
    var maskIps = ds.maskIps === "1";
    var renderHtml = ds.renderHtml !== "0";
    var targetZ = ds.targetZ ? Number(ds.targetZ) : null;
    var targetX = ds.targetX ? Number(ds.targetX) : null;
    var targetY = ds.targetY ? Number(ds.targetY) : null;
    var focusLine = ds.focusLine ? Number(ds.focusLine) : null;
    var filterCkey = normalizeCkeyClient(ds.ckey || "");
    var defaultTilePx = Number(ds.tilePx) || 16;

    var TRACK_COLORS = ["#60a5fa", "#34d399", "#fbbf24", "#f472b6", "#c084fc", "#38bdf8"];

    var state = {
      manifest: null,
      levelsByZ: {},
      events: [],
      zCounts: {},
      currentZ: targetZ || 1,
      mode: "pins",
      dimMap: true,
      clusterEnabled: clusterCb ? clusterCb.checked : true,
      trajEnabled: trajCb ? trajCb.checked : Boolean(filterCkey),
      scale: 0.25,
      offsetX: 0,
      offsetY: 0,
      cssW: 900,
      cssH: 620,
      dpr: window.devicePixelRatio || 1,
      hoverTile: null,
      hoverPin: null,
      selectedLineNos: {},
      selectedTile: (targetX && targetY) ? { x: targetX, y: targetY, z: targetZ || 1 } : null,
      renderedPins: [],
      userMovedView: false,
      atTargetView: Boolean(targetX && targetY),
      dragging: false,
      dragMoved: false,
      dragStartX: 0,
      dragStartY: 0,
      dragOrigOffsetX: 0,
      dragOrigOffsetY: 0,
      imageCache: {},
      areaCache: {},
    };

    function getLevelMeta(z) {
      var lv = state.levelsByZ[String(z)];
      if (lv) return lv;
      return {
        z: z,
        label: "Z" + z,
        short_label: "Z" + z,
        width: 200,
        height: 200,
        tile_px: defaultTilePx,
        image_file: null,
        areas_file: null,
      };
    }

    function lookupArea(z, tx, ty) {
      var cached = state.areaCache[String(z)];
      if (cached && cached.ready && cached.data) {
        var d = cached.data;
        if (tx >= 1 && tx <= d.width && ty >= 1 && ty <= d.height) {
          var idx = d.grid[(ty - 1) * d.width + (tx - 1)];
          if (idx >= 0 && idx < d.palette.length) {
            return d.palette[idx];
          }
        }
        return "";
      }
      var list = state.events;
      for (var i = 0; i < list.length; i++) {
        var ev = list[i];
        if (ev.z === z && ev.x === tx && ev.y === ty && ev.area) {
          return ev.area;
        }
      }
      return z > 7 ? ("Сектор / Экспедиция (Z" + z + ")") : "";
    }

    function detectContentBounds(img, lv) {
      try {
        var w = Math.max(1, lv.width | 0);
        var h = Math.max(1, lv.height | 0);
        var probe = document.createElement("canvas");
        probe.width = w;
        probe.height = h;
        var pctx = probe.getContext("2d");
        if (!pctx) return null;
        pctx.imageSmoothingEnabled = false;
        pctx.drawImage(img, 0, 0, w, h);
        var data = pctx.getImageData(0, 0, w, h).data;
        var minX = w + 1;
        var maxX = 0;
        var minY = h + 1;
        var maxY = 0;
        for (var py = 0; py < h; py++) {
          var ty = h - py;
          var rowOff = py * w * 4;
          for (var px = 0; px < w; px++) {
            var off = rowOff + px * 4;
            var a = data[off + 3];
            if (a < 16) continue;
            var r = data[off];
            var g = data[off + 1];
            var b = data[off + 2];
            // Void background is #090d14 (9, 13, 20)
            if (r > 16 || g > 21 || b > 28) {
              var tx = px + 1;
              if (tx < minX) minX = tx;
              if (tx > maxX) maxX = tx;
              if (ty < minY) minY = ty;
              if (ty > maxY) maxY = ty;
            }
          }
        }
        if (minX <= maxX && minY <= maxY) {
          return { minX: minX, maxX: maxX, minY: minY, maxY: maxY };
        }
      } catch (_err) {
        return null;
      }
      return null;
    }

    function loadAssetsForZ(z) {
      var key = String(z);
      var lv = getLevelMeta(z);
      if (lv.image_file && !state.imageCache[key]) {
        var img = new Image();
        var rec = { status: "loading", img: img, bounds: null };
        state.imageCache[key] = rec;
        img.onload = function () {
          rec.status = "loaded";
          rec.bounds = detectContentBounds(img, lv);
          if (state.currentZ === z && !state.userMovedView && !(targetX && targetY && (!targetZ || targetZ === z))) {
            fitView();
          } else {
            requestDraw();
          }
        };
        img.onerror = function () {
          rec.status = "error";
          requestDraw();
        };
        img.src = "/logs/maps/z/" + z + ".png";
      }
      if (lv.areas_file && !state.areaCache[key]) {
        var arec = { ready: false, data: null };
        state.areaCache[key] = arec;
        fetch("/logs/maps/areas/" + z + ".json", { credentials: "same-origin" })
          .then(function (resp) {
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            return resp.json();
          })
          .then(function (payload) {
            arec.data = decodeRleAreas(payload);
            arec.ready = true;
            if (state.hoverTile && state.currentZ === z) {
              updateHud(state.hoverTile.x, state.hoverTile.y);
            }
          })
          .catch(function () {
            arec.ready = false;
          });
      }
    }

    function updateZoomLabel() {
      if (zoomLabel) {
        zoomLabel.textContent = Math.round(state.scale * 100) + "%";
      }
    }

    function updateCenterButton(isAtTargetNow) {
      if (typeof isAtTargetNow === "boolean") {
        state.atTargetView = isAtTargetNow;
      }
      if (!centerBtn) return;
      if (!targetX || !targetY) {
        centerBtn.hidden = true;
        return;
      }
      centerBtn.hidden = Boolean(state.atTargetView && (!targetZ || targetZ === state.currentZ));
    }

    function parseEventSeconds(ev) {
      var raw = String((ev && (ev.ts || ev.timestamp)) || "");
      var full = raw.match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})/);
      if (full) {
        return {
          hasDate: true,
          sec: Date.UTC(Number(full[1]), Number(full[2]) - 1, Number(full[3]), Number(full[4]), Number(full[5]), Number(full[6])) / 1000
        };
      }
      var m = raw.match(/(\d{2}):(\d{2}):(\d{2})/);
      if (!m) return null;
      return {
        hasDate: false,
        sec: Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3])
      };
    }

    function fmtSecondsHMS(sec) {
      var s = Math.max(0, Math.round(sec));
      var d = new Date(s * 1000);
      return (
        String(d.getUTCHours()).padStart(2, "0") +
        ":" +
        String(d.getUTCMinutes()).padStart(2, "0") +
        ":" +
        String(d.getUTCSeconds()).padStart(2, "0")
      );
    }

    var timelineState = {
      hasTime: false,
      minSec: 0,
      maxSec: 0,
      startRatio: 0,
      endRatio: 1
    };

    function syncTimelineUI() {
      if (!timelineEl || !timeStartInput || !timeEndInput) return;
      if (!timelineState.hasTime || state.events.length < 2) {
        timelineEl.hidden = true;
        return;
      }
      timelineEl.hidden = false;
      var span = Math.max(1, timelineState.maxSec - timelineState.minSec);
      var fromSec = timelineState.minSec + span * timelineState.startRatio;
      var toSec = timelineState.minSec + span * timelineState.endRatio;
      var fromStr = fmtSecondsHMS(fromSec);
      var toStr = fmtSecondsHMS(toSec);
      if (timeReadout) {
        timeReadout.textContent = fromStr + " \u2014 " + toStr;
      }
      if (timeFromLbl) timeFromLbl.textContent = fromStr;
      if (timeToLbl) timeToLbl.textContent = toStr;
      var isFiltered = timelineState.startRatio > 0 || timelineState.endRatio < 1;
      if (timeResetBtn) timeResetBtn.hidden = !isFiltered;
      if (timeCount) {
        var deckEvs = currentZEvents();
        timeCount.textContent = "на палубе: " + deckEvs.length;
      }
    }

    function setupTimelineForEvents(events) {
      var minSec = Infinity;
      var maxSec = -Infinity;
      var prevNoDateSec = null;
      var dayOffset = 0;
      for (var i = 0; i < events.length; i++) {
        var parsed = parseEventSeconds(events[i]);
        if (parsed !== null) {
          var s = parsed.sec;
          if (!parsed.hasDate) {
            if (prevNoDateSec !== null && s + dayOffset < prevNoDateSec - 43200) {
              dayOffset += 86400;
            }
            s += dayOffset;
            prevNoDateSec = s;
          }
          events[i]._sec = s;
          if (s < minSec) minSec = s;
          if (s > maxSec) maxSec = s;
        }
      }
      timelineState.hasTime = isFinite(minSec) && isFinite(maxSec) && maxSec > minSec;
      timelineState.minSec = timelineState.hasTime ? minSec : 0;
      timelineState.maxSec = timelineState.hasTime ? maxSec : 0;
      timelineState.startRatio = 0;
      timelineState.endRatio = 1;
      if (timeStartInput) timeStartInput.value = "0";
      if (timeEndInput) timeEndInput.value = "1000";
      syncTimelineUI();
    }

    if (timeStartInput && timeEndInput && !timeStartInput.dataset.bound) {
      timeStartInput.dataset.bound = "1";
      var onSlider = function (isStart) {
        var sVal = Number(timeStartInput.value);
        var eVal = Number(timeEndInput.value);
        if (sVal > eVal) {
          if (isStart) {
            eVal = sVal;
            timeEndInput.value = String(eVal);
          } else {
            sVal = eVal;
            timeStartInput.value = String(sVal);
          }
        }
        timelineState.startRatio = sVal / 1000;
        timelineState.endRatio = eVal / 1000;
        syncTimelineUI();
        if (!state.selectedTile) {
          showDeckOverview();
        }
        requestDraw();
      };
      timeStartInput.addEventListener("input", function () { onSlider(true); });
      timeEndInput.addEventListener("input", function () { onSlider(false); });
    }

    if (timeResetBtn && !timeResetBtn.dataset.bound) {
      timeResetBtn.dataset.bound = "1";
      timeResetBtn.addEventListener("click", function () {
        timelineState.startRatio = 0;
        timelineState.endRatio = 1;
        if (timeStartInput) timeStartInput.value = "0";
        if (timeEndInput) timeEndInput.value = "1000";
        syncTimelineUI();
        if (!state.selectedTile) {
          showDeckOverview();
        }
        requestDraw();
      });
    }

    function currentZEvents() {
      var out = [];
      var useWindow = timelineState.hasTime && (timelineState.startRatio > 0 || timelineState.endRatio < 1);
      var span = Math.max(1, timelineState.maxSec - timelineState.minSec);
      var minAllowed = timelineState.minSec + span * timelineState.startRatio - 0.5;
      var maxAllowed = timelineState.minSec + span * timelineState.endRatio + 0.5;
      for (var i = 0; i < state.events.length; i++) {
        var ev = state.events[i];
        if (ev.z !== state.currentZ) continue;
        if (useWindow && typeof ev._sec === "number") {
          if (ev._sec < minAllowed || ev._sec > maxAllowed) continue;
        }
        out.push(ev);
      }
      return out;
    }

    function getViewBounds(z, zEvents) {
      var lv = getLevelMeta(z);
      var imgRec = state.imageCache[String(z)];
      var minX = lv.width + 1;
      var maxX = 0;
      var minY = lv.height + 1;
      var maxY = 0;

      if (imgRec && imgRec.bounds) {
        minX = Math.min(minX, imgRec.bounds.minX);
        maxX = Math.max(maxX, imgRec.bounds.maxX);
        minY = Math.min(minY, imgRec.bounds.minY);
        maxY = Math.max(maxY, imgRec.bounds.maxY);
      }
      if (Array.isArray(zEvents)) {
        for (var i = 0; i < zEvents.length; i++) {
          var ev = zEvents[i];
          if (ev.x < minX) minX = ev.x;
          if (ev.x > maxX) maxX = ev.x;
          if (ev.y < minY) minY = ev.y;
          if (ev.y > maxY) maxY = ev.y;
        }
      }

      if (minX > maxX || minY > maxY) {
        return { minX: 1, maxX: lv.width, minY: 1, maxY: lv.height };
      }

      var padTiles = 4;
      return {
        minX: Math.max(1, minX - padTiles),
        maxX: Math.min(lv.width, maxX + padTiles),
        minY: Math.max(1, minY - padTiles),
        maxY: Math.min(lv.height, maxY + padTiles),
      };
    }

    function fitView() {
      var lv = getLevelMeta(state.currentZ);
      var b = getViewBounds(state.currentZ, currentZEvents());
      var bx0 = (b.minX - 1) * lv.tile_px;
      var bx1 = b.maxX * lv.tile_px;
      var by0 = (lv.height - b.maxY) * lv.tile_px;
      var by1 = (lv.height - b.minY + 1) * lv.tile_px;
      var boxW = Math.max(lv.tile_px, bx1 - bx0);
      var boxH = Math.max(lv.tile_px, by1 - by0);
      var pad = 24;
      var availW = Math.max(100, state.cssW - pad * 2);
      var availH = Math.max(100, state.cssH - pad * 2);
      var s = Math.min(availW / boxW, availH / boxH);
      state.scale = Math.max(0.15, Math.min(3.5, s));
      var midX = (bx0 + bx1) / 2;
      var midY = (by0 + by1) / 2;
      state.offsetX = state.cssW / 2 - midX * state.scale;
      state.offsetY = state.cssH / 2 - midY * state.scale;
      state.userMovedView = false;
      updateCenterButton(false);
      updateZoomLabel();
      requestDraw();
    }

    function centerOnTile(tx, ty, desiredScale, markAtTarget) {
      var lv = getLevelMeta(state.currentZ);
      if (desiredScale) {
        state.scale = Math.max(0.12, Math.min(6.0, desiredScale));
      } else if (state.scale < 0.85) {
        state.scale = 1.25;
      }
      var ix = (tx - 0.5) * lv.tile_px;
      var iy = (lv.height - ty + 0.5) * lv.tile_px;
      state.offsetX = state.cssW / 2 - ix * state.scale;
      state.offsetY = state.cssH / 2 - iy * state.scale;
      state.userMovedView = true;
      updateCenterButton(Boolean(markAtTarget));
      updateZoomLabel();
      requestDraw();
    }

    function zoomAt(factor, cx, cy) {
      var oldScale = state.scale;
      var newScale = Math.max(0.12, Math.min(6.0, oldScale * factor));
      if (Math.abs(newScale - oldScale) < 1e-5) return;
      var ratio = newScale / oldScale;
      state.offsetX = cx - (cx - state.offsetX) * ratio;
      state.offsetY = cy - (cy - state.offsetY) * ratio;
      state.scale = newScale;
      state.userMovedView = true;
      updateCenterButton(false);
      updateZoomLabel();
      requestDraw();
    }

    function tileToScreen(tx, ty, lv) {
      var ix = (tx - 0.5) * lv.tile_px;
      var iy = (lv.height - ty + 0.5) * lv.tile_px;
      return {
        x: state.offsetX + ix * state.scale,
        y: state.offsetY + iy * state.scale,
      };
    }

    function screenToTile(sx, sy, lv) {
      var ix = (sx - state.offsetX) / state.scale;
      var iy = (sy - state.offsetY) / state.scale;
      var tx = Math.floor(ix / lv.tile_px) + 1;
      var ty = lv.height - Math.floor(iy / lv.tile_px);
      if (tx < 1 || tx > lv.width || ty < 1 || ty > lv.height) return null;
      return { x: tx, y: ty };
    }

    function updateHud(tx, ty) {
      if (!hudCoords || !hudArea) return;
      if (tx == null || ty == null) {
        hudCoords.textContent = "X: \u2014, Y: \u2014, Z: " + state.currentZ;
        hudArea.textContent = "Наведите на карту";
        return;
      }
      hudCoords.textContent = "X: " + tx + ", Y: " + ty + ", Z: " + state.currentZ;
      var area = lookupArea(state.currentZ, tx, ty);
      hudArea.textContent = area || "Космос / без зоны";
    }

    function buildEventCard(ev) {
      var card = document.createElement("div");
      card.className = "logs-map-event-card is-clickable" + (focusLine && ev.line_no === focusLine ? " is-focused" : "");

      card.addEventListener("click", function (clickEv) {
        if (clickEv.target && clickEv.target.closest("a, button")) return;
        state.selectedLineNos = {};
        state.selectedLineNos[String(ev.line_no)] = true;
        state.selectedTile = { x: ev.x, y: ev.y, z: ev.z };
        inspectorList.querySelectorAll(".logs-map-event-card.is-focused").forEach(function (el) {
          el.classList.remove("is-focused");
        });
        card.classList.add("is-focused");
        if (ev.z !== state.currentZ) {
          selectDeck(ev.z, false, true);
        }
        centerOnTile(ev.x, ev.y, Math.max(state.scale, 1.35), false);
      });

      var metaRow = document.createElement("div");
      metaRow.className = "logs-map-event-meta";

      var lineLink = document.createElement("a");
      lineLink.className = "log-ln";
      lineLink.href = "/logs/round/" + encodeURIComponent(roundUid) + "?" + baseQuery + "&view=stream&focus=" + ev.line_no + "#L" + ev.line_no;
      lineLink.textContent = "#L" + ev.line_no;
      metaRow.appendChild(lineLink);

      var tsSpan = document.createElement("span");
      tsSpan.className = "log-ts";
      tsSpan.textContent = ev.time_short || ev.timestamp || "";
      metaRow.appendChild(tsSpan);

      var catSpan = document.createElement("span");
      catSpan.className = "log-cat-tag " + (ev.cat_css || "cat-misc");
      catSpan.textContent = ev.category || "";
      metaRow.appendChild(catSpan);

      var coordSpan = document.createElement("span");
      coordSpan.className = "mono-val cell-sub";
      coordSpan.textContent = "(" + ev.x + ", " + ev.y + ", Z" + ev.z + ")" + (ev.area ? " \u2022 " + ev.area : "");
      metaRow.appendChild(coordSpan);

      card.appendChild(metaRow);

      var msgDiv = document.createElement("div");
      msgDiv.className = "logs-map-event-msg";
      if (ev.safe_html) {
        msgDiv.innerHTML = ev.safe_html;
      } else {
        msgDiv.textContent = ev.plain_message || "";
      }
      card.appendChild(msgDiv);

      var actionsRow = document.createElement("div");
      actionsRow.className = "logs-map-event-actions";

      var ctxBtn = document.createElement("button");
      ctxBtn.type = "button";
      ctxBtn.className = "log-mini-btn";
      ctxBtn.textContent = "\u00B120";
      ctxBtn.dataset.contextUrl =
        "/logs/round/" +
        encodeURIComponent(roundUid) +
        "/context?file=" +
        encodeURIComponent(selectedFile) +
        "&line=" +
        ev.line_no +
        "&mask_ips=" +
        (maskIps ? "1" : "0") +
        "&html=" +
        (renderHtml ? "1" : "0");
      ctxBtn.dataset.lineNo = String(ev.line_no);
      ctxBtn.addEventListener("click", function () {
        openLogContextModal(ctxBtn.dataset.contextUrl, ctxBtn.dataset.lineNo);
      });
      actionsRow.appendChild(ctxBtn);

      if (Array.isArray(ev.ckeys) && ev.ckeys.length > 0) {
        for (var k = 0; k < ev.ckeys.length; k++) {
          var ck = ev.ckeys[k];
          if (filterCkey && ck === filterCkey) continue;
          var ckBtn = document.createElement("button");
          ckBtn.type = "button";
          ckBtn.className = "log-ckey-btn";
          ckBtn.textContent = ck;
          ckBtn.title = "Фильтровать карту по ckey";
          (function (ckeyVal) {
            ckBtn.addEventListener("click", function () {
              var filterForm = document.getElementById("logs-filter-form");
              var ckeyInput = document.getElementById("logs-ckey-input");
              if (filterForm && ckeyInput) {
                ckeyInput.value = ckeyVal;
                submitFilterForm(filterForm);
              }
            });
          })(ck);
          actionsRow.appendChild(ckBtn);
        }
      }

      card.appendChild(actionsRow);
      return card;
    }

    function appendEventCards(eventsList, markSelected) {
      var slice = eventsList.slice(0, 120);
      for (var i = 0; i < slice.length; i++) {
        if (markSelected) {
          state.selectedLineNos[String(slice[i].line_no)] = true;
        }
        inspectorList.appendChild(buildEventCard(slice[i]));
      }
      if (eventsList.length > slice.length) {
        var moreNote = document.createElement("div");
        moreNote.className = "cell-sub mono-val";
        moreNote.textContent = "Показаны первые " + slice.length + " из " + eventsList.length + " событий.";
        inspectorList.appendChild(moreNote);
      }
    }

    function renderInspector(eventsList, title, subtitle, opts) {
      if (!inspectorList || !inspectorTitle || !inspectorSub) return;
      var options = opts || {};
      inspectorTitle.textContent = title;
      inspectorSub.textContent = subtitle;
      inspectorList.innerHTML = "";
      state.selectedLineNos = {};
      if (showAllDeckBtn) {
        showAllDeckBtn.hidden = !options.showBackBtn;
      }

      if (!eventsList || eventsList.length === 0) {
        var deckEvents = options.fallbackEvents || currentZEvents();
        if (options.isEmptyTile && deckEvents.length > 0) {
          var banner = document.createElement("div");
          banner.className = "logs-map-empty-tile";
          var msg = document.createElement("span");
          msg.textContent = "На этом тайле нет событий под текущие фильтры.";
          banner.appendChild(msg);
          var backBtn = document.createElement("button");
          backBtn.type = "button";
          backBtn.className = "logs-btn logs-btn-sm";
          backBtn.textContent = "Показать все события палубы (" + deckEvents.length + ")";
          backBtn.addEventListener("click", showDeckOverview);
          banner.appendChild(backBtn);
          inspectorList.appendChild(banner);

          var divLbl = document.createElement("div");
          divLbl.className = "logs-map-inspector-divider mono-val";
          divLbl.textContent = "События на палубе Z" + state.currentZ;
          inspectorList.appendChild(divLbl);

          appendEventCards(deckEvents, false);
        } else {
          var empty = document.createElement("div");
          empty.className = "block-empty";
          empty.textContent = "Нет событий на этой палубе по текущим фильтрам.";
          inspectorList.appendChild(empty);
        }
        requestDraw();
        return;
      }

      appendEventCards(eventsList, !options.overview);
      requestDraw();
    }

    function showDeckOverview() {
      var lv = getLevelMeta(state.currentZ);
      var zEvents = currentZEvents();
      var title = "Палуба " + (lv.short_label || ("Z" + state.currentZ));
      var sub = zEvents.length > 0
        ? ("Событий: " + zEvents.length + " \u2022 выберите маркер или карточку")
        : "Нет событий по текущим фильтрам";
      renderInspector(zEvents, title, sub, { showBackBtn: false, overview: true });
    }

    function buildDeckTabs() {
      if (!deckTabs) return;
      deckTabs.innerHTML = "";
      var primaryZs = [];
      var extraZs = [];
      var seen = {};
      if (state.manifest && Array.isArray(state.manifest.levels)) {
        for (var i = 0; i < state.manifest.levels.length; i++) {
          var lv = state.manifest.levels[i];
          seen[String(lv.z)] = true;
          if (lv.z <= 7) {
            primaryZs.push(lv.z);
          } else {
            extraZs.push(lv.z);
          }
        }
      }
      Object.keys(state.zCounts)
        .map(function (k) { return Number(k); })
        .filter(function (z) { return !seen[String(z)]; })
        .sort(function (a, b) { return a - b; })
        .forEach(function (z) {
          if (z <= 7) primaryZs.push(z);
          else extraZs.push(z);
        });
      extraZs.sort(function (a, b) { return a - b; });
      if (primaryZs.length === 0 && extraZs.length === 0) primaryZs = [1, 2, 3, 4, 5, 6, 7];

      for (var idx = 0; idx < primaryZs.length; idx++) {
        var z = primaryZs[idx];
        var meta = getLevelMeta(z);
        var count = state.zCounts[String(z)] || 0;
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "logs-map-deck-btn" + (z === state.currentZ ? " is-active" : "");
        btn.title = meta.label || ("Z" + z);
        var txtSpan = document.createElement("span");
        txtSpan.textContent = meta.short_label || ("Z" + z);
        btn.appendChild(txtSpan);
        var cntSpan = document.createElement("span");
        cntSpan.className = "logs-map-deck-count";
        cntSpan.textContent = "(" + count + ")";
        btn.appendChild(cntSpan);
        (function (deckZ) {
          btn.addEventListener("click", function () {
            selectDeck(deckZ, false);
          });
        })(z);
        deckTabs.appendChild(btn);
      }

      if (sectorSelect) {
        if (extraZs.length === 0) {
          sectorSelect.hidden = true;
          sectorSelect.innerHTML = "";
        } else {
          sectorSelect.hidden = false;
          sectorSelect.innerHTML = "";
          var extraTotal = 0;
          for (var ei = 0; ei < extraZs.length; ei++) {
            extraTotal += Number(state.zCounts[String(extraZs[ei])] || 0);
          }
          var placeholderOpt = document.createElement("option");
          placeholderOpt.value = "";
          placeholderOpt.textContent = "Сектора Z" + extraZs[0] + (extraZs.length > 1 ? "\u2013Z" + extraZs[extraZs.length - 1] : "") + " (" + extraTotal + ")";
          sectorSelect.appendChild(placeholderOpt);

          for (var si = 0; si < extraZs.length; si++) {
            var ez = extraZs[si];
            var emeta = getLevelMeta(ez);
            var ecnt = state.zCounts[String(ez)] || 0;
            var opt = document.createElement("option");
            opt.value = String(ez);
            opt.textContent = (emeta.label || ("Z" + ez)) + " (" + ecnt + ")";
            sectorSelect.appendChild(opt);
          }
          sectorSelect.value = state.currentZ > 7 ? String(state.currentZ) : "";
          sectorSelect.classList.toggle("is-active", state.currentZ > 7);
        }
      }
    }

    function selectDeck(z, centerTarget, skipOverview) {
      state.currentZ = z;
      state.selectedTile = centerTarget && targetX && targetY && targetZ === z
        ? { x: targetX, y: targetY, z: z }
        : null;
      loadAssetsForZ(z);
      buildDeckTabs();
      syncTimelineUI();
      fitView();
      if (centerTarget && targetX && targetY && (!targetZ || targetZ === z)) {
        centerOnTile(targetX, targetY, 1.5, true);
      }
      if (!skipOverview && !centerTarget) {
        showDeckOverview();
      }
      if (state.hoverTile) {
        updateHud(state.hoverTile.x, state.hoverTile.y);
      } else {
        updateHud(null, null);
      }
      requestDraw();
    }

    function buildTrajectoryTracks(zEvents) {
      if (filterCkey) {
        var single = [];
        for (var i = 0; i < zEvents.length; i++) {
          var ev = zEvents[i];
          if (Array.isArray(ev.ckeys) && ev.ckeys.indexOf(filterCkey) !== -1) {
            single.push(ev);
          }
        }
        return single.length >= 2 ? [{ ckey: filterCkey, color: TRACK_COLORS[0], events: single }] : [];
      }

      var groups = {};
      var order = [];
      for (var j = 0; j < zEvents.length; j++) {
        var item = zEvents[j];
        var actor = (Array.isArray(item.ckeys) && item.ckeys[0]) ? item.ckeys[0] : "";
        if (!actor) continue;
        if (!groups[actor]) {
          groups[actor] = [];
          order.push(actor);
        }
        groups[actor].push(item);
      }

      var tracks = [];
      for (var k = 0; k < order.length; k++) {
        var ck = order[k];
        if (groups[ck].length < 2) continue;
        tracks.push({
          ckey: ck,
          color: TRACK_COLORS[tracks.length % TRACK_COLORS.length],
          events: groups[ck],
        });
      }
      return tracks;
    }

    function drawTrajectories(zEvents, lv) {
      var tracks = buildTrajectoryTracks(zEvents);
      if (tracks.length === 0) return;

      var showStepPills = tracks.length === 1 || tracks.length <= 3;
      for (var tIdx = 0; tIdx < tracks.length; tIdx++) {
        var tr = tracks[tIdx];
        var evs = tr.events;
        ctx.save();
        ctx.strokeStyle = tr.color;
        ctx.globalAlpha = tracks.length > 1 ? 0.72 : 0.92;
        ctx.lineWidth = tracks.length > 1 ? 1.8 : 2.4;
        ctx.lineJoin = "round";
        ctx.beginPath();
        var prevPt = null;
        for (var i = 0; i < evs.length; i++) {
          var curPt = tileToScreen(evs[i].x, evs[i].y, lv);
          if (!prevPt) {
            ctx.moveTo(curPt.x, curPt.y);
          } else if (Math.hypot(curPt.x - prevPt.x, curPt.y - prevPt.y) > 4) {
            ctx.lineTo(curPt.x, curPt.y);
          }
          prevPt = curPt;
        }
        ctx.stroke();

        // Direction arrows
        ctx.fillStyle = tr.color;
        for (var k = 1; k < evs.length; k++) {
          var aPt = tileToScreen(evs[k - 1].x, evs[k - 1].y, lv);
          var bPt = tileToScreen(evs[k].x, evs[k].y, lv);
          var dist = Math.hypot(bPt.x - aPt.x, bPt.y - aPt.y);
          if (dist >= 22) {
            var mx = (aPt.x + bPt.x) / 2;
            var my = (aPt.y + bPt.y) / 2;
            var ang = Math.atan2(bPt.y - aPt.y, bPt.x - aPt.x);
            ctx.beginPath();
            ctx.moveTo(mx + Math.cos(ang) * 5, my + Math.sin(ang) * 5);
            ctx.lineTo(mx + Math.cos(ang + 2.5) * 5, my + Math.sin(ang + 2.5) * 5);
            ctx.lineTo(mx + Math.cos(ang - 2.5) * 5, my + Math.sin(ang - 2.5) * 5);
            ctx.closePath();
            ctx.fill();
          }
        }

        // Collapse consecutive same-tile steps into non-overlapping pills like "1–3"
        if (showStepPills) {
          var merged = [];
          for (var s = 0; s < evs.length; s++) {
            var pt = tileToScreen(evs[s].x, evs[s].y, lv);
            var last = merged.length > 0 ? merged[merged.length - 1] : null;
            if (last && (last.tx === evs[s].x && last.ty === evs[s].y || Math.hypot(pt.x - last.sx, pt.y - last.sy) < 10)) {
              last.endStep = s + 1;
            } else {
              merged.push({ tx: evs[s].x, ty: evs[s].y, sx: pt.x, sy: pt.y, startStep: s + 1, endStep: s + 1 });
            }
          }
          if (merged.length <= 30) {
            ctx.globalAlpha = 1;
            ctx.font = "700 10px 'JetBrains Mono', Consolas, monospace";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            for (var m = 0; m < merged.length; m++) {
              var st = merged[m];
              var lbl = st.startStep === st.endStep ? String(st.startStep) : (st.startStep + "\u2013" + st.endStep);
              var pillW = Math.max(16, ctx.measureText(lbl).width + 8);
              var px = st.sx - pillW / 2;
              var py = st.sy - 21;
              ctx.fillStyle = "rgba(8, 10, 14, 0.9)";
              ctx.strokeStyle = tr.color;
              ctx.lineWidth = 1;
              ctx.fillRect(px, py, pillW, 13);
              ctx.strokeRect(px, py, pillW, 13);
              ctx.fillStyle = "#f1f5f9";
              ctx.fillText(lbl, st.sx, py + 6.5);
            }
          }
        }
        ctx.restore();
      }
    }

    function drawHeatmap(zEvents, lv, w, h) {
      if (zEvents.length === 0) return;
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      var rHeat = Math.max(18, lv.tile_px * state.scale * 2.8);
      for (var hi = 0; hi < zEvents.length; hi++) {
        var hev = zEvents[hi];
        var hpt = tileToScreen(hev.x, hev.y, lv);
        if (hpt.x < -rHeat || hpt.x > w + rHeat || hpt.y < -rHeat || hpt.y > h + rHeat) continue;
        var grad = ctx.createRadialGradient(hpt.x, hpt.y, 1, hpt.x, hpt.y, rHeat);
        grad.addColorStop(0, "rgba(239, 68, 68, 0.45)");
        grad.addColorStop(0.45, "rgba(245, 158, 11, 0.22)");
        grad.addColorStop(1, "rgba(245, 158, 11, 0)");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(hpt.x, hpt.y, rHeat, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    function drawTooltip(pin, w, h) {
      if (!pin || !pin.events || pin.events.length === 0) return;
      var cnt = pin.events.length;
      var first = pin.events[0];
      var primaryCkey = (Array.isArray(first.ckeys) && first.ckeys[0]) ? first.ckeys[0] : "";
      var header = cnt === 1
        ? (first.category + " #L" + first.line_no + (first.time_short ? " \u2022 " + first.time_short : "") + (primaryCkey ? " \u2022 " + primaryCkey : ""))
        : ("Кластер: " + cnt + " соб. (" + pin.x + ", " + pin.y + ")");
      var previewRaw = "";
      if (cnt === 1) {
        previewRaw = first.plain_message || "";
      } else {
        var catFreq = {};
        var catOrder = [];
        for (var ci = 0; ci < pin.events.length; ci++) {
          var cname = pin.events[ci].category || "OTHER";
          if (!catFreq[cname]) {
            catFreq[cname] = 0;
            catOrder.push(cname);
          }
          catFreq[cname] += 1;
        }
        catOrder.sort(function (a, b) { return catFreq[b] - catFreq[a]; });
        previewRaw = catOrder.slice(0, 3).map(function (c) {
          return c + "\u00D7" + catFreq[c];
        }).join(", ") + (catOrder.length > 3 ? " \u2026" : "");
      }
      var preview = previewRaw.length > 56 ? previewRaw.slice(0, 55) + "\u2026" : previewRaw;

      ctx.save();
      ctx.font = "700 11px 'JetBrains Mono', Consolas, monospace";
      var hdrW = ctx.measureText(header).width;
      ctx.font = "400 11px system-ui, -apple-system, sans-serif";
      var bodyW = ctx.measureText(preview).width;
      var boxW = Math.min(340, Math.max(150, Math.max(hdrW, bodyW) + 20));
      var boxH = preview ? 42 : 24;

      var bx = pin.sx + 14;
      var by = pin.sy - boxH - 10;
      if (bx + boxW > w - 8) bx = pin.sx - boxW - 14;
      if (bx < 8) bx = 8;
      if (by < 8) by = pin.sy + 14;
      if (by + boxH > h - 8) by = h - boxH - 8;

      ctx.fillStyle = "rgba(11, 14, 20, 0.94)";
      ctx.strokeStyle = "#334155";
      ctx.lineWidth = 1;
      ctx.fillRect(bx, by, boxW, boxH);
      ctx.strokeRect(bx, by, boxW, boxH);

      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.font = "700 11px 'JetBrains Mono', Consolas, monospace";
      ctx.fillStyle = categoryPinColor(first.category);
      ctx.fillText(header, bx + 9, by + 6, boxW - 18);

      if (preview) {
        ctx.font = "400 11px system-ui, -apple-system, sans-serif";
        ctx.fillStyle = "#e2e8f0";
        ctx.fillText(preview, bx + 9, by + 22, boxW - 18);
      }
      ctx.restore();
    }

    var drawQueued = false;
    function requestDraw() {
      if (drawQueued) return;
      drawQueued = true;
      window.requestAnimationFrame(function () {
        drawQueued = false;
        draw();
      });
    }

    function draw() {
      var w = state.cssW;
      var h = state.cssH;
      var lv = getLevelMeta(state.currentZ);
      var imgW = lv.width * lv.tile_px;
      var imgH = lv.height * lv.tile_px;

      ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
      ctx.imageSmoothingEnabled = false;
      ctx.fillStyle = "#080a0e";
      ctx.fillRect(0, 0, w, h);

      var x0 = state.offsetX;
      var y0 = state.offsetY;
      var drawW = imgW * state.scale;
      var drawH = imgH * state.scale;

      // 1. Station PNG or Fallback Coordinate Grid
      var imgRec = state.imageCache[String(state.currentZ)];
      if (imgRec && imgRec.status === "loaded") {
        ctx.drawImage(imgRec.img, x0, y0, drawW, drawH);
        if (state.dimMap) {
          ctx.fillStyle = "rgba(8, 10, 14, 0.56)";
          ctx.fillRect(x0, y0, drawW, drawH);
        }
      } else {
        ctx.fillStyle = "#12161c";
        ctx.fillRect(x0, y0, drawW, drawH);
        ctx.strokeStyle = "rgba(134, 148, 168, 0.14)";
        ctx.lineWidth = 1;
        var stepMajor = 10 * lv.tile_px * state.scale;
        if (stepMajor >= 8) {
          ctx.beginPath();
          for (var gx = 0; gx <= lv.width; gx += 10) {
            var sxLine = x0 + gx * lv.tile_px * state.scale;
            ctx.moveTo(sxLine, y0);
            ctx.lineTo(sxLine, y0 + drawH);
          }
          for (var gy = 0; gy <= lv.height; gy += 10) {
            var syLine = y0 + gy * lv.tile_px * state.scale;
            ctx.moveTo(x0, syLine);
            ctx.lineTo(x0 + drawW, syLine);
          }
          ctx.stroke();
        }
      }

      // Map bounds frame
      ctx.strokeStyle = "rgba(134, 148, 168, 0.35)";
      ctx.lineWidth = 1;
      ctx.strokeRect(x0, y0, drawW, drawH);

      var zEvents = currentZEvents();

      // 2. Heatmap Overlay
      if (state.mode === "heat") {
        drawHeatmap(zEvents, lv, w, h);
      }

      // 3. Trajectory Path
      if (state.trajEnabled && zEvents.length > 1) {
        drawTrajectories(zEvents, lv);
      }

      // 4. Pins / Clusters (build hit targets in both modes; render pins in pins mode)
      var renderedPins = [];
      var byTile = {};
      var tileKeys = [];
      for (var pi = 0; pi < zEvents.length; pi++) {
        var pev = zEvents[pi];
        var tKey = pev.x + "," + pev.y;
        if (!byTile[tKey]) {
          var spt2 = tileToScreen(pev.x, pev.y, lv);
          byTile[tKey] = {
            sx: spt2.x,
            sy: spt2.y,
            x: pev.x,
            y: pev.y,
            events: [],
            hasFocus: false,
          };
          tileKeys.push(tKey);
        }
        byTile[tKey].events.push(pev);
        if ((focusLine && pev.line_no === focusLine) || state.selectedLineNos[String(pev.line_no)]) {
          byTile[tKey].hasFocus = true;
        }
      }

      var clusters = [];
      if (state.clusterEnabled) {
        var clusterDist = 20;
        for (var ci = 0; ci < tileKeys.length; ci++) {
          var grp = byTile[tileKeys[ci]];
          if (grp.sx < -30 || grp.sx > w + 30 || grp.sy < -30 || grp.sy > h + 30) continue;
          var merged = false;
          for (var cj = 0; cj < clusters.length; cj++) {
            var cl = clusters[cj];
            if (Math.hypot(cl.sx - grp.sx, cl.sy - grp.sy) <= clusterDist) {
              var totalOld = cl.events.length;
              var totalAdd = grp.events.length;
              cl.sx = (cl.sx * totalOld + grp.sx * totalAdd) / (totalOld + totalAdd);
              cl.sy = (cl.sy * totalOld + grp.sy * totalAdd) / (totalOld + totalAdd);
              for (var ek = 0; ek < grp.events.length; ek++) cl.events.push(grp.events[ek]);
              if (grp.hasFocus) cl.hasFocus = true;
              merged = true;
              break;
            }
          }
          if (!merged) {
            clusters.push({
              sx: grp.sx,
              sy: grp.sy,
              x: grp.x,
              y: grp.y,
              events: grp.events.slice(),
              hasFocus: grp.hasFocus,
            });
          }
        }
      } else {
        for (var ui = 0; ui < tileKeys.length; ui++) {
          var ugrp = byTile[tileKeys[ui]];
          if (ugrp.sx < -30 || ugrp.sx > w + 30 || ugrp.sy < -30 || ugrp.sy > h + 30) continue;
          clusters.push(ugrp);
        }
      }

      for (var ri = 0; ri < clusters.length; ri++) {
        var item = clusters[ri];
        var cnt = item.events.length;
        var color = categoryPinColor(item.events[0].category);
        var radius = cnt > 1 ? Math.min(18, 9 + Math.log2(cnt) * 2.4) : 7;

        if (state.mode === "pins" || item.hasFocus) {
          if (item.hasFocus) {
            ctx.strokeStyle = "#d92638";
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            ctx.arc(item.sx, item.sy, radius + 5, 0, Math.PI * 2);
            ctx.stroke();
          }

          if (state.mode === "pins") {
            ctx.fillStyle = "rgba(0, 0, 0, 0.72)";
            ctx.beginPath();
            ctx.arc(item.sx, item.sy, radius + 2, 0, Math.PI * 2);
            ctx.fill();

            ctx.fillStyle = color;
            ctx.strokeStyle = "#f8fafc";
            ctx.lineWidth = 1.4;
            ctx.beginPath();
            ctx.arc(item.sx, item.sy, radius, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();

            if (cnt > 1) {
              ctx.fillStyle = "#080a0e";
              ctx.font = "700 10px 'JetBrains Mono', Consolas, monospace";
              ctx.textAlign = "center";
              ctx.textBaseline = "middle";
              ctx.fillText(String(cnt), item.sx, item.sy + 0.5);
            }
          }
        }

        renderedPins.push({
          sx: item.sx,
          sy: item.sy,
          radius: Math.max(radius + 5, 13),
          x: item.x,
          y: item.y,
          events: item.events,
        });
      }
      state.renderedPins = renderedPins;

      // 5. Target Tile & Hover Tile Outlines
      if (state.selectedTile && state.selectedTile.z === state.currentZ) {
        var st = state.selectedTile;
        var stX = x0 + (st.x - 1) * lv.tile_px * state.scale;
        var stY = y0 + (lv.height - st.y) * lv.tile_px * state.scale;
        var stSize = lv.tile_px * state.scale;
        ctx.strokeStyle = "#d92638";
        ctx.lineWidth = 2;
        ctx.strokeRect(stX, stY, stSize, stSize);
      }

      if (state.hoverTile && !state.hoverPin) {
        var ht = state.hoverTile;
        var htX = x0 + (ht.x - 1) * lv.tile_px * state.scale;
        var htY = y0 + (lv.height - ht.y) * lv.tile_px * state.scale;
        var htSize = lv.tile_px * state.scale;
        ctx.strokeStyle = "rgba(255, 255, 255, 0.7)";
        ctx.lineWidth = 1;
        ctx.strokeRect(htX, htY, htSize, htSize);
      }

      // 6. Hovered pin tooltip
      if (state.hoverPin && !state.dragging) {
        drawTooltip(state.hoverPin, w, h);
      }
    }

    function hitTestPin(mx, my) {
      var best = null;
      var bestDist = 99999;
      for (var i = 0; i < state.renderedPins.length; i++) {
        var pin = state.renderedPins[i];
        var d = Math.hypot(mx - pin.sx, my - pin.sy);
        if (d <= pin.radius && d < bestDist) {
          best = pin;
          bestDist = d;
        }
      }
      return best;
    }

    function syncCanvasSize() {
      var rect = canvas.getBoundingClientRect();
      if (rect.width < 10 || rect.height < 10) return;
      state.dpr = window.devicePixelRatio || 1;
      state.cssW = rect.width;
      state.cssH = rect.height;
      canvas.width = Math.round(rect.width * state.dpr);
      canvas.height = Math.round(rect.height * state.dpr);
      requestDraw();
    }

    function applyMapPayload(data, opts) {
      var isInitial = opts && opts.isInitial;
      if (data.manifest) {
        state.manifest = data.manifest;
        state.levelsByZ = {};
        if (Array.isArray(state.manifest.levels)) {
          for (var i = 0; i < state.manifest.levels.length; i++) {
            var lv = state.manifest.levels[i];
            state.levelsByZ[String(lv.z)] = lv;
          }
        }
      }
      state.events = Array.isArray(data.events) ? data.events : [];
      state.zCounts = data.z_counts || {};
      state.hoverPin = null;
      setupTimelineForEvents(state.events);

      if (isInitial) {
        if (!targetZ) {
          var bestZ = 1;
          var bestCnt = -1;
          Object.keys(state.zCounts).forEach(function (k) {
            var c = Number(state.zCounts[k]) || 0;
            if (c > bestCnt) {
              bestCnt = c;
              bestZ = Number(k) || 1;
            }
          });
          if (bestCnt <= 0 && state.manifest && state.manifest.levels && state.manifest.levels.length > 0) {
            bestZ = state.manifest.levels[0].z;
          }
          state.currentZ = bestZ;
        } else {
          state.currentZ = targetZ;
        }
      } else if (!(state.zCounts[String(state.currentZ)] > 0)) {
        // Switch deck on filter reload only if current deck has 0 matches and another deck has matches
        var altZ = state.currentZ;
        var altCnt = 0;
        Object.keys(state.zCounts).forEach(function (k) {
          var c = Number(state.zCounts[k]) || 0;
          if (c > altCnt) {
            altCnt = c;
            altZ = Number(k) || altZ;
          }
        });
        if (altCnt > 0 && altZ !== state.currentZ) {
          state.currentZ = altZ;
          state.selectedTile = null;
          loadAssetsForZ(altZ);
          syncCanvasSize();
          fitView();
        }
      }

      if (statusEl) {
        var totalCoords = Number(data.total_with_coords) || state.events.length;
        var msg = "Событий с координатами: " + totalCoords;
        if (data.truncated) {
          msg += " (показаны первые " + state.events.length + " \u2014 уточните фильтры)";
        }
        statusEl.textContent = msg;
      }

      loadAssetsForZ(state.currentZ);
      buildDeckTabs();
      if (isInitial) {
        syncCanvasSize();
        fitView();
      }

      if (isInitial && targetX && targetY && (!targetZ || targetZ === state.currentZ)) {
        state.selectedTile = { x: targetX, y: targetY, z: state.currentZ };
        centerOnTile(targetX, targetY, 1.5, true);
        var focusedMatches = [];
        for (var f = 0; f < state.events.length; f++) {
          var fev = state.events[f];
          if (
            (focusLine && fev.line_no === focusLine) ||
            (fev.z === state.currentZ && fev.x === targetX && fev.y === targetY)
          ) {
            focusedMatches.push(fev);
          }
        }
        var fArea = lookupArea(state.currentZ, targetX, targetY) || (focusedMatches[0] && focusedMatches[0].area) || "";
        renderInspector(
          focusedMatches,
          focusLine && focusedMatches.length ? ("Фокус #L" + focusLine) : ("Точка (" + targetX + ", " + targetY + ", Z" + state.currentZ + ")"),
          "X:" + targetX + " Y:" + targetY + " Z:" + state.currentZ + (fArea ? " \u2022 " + fArea : ""),
          { showBackBtn: true, isEmptyTile: focusedMatches.length === 0 }
        );
      } else if (state.selectedTile && state.selectedTile.z === state.currentZ && !isInitial) {
        var st = state.selectedTile;
        var tileMatches = [];
        for (var m = 0; m < state.events.length; m++) {
          var mev = state.events[m];
          if (mev.z === st.z && mev.x === st.x && mev.y === st.y) tileMatches.push(mev);
        }
        if (tileMatches.length > 0) {
          var mArea = lookupArea(st.z, st.x, st.y) || tileMatches[0].area || "";
          renderInspector(tileMatches, "Тайл (" + st.x + ", " + st.y + ", Z" + st.z + ")", mArea, { showBackBtn: true });
        } else {
          state.selectedTile = null;
          showDeckOverview();
        }
      } else {
        showDeckOverview();
      }
      requestDraw();
    }

    function reloadFromFilterForm(filterForm, newBaseQuery) {
      if (!filterForm) return;
      var formData = new FormData(filterForm);
      var mapParams = new URLSearchParams();
      mapParams.set("file", String(formData.get("file") || selectedFile));
      mapParams.set("preset", String(formData.get("preset") || "gameplay"));
      formData.getAll("cat").forEach(function (c) {
        if (c) mapParams.append("cat", String(c));
      });
      var newCkey = String(formData.get("ckey") || "").trim();
      var newQ = String(formData.get("q") || "").trim();
      var newFrom = String(formData.get("time_from") || "").trim();
      var newTo = String(formData.get("time_to") || "").trim();
      var newRegex = Boolean(formData.get("regex"));
      var newMask = Boolean(formData.get("mask_ips"));
      if (newCkey) mapParams.set("ckey", newCkey);
      if (newQ) mapParams.set("q", newQ);
      if (newRegex) mapParams.set("regex", "1");
      if (newFrom) mapParams.set("time_from", newFrom);
      if (newTo) mapParams.set("time_to", newTo);
      if (newMask) mapParams.set("mask_ips", "1");

      var prevCkey = filterCkey;
      filterCkey = normalizeCkeyClient(newCkey);
      maskIps = newMask;
      renderHtml = String(formData.get("html") || "1") !== "0";
      if (newBaseQuery) baseQuery = newBaseQuery;

      if (!prevCkey && filterCkey && trajCb) {
        trajCb.checked = true;
        state.trajEnabled = true;
      }

      var ajaxUrl = "/api/v1/logs/rounds/" + encodeURIComponent(roundUid) + "/map-events?" + mapParams.toString();
      if (statusEl) statusEl.textContent = "Обновление событий...";
      fetch(ajaxUrl, { credentials: "same-origin" })
        .then(function (resp) {
          if (!resp.ok) {
            return resp.json().catch(function () { return {}; }).then(function (errBody) {
              throw new Error(errBody.detail || ("HTTP " + resp.status));
            });
          }
          return resp.json();
        })
        .then(function (data) {
          if (errorEl) {
            errorEl.hidden = true;
            errorEl.textContent = "";
          }
          applyMapPayload(data, { isInitial: false });
        })
        .catch(function (err) {
          if (errorEl) {
            errorEl.textContent = err && err.message ? err.message : "Ошибка загрузки событий карты.";
            errorEl.hidden = false;
          }
          if (statusEl) {
            statusEl.textContent = "Ошибка фильтрации карты.";
          }
        });
    }

    window.__logsMapReload = reloadFromFilterForm;

    // Bind controls
    if (modePinsBtn && modeHeatBtn) {
      modePinsBtn.addEventListener("click", function () {
        state.mode = "pins";
        modePinsBtn.classList.add("is-active");
        modeHeatBtn.classList.remove("is-active");
        requestDraw();
      });
      modeHeatBtn.addEventListener("click", function () {
        state.mode = "heat";
        modeHeatBtn.classList.add("is-active");
        modePinsBtn.classList.remove("is-active");
        requestDraw();
      });
    }
    if (dimBtn) {
      dimBtn.addEventListener("click", function () {
        state.dimMap = !state.dimMap;
        dimBtn.classList.toggle("is-active", state.dimMap);
        dimBtn.setAttribute("aria-pressed", state.dimMap ? "true" : "false");
        requestDraw();
      });
    }
    if (clusterCb) {
      clusterCb.addEventListener("change", function () {
        state.clusterEnabled = clusterCb.checked;
        requestDraw();
      });
    }
    if (trajCb) {
      trajCb.addEventListener("change", function () {
        state.trajEnabled = trajCb.checked;
        requestDraw();
      });
    }
    if (zoomInBtn) {
      zoomInBtn.addEventListener("click", function () {
        zoomAt(1.35, state.cssW / 2, state.cssH / 2);
      });
    }
    if (zoomOutBtn) {
      zoomOutBtn.addEventListener("click", function () {
        zoomAt(1 / 1.35, state.cssW / 2, state.cssH / 2);
      });
    }
    if (fitBtn) {
      fitBtn.addEventListener("click", fitView);
    }
    if (centerBtn && targetX && targetY) {
      centerBtn.addEventListener("click", function () {
        var desiredZ = targetZ || state.currentZ;
        if (desiredZ !== state.currentZ) {
          selectDeck(desiredZ, true);
        } else {
          state.selectedTile = { x: targetX, y: targetY, z: state.currentZ };
          centerOnTile(targetX, targetY, 1.5, true);
        }
      });
    }
    if (sectorSelect) {
      sectorSelect.addEventListener("change", function () {
        var val = Number(sectorSelect.value);
        if (val > 0) {
          selectDeck(val, false);
        }
      });
    }
    if (showAllDeckBtn) {
      showAllDeckBtn.addEventListener("click", function () {
        state.selectedTile = null;
        showDeckOverview();
        requestDraw();
      });
    }

    canvas.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var rect = canvas.getBoundingClientRect();
      var mx = ev.clientX - rect.left;
      var my = ev.clientY - rect.top;
      var factor = ev.deltaY < 0 ? 1.18 : 1 / 1.18;
      zoomAt(factor, mx, my);
    }, { passive: false });

    canvas.addEventListener("mousedown", function (ev) {
      if (ev.button !== 0) return;
      state.dragging = true;
      state.dragMoved = false;
      state.hoverPin = null;
      state.dragStartX = ev.clientX;
      state.dragStartY = ev.clientY;
      state.dragOrigOffsetX = state.offsetX;
      state.dragOrigOffsetY = state.offsetY;
      canvas.classList.add("is-dragging");
      canvas.classList.remove("is-hover-pin");
    });

    canvas.addEventListener("mouseleave", function () {
      var needDraw = Boolean(state.hoverTile || state.hoverPin);
      state.hoverTile = null;
      state.hoverPin = null;
      canvas.classList.remove("is-hover-pin");
      updateHud(null, null);
      if (needDraw) requestDraw();
    });

    window.addEventListener("mousemove", function (ev) {
      if (state.dragging) {
        var dx = ev.clientX - state.dragStartX;
        var dy = ev.clientY - state.dragStartY;
        if (Math.abs(dx) > 4 || Math.abs(dy) > 4) {
          state.dragMoved = true;
          state.userMovedView = true;
          updateCenterButton(false);
        }
        state.offsetX = state.dragOrigOffsetX + dx;
        state.offsetY = state.dragOrigOffsetY + dy;
        requestDraw();
        return;
      }
      var rect = canvas.getBoundingClientRect();
      if (
        ev.clientX >= rect.left &&
        ev.clientX <= rect.right &&
        ev.clientY >= rect.top &&
        ev.clientY <= rect.bottom
      ) {
        var mx = ev.clientX - rect.left;
        var my = ev.clientY - rect.top;
        var lv = getLevelMeta(state.currentZ);
        var tile = screenToTile(mx, my, lv);
        var pinHit = hitTestPin(mx, my);
        canvas.classList.toggle("is-hover-pin", Boolean(pinHit));

        var prevPin = state.hoverPin;
        var prevTile = state.hoverTile;
        var tileChanged =
          (!prevTile && tile) ||
          (prevTile && !tile) ||
          (prevTile && tile && (prevTile.x !== tile.x || prevTile.y !== tile.y));
        var pinChanged = prevPin !== pinHit;

        if (tileChanged) {
          state.hoverTile = tile;
          updateHud(tile ? tile.x : null, tile ? tile.y : null);
        }
        if (pinChanged) {
          state.hoverPin = pinHit;
        }
        if (tileChanged || pinChanged) {
          requestDraw();
        }
      }
    });

    window.addEventListener("mouseup", function (ev) {
      if (!state.dragging) return;
      state.dragging = false;
      canvas.classList.remove("is-dragging");
      if (state.dragMoved) return;

      var rect = canvas.getBoundingClientRect();
      var mx = ev.clientX - rect.left;
      var my = ev.clientY - rect.top;
      if (mx < 0 || my < 0 || mx > rect.width || my > rect.height) return;

      var best = hitTestPin(mx, my);
      if (best) {
        state.selectedTile = { x: best.x, y: best.y, z: state.currentZ };
        var title = best.events.length === 1
          ? ("Событие #L" + best.events[0].line_no)
          : ("Кластер (" + best.events.length + " событий)");
        var area = lookupArea(state.currentZ, best.x, best.y) || best.events[0].area || "";
        var sub = "X:" + best.x + " Y:" + best.y + " Z:" + state.currentZ + (area ? " \u2022 " + area : "");
        updateCenterButton(targetX === best.x && targetY === best.y);
        renderInspector(best.events, title, sub, { showBackBtn: true });
        return;
      }

      var lv = getLevelMeta(state.currentZ);
      var tile = screenToTile(mx, my, lv);
      if (tile) {
        state.selectedTile = { x: tile.x, y: tile.y, z: state.currentZ };
        var tileEvents = [];
        for (var k = 0; k < state.events.length; k++) {
          var evItem = state.events[k];
          if (evItem.z === state.currentZ && evItem.x === tile.x && evItem.y === tile.y) {
            tileEvents.push(evItem);
          }
        }
        var tArea = lookupArea(state.currentZ, tile.x, tile.y) || "Космос / без зоны";
        updateCenterButton(targetX === tile.x && targetY === tile.y);
        renderInspector(
          tileEvents,
          "Тайл (" + tile.x + ", " + tile.y + ", Z" + state.currentZ + ")",
          tArea,
          { showBackBtn: true, isEmptyTile: tileEvents.length === 0 }
        );
      }
    });

    syncCanvasSize();
    if (window.ResizeObserver) {
      new ResizeObserver(syncCanvasSize).observe(canvas);
    } else {
      window.addEventListener("resize", syncCanvasSize);
    }

    // Fetch round map events + manifest
    fetch(eventsUrl, { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        applyMapPayload(data, { isInitial: true });
      })
      .catch(function () {
        if (statusEl) {
          statusEl.textContent = "Ошибка загрузки событий карты.";
        }
      });
  }

  function initAdminMapBuildPolling() {
    var banner = document.getElementById("logs-map-build-status");
    if (!banner || banner.dataset.mapBuilding !== "1") return;
    var detailEl = document.getElementById("logs-map-build-detail");
    var timer = setInterval(function () {
      fetch("/logs/maps/manifest", { credentials: "same-origin" })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(function (data) {
          var bs = data && data.build_status;
          if (!bs) return;
          if (bs.state === "running") {
            if (detailEl && bs.detail) {
              detailEl.textContent = bs.detail;
            }
            return;
          }
          clearInterval(timer);
          window.location.reload();
        })
        .catch(function () {
          // Keep polling quietly on transient error
        });
    }, 2000);
  }

  var adminAuditLiveEnabled = false;
  var adminAuditLiveTimer = null;

  function initConfirmButtons(root) {
    var scope = root || document;
    scope.querySelectorAll("[data-confirm-text]").forEach(function (btn) {
      if (btn.dataset.confirmBound) return;
      btn.dataset.confirmBound = "1";
      var defaultText = btn.textContent;
      var armTimer = null;

      btn.addEventListener("click", function (ev) {
        if (btn.dataset.confirmArmed === "1") {
          if (armTimer) clearTimeout(armTimer);
          return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        btn.dataset.confirmArmed = "1";
        btn.classList.add("is-confirm-armed");
        btn.textContent = btn.dataset.confirmText || "Подтвердить?";
        armTimer = setTimeout(function () {
          btn.dataset.confirmArmed = "0";
          btn.classList.remove("is-confirm-armed");
          btn.textContent = defaultText;
        }, 4000);
      });
    });
  }

  function initLoadingForms(root) {
    var scope = root || document;
    scope.querySelectorAll("form[data-loading-form]").forEach(function (form) {
      if (form.dataset.loadingBound) return;
      form.dataset.loadingBound = "1";
      form.addEventListener("submit", function () {
        var submitBtn = form.querySelector('button[type="submit"]');
        if (submitBtn) {
          var loadingLabel = submitBtn.dataset.loadingLabel || "Выполняется...";
          submitBtn.disabled = true;
          submitBtn.textContent = loadingLabel;
        }
      });
    });
  }

  function formatAuditLocalTimes(root) {
    var scope = root || document;
    scope.querySelectorAll("time[data-utc-iso]").forEach(function (el) {
      var iso = el.dataset.utcIso;
      if (!iso) return;
      var d = new Date(iso);
      if (isNaN(d.getTime())) return;
      try {
        el.title = "Локальное время: " + d.toLocaleString();
      } catch (_err) {
        // Ignore locale formatting errors
      }
    });
  }

  function reloadAdminAuditContainer() {
    var container = document.getElementById("admin-audit-container");
    if (!container) return;
    var url = window.location.pathname + window.location.search;
    if (window.htmx && typeof window.htmx.ajax === "function") {
      window.htmx.ajax("GET", url, { target: "#admin-audit-container", swap: "innerHTML" });
    } else {
      window.location.reload();
    }
  }

  function initAdminAuditHelpers() {
    var container = document.getElementById("admin-audit-container");
    if (!container) return;

    formatAuditLocalTimes(container);
    initCopyButtons();
    initConfirmButtons(container);

    var filterForm = document.getElementById("admin-audit-filter-form");
    if (filterForm && !filterForm.dataset.auditBound) {
      filterForm.dataset.auditBound = "1";
      filterForm.querySelectorAll('select, input[type="date"]').forEach(function (ctrl) {
        ctrl.addEventListener("change", function () {
          if (ctrl.name === "action") {
            var grpInput = filterForm.querySelector('input[name="group"]');
            if (grpInput) grpInput.value = "";
          }
          if (window.htmx && typeof window.htmx.trigger === "function") {
            window.htmx.trigger(filterForm, "submit");
          } else {
            filterForm.submit();
          }
        });
      });
    }

    var refreshBtn = document.getElementById("admin-audit-refresh-btn");
    if (refreshBtn && !refreshBtn.dataset.bound) {
      refreshBtn.dataset.bound = "1";
      refreshBtn.addEventListener("click", reloadAdminAuditContainer);
    }

    var liveCb = document.getElementById("admin-audit-live-cb");
    if (liveCb) {
      liveCb.checked = adminAuditLiveEnabled;
      var toggleLabel = liveCb.closest(".admin-live-toggle");
      if (toggleLabel) toggleLabel.classList.toggle("is-live", adminAuditLiveEnabled);

      if (!liveCb.dataset.bound) {
        liveCb.dataset.bound = "1";
        liveCb.addEventListener("change", function () {
          adminAuditLiveEnabled = liveCb.checked;
          if (toggleLabel) toggleLabel.classList.toggle("is-live", adminAuditLiveEnabled);
          if (adminAuditLiveTimer) {
            clearInterval(adminAuditLiveTimer);
            adminAuditLiveTimer = null;
          }
          if (adminAuditLiveEnabled) {
            adminAuditLiveTimer = setInterval(function () {
              if (document.visibilityState !== "visible") return;
              var active = document.activeElement;
              var curForm = document.getElementById("admin-audit-filter-form");
              if (active && curForm && curForm.contains(active)) return;
              reloadAdminAuditContainer();
            }, 10000);
          }
        });
      }
    }
  }

  function setActiveAdminTab(tabName) {
    var tabs = document.querySelectorAll("[data-admin-tab]");
    var sections = document.querySelectorAll("[data-admin-section]");
    if (!tabs.length || !sections.length) return;
    var activeTab = Array.from(tabs).some(function (btn) {
      return btn.dataset.adminTab === tabName;
    }) ? tabName : "audit";
    tabs.forEach(function (btn) {
      var selected = btn.dataset.adminTab === activeTab;
      btn.classList.toggle("is-active", selected);
      btn.setAttribute("aria-pressed", String(selected));
    });
    sections.forEach(function (sec) {
      var visible = activeTab === "all" || sec.dataset.adminSection === activeTab;
      sec.classList.toggle("admin-section-hidden", !visible);
    });
  }

  function initAdminDashboard() {
    var adminRoot = document.querySelector(".admin-page-root");
    if (!adminRoot) return;

    initConfirmButtons(adminRoot);
    initLoadingForms(adminRoot);
    initAdminAuditHelpers();

    var tabs = adminRoot.querySelectorAll("[data-admin-tab]");
    var hashMatch = window.location.hash.match(/^#tab=([a-z]+)$/);
    setActiveAdminTab(hashMatch ? hashMatch[1] : "audit");
    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var nextTab = btn.dataset.adminTab;
        setActiveAdminTab(nextTab);
        var newHash = nextTab === "audit" ? "" : "#tab=" + nextTab;
        window.history.replaceState(null, "", window.location.pathname + window.location.search + newHash);
      });
    });

    adminRoot.querySelectorAll("[data-audit-jump]").forEach(function (link) {
      if (link.dataset.bound) return;
      link.dataset.bound = "1";
      link.addEventListener("click", function () {
        setActiveAdminTab("audit");
        var auditSec = document.getElementById("admin-audit-section");
        if (auditSec) auditSec.scrollIntoView({ block: "start" });
      });
    });

    // Discord ID paste cleanup (e.g. "<@!123456789012345678>")
    var wlIdInput = document.getElementById("wl-id");
    if (wlIdInput && !wlIdInput.dataset.cleanBound) {
      wlIdInput.dataset.cleanBound = "1";
      wlIdInput.addEventListener("input", function () {
        var m = wlIdInput.value.match(/\b([0-9]{15,22})\b/);
        if (m && wlIdInput.value.trim() !== m[1]) {
          wlIdInput.value = m[1];
        }
      });
    }

    // Inline Whitelist edit mode
    var wlNoteInput = document.getElementById("wl-note");
    var wlRoleSelect = document.getElementById("wl-role");
    var wlSubmitBtn = document.getElementById("admin-wl-submit");
    var wlEditBanner = document.getElementById("admin-wl-edit-banner");
    var wlEditIdLabel = document.getElementById("admin-wl-edit-id");
    var wlEditCancel = document.getElementById("admin-wl-edit-cancel");

    function resetWlEditMode() {
      if (wlIdInput) wlIdInput.value = "";
      if (wlNoteInput) wlNoteInput.value = "";
      if (wlRoleSelect) wlRoleSelect.value = "viewer";
      if (wlSubmitBtn) wlSubmitBtn.textContent = "Выдать доступ";
      if (wlEditBanner) wlEditBanner.hidden = true;
    }

    adminRoot.querySelectorAll(".js-wl-edit").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        var did = btn.dataset.wlId || "";
        var dname = btn.dataset.wlName || "";
        var drole = btn.dataset.wlRole || "viewer";
        if (wlIdInput) wlIdInput.value = did;
        if (wlNoteInput) wlNoteInput.value = dname;
        if (wlRoleSelect && (drole === "viewer" || drole === "admin")) wlRoleSelect.value = drole;
        if (wlEditIdLabel) wlEditIdLabel.textContent = did;
        if (wlEditBanner) wlEditBanner.hidden = false;
        if (wlSubmitBtn) wlSubmitBtn.textContent = "Сохранить";
        if (wlNoteInput) {
          wlNoteInput.focus();
          wlNoteInput.select();
        }
      });
    });

    if (wlEditCancel && !wlEditCancel.dataset.bound) {
      wlEditCancel.dataset.bound = "1";
      wlEditCancel.addEventListener("click", resetWlEditMode);
    }

    // Client-side whitelist search & role pills
    var wlSearch = document.getElementById("admin-wl-search");
    var wlRolePills = adminRoot.querySelectorAll("[data-wl-role-filter]");
    var activeWlRole = "";

    function applyWlFilter() {
      var q = wlSearch ? wlSearch.value.trim().toLowerCase() : "";
      adminRoot.querySelectorAll(".admin-wl-row").forEach(function (row) {
        var rowRole = row.dataset.wlRole || "";
        var rowText = row.dataset.wlSearch || "";
        var roleOk = !activeWlRole || rowRole === activeWlRole;
        var textOk = !q || rowText.indexOf(q) !== -1;
        row.hidden = !(roleOk && textOk);
      });
    }

    if (wlSearch && !wlSearch.dataset.bound) {
      wlSearch.dataset.bound = "1";
      wlSearch.addEventListener("input", applyWlFilter);
    }

    wlRolePills.forEach(function (pill) {
      if (pill.dataset.bound) return;
      pill.dataset.bound = "1";
      pill.addEventListener("click", function () {
        activeWlRole = pill.dataset.wlRoleFilter || "";
        wlRolePills.forEach(function (p) {
          p.classList.toggle("is-active", p === pill);
        });
        applyWlFilter();
      });
    });
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

  function initQuickDateChips() {
    var form = document.getElementById("logs-rounds-filter-form");
    var dateSelect = document.getElementById("logs-rounds-date");
    var sourceSelect = document.getElementById("logs-rounds-source");
    var qInput = document.getElementById("logs-rounds-q");
    var resetLink = document.getElementById("logs-rounds-reset");
    var chips = document.querySelectorAll("[data-quick-date]");
    if (!form || !dateSelect) return;

    function syncResetAndChips() {
      var curDate = dateSelect.value || "";
      chips.forEach(function (ch) {
        var active = (ch.dataset.quickDate || "") === curDate;
        ch.classList.toggle("is-active", active);
        ch.setAttribute("aria-pressed", String(active));
      });
      if (resetLink) {
        var hasFilter = Boolean(curDate || (sourceSelect && sourceSelect.value) || (qInput && qInput.value.trim()));
        resetLink.hidden = !hasFilter;
      }
    }

    function submitRoundsDirectory() {
      syncResetAndChips();
      var container = document.getElementById("logs-rounds-container");
      if (container && window.htmx && typeof window.htmx.ajax === "function") {
        var params = buildCleanFilterParams(form);
        var qs = params.toString();
        var action = form.getAttribute("action") || "/logs";
        var url = action + (qs ? "?" + qs : "");
        window.history.pushState({}, "", url);
        window.htmx.ajax("GET", url, { target: "#logs-rounds-container", swap: "innerHTML" });
      } else {
        form.submit();
      }
    }

    chips.forEach(function (chip) {
      if (chip.dataset.bound) return;
      chip.dataset.bound = "1";
      chip.addEventListener("click", function () {
        var val = chip.dataset.quickDate || "";
        if (val && !Array.prototype.some.call(dateSelect.options, function (o) { return o.value === val; })) {
          var opt = document.createElement("option");
          opt.value = val;
          opt.textContent = val;
          dateSelect.appendChild(opt);
        }
        dateSelect.value = val;
        submitRoundsDirectory();
      });
    });

    if (!form.dataset.chipSyncBound) {
      form.dataset.chipSyncBound = "1";
      var qDebounce = null;
      form.addEventListener("submit", function (ev) {
        var container = document.getElementById("logs-rounds-container");
        if (container && window.htmx && typeof window.htmx.ajax === "function") {
          ev.preventDefault();
          submitRoundsDirectory();
        }
      });
      dateSelect.addEventListener("change", submitRoundsDirectory);
      if (sourceSelect) sourceSelect.addEventListener("change", submitRoundsDirectory);
      if (qInput) {
        qInput.addEventListener("input", function () {
          syncResetAndChips();
          clearTimeout(qDebounce);
          qDebounce = setTimeout(submitRoundsDirectory, 280);
        });
      }
    }
    syncResetAndChips();
  }

  function bindStreamHelpers() {
    initRoundFilterHelpers();
    initContextModal();
    initCopyButtons();
    scrollFocusedLine();
  }

  function initAll() {
    initLogsBootOverlay();
    initDragAndDropUpload();
    initQuickDateChips();
    initRoundMapViewer();
    bindStreamHelpers();
    initKeyboardShortcuts();
    initAdminMapBuildPolling();
    initAdminDashboard();

    document.body.addEventListener("htmx:afterSwap", function (ev) {
      var target = ev.detail && ev.detail.target;
      if (!target) return;
      if (target.id === "logs-stream-container") {
        bindStreamHelpers();
      } else if (target.id === "admin-audit-container") {
        initAdminAuditHelpers();
      } else if (target.id === "logs-rounds-container") {
        initQuickDateChips();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", initAll);
})();
