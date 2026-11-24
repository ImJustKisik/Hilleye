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

  function initQuickDateChips(buildCleanFilterParams) {
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
      if (!usesCeladon() && container && window.htmx && typeof window.htmx.ajax === "function") {
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

    function usesCeladon() {
      return form.dataset.roundsSource === "celadon" || (sourceSelect && sourceSelect.value === "celadon");
    }

    chips.forEach(function (chip) {
      if (chip.dataset.bound) return;
      chip.dataset.bound = "1";
      chip.addEventListener("click", function () {
        var val = chip.dataset.quickDate || "";
        if (val && dateSelect.options && !Array.prototype.some.call(dateSelect.options, function (o) { return o.value === val; })) {
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
        if (!usesCeladon() && container && window.htmx && typeof window.htmx.ajax === "function") {
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
          if (!usesCeladon()) qDebounce = setTimeout(submitRoundsDirectory, 280);
        });
      }
    }
    syncResetAndChips();
  }

  window.HilleyeLogCatalog = {
    init: function (buildCleanFilterParams) {
      initDragAndDropUpload();
      initQuickDateChips(buildCleanFilterParams);
      document.body.addEventListener("htmx:afterSwap", function (ev) {
        var target = ev.detail && ev.detail.target;
        if (target && target.id === "logs-rounds-container") initQuickDateChips(buildCleanFilterParams);
      });
    }
  };
})();
