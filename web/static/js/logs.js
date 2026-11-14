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

  function syncToolbarActiveStates(filterForm) {
    if (!filterForm) return;
    var presetField = document.getElementById("logs-preset-input");
    var anyCatChecked = false;
    filterForm.querySelectorAll('input[name="cat"]').forEach(function (cb) {
      var chip = cb.closest(".logs-cat-chip");
      if (chip) {
        chip.classList.toggle("is-selected", cb.checked);
      }
      if (cb.checked) anyCatChecked = true;
    });
    var activePreset = presetField ? presetField.value : "";
    document.querySelectorAll("[data-preset-id]").forEach(function (btn) {
      btn.classList.toggle("is-active", !anyCatChecked && btn.dataset.presetId === activePreset);
    });
  }

  function submitFilterForm(filterForm) {
    if (!filterForm) return;
    syncToolbarActiveStates(filterForm);
    var streamContainer = document.getElementById("logs-stream-container");
    if (streamContainer && window.htmx && typeof window.htmx.ajax === "function") {
      var params = new URLSearchParams(new FormData(filterForm));
      var action = filterForm.getAttribute("action") || window.location.pathname;
      var url = action + "?" + params.toString();
      window.history.pushState({}, "", url);
      window.htmx.ajax("GET", url, { target: "#logs-stream-container", swap: "innerHTML" });
      return;
    }
    filterForm.submit();
  }

  function initRoundFilterHelpers() {
    var filterForm = document.getElementById("logs-filter-form");
    var ckeyInput = document.getElementById("logs-ckey-input");
    var presetField = document.getElementById("logs-preset-input");

    if (filterForm && !filterForm.dataset.htmxBound) {
      filterForm.dataset.htmxBound = "1";
      filterForm.addEventListener("submit", function (ev) {
        var streamContainer = document.getElementById("logs-stream-container");
        if (streamContainer && window.htmx) {
          ev.preventDefault();
          var pageInput = filterForm.querySelector('input[name="page"]');
          if (pageInput) pageInput.value = "1";
          submitFilterForm(filterForm);
        }
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

    var fetchUrl = url + (url.indexOf("?") === -1 ? "?" : "&") + "inline=1";
    fetch(fetchUrl, { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (html) {
        drawer.innerHTML = html;
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
    if (!modal || !modalBody) return;

    function closeModal() {
      modal.hidden = true;
      modalBody.innerHTML = "";
    }

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
          document.getElementById("qdel-q");
        if (searchInput) {
          ev.preventDefault();
          searchInput.focus();
          searchInput.select();
        }
        return;
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
    if (cat === "GAME" || cat === "EMOTE" || cat === "VOTE") return "#c084fc";
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

  function initRoundMapViewer() {
    var root = document.getElementById("logs-map-root");
    var canvas = document.getElementById("logs-map-canvas");
    if (!root || !canvas || !canvas.getContext) return;

    var ctx = canvas.getContext("2d");
    var deckTabs = document.getElementById("logs-map-deck-tabs");
    var statusEl = document.getElementById("logs-map-status");
    var hudCoords = document.getElementById("logs-map-hud-coords");
    var hudArea = document.getElementById("logs-map-hud-area");
    var zoomLabel = document.getElementById("logs-map-zoom-label");
    var modePinsBtn = document.getElementById("logs-map-mode-pins");
    var modeHeatBtn = document.getElementById("logs-map-mode-heat");
    var clusterCb = document.getElementById("logs-map-cluster-cb");
    var trajCb = document.getElementById("logs-map-traj-cb");
    var zoomInBtn = document.getElementById("logs-map-zoom-in");
    var zoomOutBtn = document.getElementById("logs-map-zoom-out");
    var fitBtn = document.getElementById("logs-map-fit");
    var centerBtn = document.getElementById("logs-map-center-target");
    var inspectorTitle = document.getElementById("logs-map-inspector-title");
    var inspectorSub = document.getElementById("logs-map-inspector-sub");
    var inspectorList = document.getElementById("logs-map-inspector-list");

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
    var filterCkey = (ds.ckey || "").toLowerCase();
    var defaultTilePx = Number(ds.tilePx) || 16;

    var state = {
      manifest: null,
      levelsByZ: {},
      events: [],
      zCounts: {},
      currentZ: targetZ || 1,
      mode: "pins",
      clusterEnabled: clusterCb ? clusterCb.checked : true,
      trajEnabled: trajCb ? trajCb.checked : Boolean(filterCkey),
      scale: 0.25,
      offsetX: 0,
      offsetY: 0,
      cssW: 900,
      cssH: 620,
      dpr: window.devicePixelRatio || 1,
      hoverTile: null,
      selectedLineNos: {},
      selectedTile: (targetX && targetY) ? { x: targetX, y: targetY, z: targetZ || 1 } : null,
      renderedPins: [],
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

    function loadAssetsForZ(z) {
      var key = String(z);
      var lv = getLevelMeta(z);
      if (lv.image_file && !state.imageCache[key]) {
        var img = new Image();
        var rec = { status: "loading", img: img };
        state.imageCache[key] = rec;
        img.onload = function () {
          rec.status = "loaded";
          requestDraw();
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

    function fitView() {
      var lv = getLevelMeta(state.currentZ);
      var imgW = lv.width * lv.tile_px;
      var imgH = lv.height * lv.tile_px;
      var pad = 20;
      var availW = Math.max(100, state.cssW - pad * 2);
      var availH = Math.max(100, state.cssH - pad * 2);
      var s = Math.min(availW / imgW, availH / imgH);
      state.scale = Math.max(0.08, Math.min(4.0, s));
      state.offsetX = (state.cssW - imgW * state.scale) / 2;
      state.offsetY = (state.cssH - imgH * state.scale) / 2;
      updateZoomLabel();
      requestDraw();
    }

    function centerOnTile(tx, ty, desiredScale) {
      var lv = getLevelMeta(state.currentZ);
      if (desiredScale) {
        state.scale = Math.max(0.08, Math.min(6.0, desiredScale));
      } else if (state.scale < 0.7) {
        state.scale = 1.25;
      }
      var ix = (tx - 0.5) * lv.tile_px;
      var iy = (lv.height - ty + 0.5) * lv.tile_px;
      state.offsetX = state.cssW / 2 - ix * state.scale;
      state.offsetY = state.cssH / 2 - iy * state.scale;
      updateZoomLabel();
      requestDraw();
    }

    function zoomAt(factor, cx, cy) {
      var oldScale = state.scale;
      var newScale = Math.max(0.08, Math.min(6.0, oldScale * factor));
      if (Math.abs(newScale - oldScale) < 1e-5) return;
      var ratio = newScale / oldScale;
      state.offsetX = cx - (cx - state.offsetX) * ratio;
      state.offsetY = cy - (cy - state.offsetY) * ratio;
      state.scale = newScale;
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

    function renderInspector(eventsList, title, subtitle) {
      if (!inspectorList || !inspectorTitle || !inspectorSub) return;
      inspectorTitle.textContent = title;
      inspectorSub.textContent = subtitle;
      inspectorList.innerHTML = "";
      state.selectedLineNos = {};

      if (!eventsList || eventsList.length === 0) {
        var empty = document.createElement("div");
        empty.className = "block-empty";
        empty.textContent = "На этом месте нет событий, подходящих под текущие фильтры.";
        inspectorList.appendChild(empty);
        return;
      }

      var slice = eventsList.slice(0, 120);
      for (var i = 0; i < slice.length; i++) {
        var ev = slice[i];
        state.selectedLineNos[String(ev.line_no)] = true;

        var card = document.createElement("div");
        card.className = "logs-map-event-card" + (focusLine && ev.line_no === focusLine ? " is-focused" : "");

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
        catSpan.className = "log-cat-tag " + (ev.cat_css || "cat-default");
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
        (function (btn) {
          btn.addEventListener("click", function () {
            openLogContextModal(btn.dataset.contextUrl, btn.dataset.lineNo);
          });
        })(ctxBtn);
        actionsRow.appendChild(ctxBtn);

        if (Array.isArray(ev.ckeys) && ev.ckeys.length > 0) {
          for (var k = 0; k < ev.ckeys.length; k++) {
            var ck = ev.ckeys[k];
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
                  filterForm.submit();
                }
              });
            })(ck);
            actionsRow.appendChild(ckBtn);
          }
        }

        card.appendChild(actionsRow);
        inspectorList.appendChild(card);
      }

      if (eventsList.length > slice.length) {
        var moreNote = document.createElement("div");
        moreNote.className = "cell-sub mono-val";
        moreNote.textContent = "Показаны первые " + slice.length + " из " + eventsList.length + " событий.";
        inspectorList.appendChild(moreNote);
      }
      requestDraw();
    }

    function currentZEvents() {
      var out = [];
      for (var i = 0; i < state.events.length; i++) {
        if (state.events[i].z === state.currentZ) {
          out.push(state.events[i]);
        }
      }
      return out;
    }

    function buildDeckTabs() {
      if (!deckTabs) return;
      deckTabs.innerHTML = "";
      var zList = [];
      var seen = {};
      if (state.manifest && Array.isArray(state.manifest.levels)) {
        for (var i = 0; i < state.manifest.levels.length; i++) {
          var lv = state.manifest.levels[i];
          zList.push(lv.z);
          seen[String(lv.z)] = true;
        }
      }
      var extraZs = Object.keys(state.zCounts)
        .map(function (k) { return Number(k); })
        .filter(function (z) { return !seen[String(z)]; })
        .sort(function (a, b) { return a - b; });
      zList = zList.concat(extraZs);
      if (zList.length === 0) zList = [1, 2, 3, 4, 5, 6, 7];

      for (var idx = 0; idx < zList.length; idx++) {
        var z = zList[idx];
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
    }

    function selectDeck(z, centerTarget) {
      state.currentZ = z;
      loadAssetsForZ(z);
      buildDeckTabs();
      fitView();
      if (centerTarget && targetX && targetY && targetZ === z) {
        centerOnTile(targetX, targetY, 1.5);
      }
      if (state.hoverTile) {
        updateHud(state.hoverTile.x, state.hoverTile.y);
      } else {
        updateHud(null, null);
      }
      requestDraw();
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
        var rHeat = Math.max(12, lv.tile_px * state.scale * 2.2);
        for (var hi = 0; hi < zEvents.length; hi++) {
          var hev = zEvents[hi];
          var hpt = tileToScreen(hev.x, hev.y, lv);
          if (hpt.x < -rHeat || hpt.x > w + rHeat || hpt.y < -rHeat || hpt.y > h + rHeat) continue;
          var grad = ctx.createRadialGradient(hpt.x, hpt.y, 1, hpt.x, hpt.y, rHeat);
          grad.addColorStop(0, "rgba(217, 38, 56, 0.42)");
          grad.addColorStop(0.5, "rgba(245, 158, 11, 0.18)");
          grad.addColorStop(1, "rgba(245, 158, 11, 0)");
          ctx.fillStyle = grad;
          ctx.beginPath();
          ctx.arc(hpt.x, hpt.y, rHeat, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // 3. Trajectory Path
      if (state.trajEnabled && zEvents.length > 1) {
        var trajEvents = [];
        if (filterCkey) {
          for (var ti = 0; ti < zEvents.length; ti++) {
            var tev = zEvents[ti];
            if (Array.isArray(tev.ckeys) && tev.ckeys.indexOf(filterCkey) !== -1) {
              trajEvents.push(tev);
            }
          }
          if (trajEvents.length === 0) trajEvents = zEvents;
        } else {
          trajEvents = zEvents;
        }

        if (trajEvents.length > 1) {
          ctx.strokeStyle = "rgba(96, 165, 250, 0.85)";
          ctx.lineWidth = 2;
          ctx.beginPath();
          var prevPt = null;
          var stepIdx = 0;
          for (var tj = 0; tj < trajEvents.length; tj++) {
            var tItem = trajEvents[tj];
            var curPt = tileToScreen(tItem.x, tItem.y, lv);
            if (!prevPt) {
              ctx.moveTo(curPt.x, curPt.y);
            } else {
              if (Math.hypot(curPt.x - prevPt.x, curPt.y - prevPt.y) > 4) {
                ctx.lineTo(curPt.x, curPt.y);
              }
            }
            prevPt = curPt;
          }
          ctx.stroke();

          // Direction arrows & start/end markers
          for (var tk = 1; tk < trajEvents.length; tk++) {
            var aPt = tileToScreen(trajEvents[tk - 1].x, trajEvents[tk - 1].y, lv);
            var bPt = tileToScreen(trajEvents[tk].x, trajEvents[tk].y, lv);
            var dist = Math.hypot(bPt.x - aPt.x, bPt.y - aPt.y);
            if (dist >= 18) {
              var mx = (aPt.x + bPt.x) / 2;
              var my = (aPt.y + bPt.y) / 2;
              var ang = Math.atan2(bPt.y - aPt.y, bPt.x - aPt.x);
              ctx.fillStyle = "#60a5fa";
              ctx.beginPath();
              ctx.moveTo(mx + Math.cos(ang) * 5, my + Math.sin(ang) * 5);
              ctx.lineTo(mx + Math.cos(ang + 2.5) * 5, my + Math.sin(ang + 2.5) * 5);
              ctx.lineTo(mx + Math.cos(ang - 2.5) * 5, my + Math.sin(ang - 2.5) * 5);
              ctx.closePath();
              ctx.fill();
            }
            stepIdx++;
          }
          if (trajEvents.length <= 40 && stepIdx > 0) {
            ctx.font = "600 10px 'JetBrains Mono', Consolas, monospace";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            for (var ts = 0; ts < trajEvents.length; ts++) {
              var spt = tileToScreen(trajEvents[ts].x, trajEvents[ts].y, lv);
              ctx.fillStyle = "#12161c";
              ctx.fillRect(spt.x - 8, spt.y - 16, 16, 12);
              ctx.fillStyle = "#eef2f6";
              ctx.fillText(String(ts + 1), spt.x, spt.y - 10);
            }
          }
        }
      }

      // 4. Pins / Clusters
      var renderedPins = [];
      if (state.mode === "pins" || state.mode === "heat") {
        // First group exact same tile
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
          var radius = cnt > 1 ? Math.min(16, 8 + Math.log2(cnt) * 2.2) : (state.mode === "heat" ? 4 : 5.5);

          if (item.hasFocus) {
            ctx.strokeStyle = "#d92638";
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            ctx.arc(item.sx, item.sy, radius + 6, 0, Math.PI * 2);
            ctx.stroke();
          }

          ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
          ctx.beginPath();
          ctx.arc(item.sx + 1, item.sy + 1, radius + 1.5, 0, Math.PI * 2);
          ctx.fill();

          ctx.fillStyle = color;
          ctx.strokeStyle = "#eef2f6";
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

          renderedPins.push({
            sx: item.sx,
            sy: item.sy,
            radius: Math.max(radius + 4, 12),
            x: item.x,
            y: item.y,
            events: item.events,
          });
        }
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

      if (state.hoverTile) {
        var ht = state.hoverTile;
        var htX = x0 + (ht.x - 1) * lv.tile_px * state.scale;
        var htY = y0 + (lv.height - ht.y) * lv.tile_px * state.scale;
        var htSize = lv.tile_px * state.scale;
        ctx.strokeStyle = "rgba(255, 255, 255, 0.7)";
        ctx.lineWidth = 1;
        ctx.strokeRect(htX, htY, htSize, htSize);
      }
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
          centerOnTile(targetX, targetY, 1.5);
        }
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
      state.dragStartX = ev.clientX;
      state.dragStartY = ev.clientY;
      state.dragOrigOffsetX = state.offsetX;
      state.dragOrigOffsetY = state.offsetY;
      canvas.classList.add("is-dragging");
    });

    canvas.addEventListener("mouseleave", function () {
      if (state.hoverTile) {
        state.hoverTile = null;
        updateHud(null, null);
        requestDraw();
      }
    });

    window.addEventListener("mousemove", function (ev) {
      if (state.dragging) {
        var dx = ev.clientX - state.dragStartX;
        var dy = ev.clientY - state.dragStartY;
        if (Math.abs(dx) > 4 || Math.abs(dy) > 4) {
          state.dragMoved = true;
        }
        state.offsetX = state.dragOrigOffsetX + dx;
        state.offsetY = state.dragOrigOffsetY + dy;
        requestDraw();
      }
      var rect = canvas.getBoundingClientRect();
      if (
        ev.clientX >= rect.left &&
        ev.clientX <= rect.right &&
        ev.clientY >= rect.top &&
        ev.clientY <= rect.bottom
      ) {
        var lv = getLevelMeta(state.currentZ);
        var tile = screenToTile(ev.clientX - rect.left, ev.clientY - rect.top, lv);
        var prev = state.hoverTile;
        var changed =
          (!prev && tile) ||
          (prev && !tile) ||
          (prev && tile && (prev.x !== tile.x || prev.y !== tile.y));
        if (changed) {
          state.hoverTile = tile;
          updateHud(tile ? tile.x : null, tile ? tile.y : null);
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

      // Hit test rendered pins/clusters first
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
      if (best) {
        state.selectedTile = { x: best.x, y: best.y, z: state.currentZ };
        var title = best.events.length === 1
          ? ("Событие #L" + best.events[0].line_no)
          : ("Кластер (" + best.events.length + " событий)");
        var area = lookupArea(state.currentZ, best.x, best.y) || best.events[0].area || "";
        var sub = "X:" + best.x + " Y:" + best.y + " Z:" + state.currentZ + (area ? " \u2022 " + area : "");
        renderInspector(best.events, title, sub);
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
        renderInspector(tileEvents, "Тайл (" + tile.x + ", " + tile.y + ", Z" + state.currentZ + ")", tArea);
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
        state.manifest = data.manifest || { ready: false, levels: [] };
        state.levelsByZ = {};
        if (Array.isArray(state.manifest.levels)) {
          for (var i = 0; i < state.manifest.levels.length; i++) {
            var lv = state.manifest.levels[i];
            state.levelsByZ[String(lv.z)] = lv;
          }
        }
        state.events = Array.isArray(data.events) ? data.events : [];
        state.zCounts = data.z_counts || {};

        if (!targetZ) {
          var bestZ = 1;
          var bestCnt = -1;
          var keys = Object.keys(state.zCounts);
          for (var k = 0; k < keys.length; k++) {
            var c = Number(state.zCounts[keys[k]]) || 0;
            if (c > bestCnt) {
              bestCnt = c;
              bestZ = Number(keys[k]) || 1;
            }
          }
          if (bestCnt <= 0 && state.manifest.levels && state.manifest.levels.length > 0) {
            bestZ = state.manifest.levels[0].z;
          }
          state.currentZ = bestZ;
        } else {
          state.currentZ = targetZ;
        }

        if (statusEl) {
          var totalCoords = Number(data.total_with_coords) || state.events.length;
          var msg = "Событий с координатами: " + totalCoords;
          if (data.truncated) {
            msg += " (показаны первые " + state.events.length + " — уточните фильтры)";
          }
          statusEl.textContent = msg;
        }

        loadAssetsForZ(state.currentZ);
        buildDeckTabs();
        syncCanvasSize();
        fitView();
        if (targetX && targetY && (!targetZ || targetZ === state.currentZ)) {
          state.selectedTile = { x: targetX, y: targetY, z: state.currentZ };
          centerOnTile(targetX, targetY, 1.5);
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
            focusLine ? ("Фокус #L" + focusLine) : ("Точка (" + targetX + ", " + targetY + ", Z" + state.currentZ + ")"),
            "X:" + targetX + " Y:" + targetY + " Z:" + state.currentZ + (fArea ? " \u2022 " + fArea : "")
          );
        }
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
          window.location.replace("/logs/admin");
        })
        .catch(function () {
          // Keep polling quietly on transient error
        });
    }, 2000);
  }

  function bindStreamHelpers() {
    initRoundFilterHelpers();
    initContextModal();
    initCopyButtons();
    scrollFocusedLine();
  }

  function initAll() {
    initDragAndDropUpload();
    bindStreamHelpers();
    initKeyboardShortcuts();
    initRoundMapViewer();
    initAdminMapBuildPolling();

    document.body.addEventListener("htmx:afterSwap", function (ev) {
      if (ev.detail && ev.detail.target && ev.detail.target.id === "logs-stream-container") {
        bindStreamHelpers();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", initAll);
})();
