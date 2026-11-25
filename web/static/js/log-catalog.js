(function () {
  "use strict";

  var initialized = new WeakSet();

  function initDragAndDropUpload() {
    var dropzone = document.getElementById("logs-dropzone");
    var fileInput = document.getElementById("logs-upload-file");
    var form = document.getElementById("logs-upload-form");
    var statusBox = document.getElementById("logs-upload-status");
    var dzTitle = document.getElementById("logs-dropzone-title");
    var dzSub = document.getElementById("logs-dropzone-sub");
    if (!dropzone || !fileInput || !form || initialized.has(form)) return;
    initialized.add(form);

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

  function initRoundsFilters() {
    var form = document.getElementById("logs-rounds-filter-form");
    if (!form || initialized.has(form)) return;
    initialized.add(form);

    var indexDate = document.getElementById("logs-rounds-index-date");
    var calendarDate = document.getElementById("logs-rounds-calendar-date");
    var sourceSelect = document.getElementById("logs-rounds-source");
    var qInput = document.getElementById("logs-rounds-q");
    var resetLink = document.getElementById("logs-rounds-reset");
    var queryNote = document.getElementById("logs-rounds-query-note");
    var chips = form.parentElement.querySelectorAll("[data-quick-date]");
    var appliedDate = form.dataset.appliedDate;
    var appliedQuery = qInput.value;

    function activeDateField() {
      return calendarDate.disabled ? indexDate : calendarDate;
    }

    function setDate(date) {
      var field = activeDateField();
      if (field === indexDate && date && !Array.from(indexDate.options).some(function (option) { return option.value === date; })) {
        indexDate.add(new Option(date, date));
      }
      field.value = date;
    }

    function syncDateField(date) {
      var celadon = sourceSelect.value === "celadon";
      indexDate.closest("[data-rounds-date-field]").hidden = celadon;
      indexDate.disabled = celadon;
      calendarDate.closest("[data-rounds-date-field]").hidden = !celadon;
      calendarDate.disabled = !celadon;
      queryNote.hidden = celadon;
      chips[0].textContent = celadon ? "Последняя дата" : "Все даты";
      setDate(date);
    }

    function syncChips() {
      var sameDateMode = (sourceSelect.value === "celadon") === (form.dataset.roundsSource === "celadon");
      chips.forEach(function (ch) {
        var active = sameDateMode && ch.dataset.quickDate === appliedDate;
        ch.classList.toggle("is-active", active);
        ch.setAttribute("aria-pressed", String(active));
      });
    }

    function syncReset() {
      var hasDraft = activeDateField().value || sourceSelect.value || qInput.value.trim();
      var hasAppliedFilters = appliedDate || form.dataset.roundsSource || appliedQuery;
      resetLink.hidden = !(hasDraft || hasAppliedFilters);
    }

    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        setDate(chip.dataset.quickDate);
        form.requestSubmit();
      });
    });

    form.addEventListener("submit", function (ev) {
      if (form.dataset.roundsSource !== "celadon" && sourceSelect.value !== "celadon" && window.htmx) {
        ev.preventDefault();
        window.htmx.ajax("GET", form.action, { source: form, target: "#logs-rounds-container" });
      }
    });
    form.addEventListener("htmx:afterRequest", function (ev) {
      if (!ev.detail.successful || ev.detail.target.id !== "logs-rounds-container") return;
      var params = new URLSearchParams(window.location.search);
      appliedDate = params.get("date") || "";
      appliedQuery = params.get("q") || "";
      form.dataset.roundsSource = params.get("source") || "";
      syncChips();
      syncReset();
    });
    sourceSelect.addEventListener("change", function () {
      syncDateField(activeDateField().value);
      syncChips();
      syncReset();
    });
    indexDate.addEventListener("change", syncReset);
    calendarDate.addEventListener("change", syncReset);
    qInput.addEventListener("input", syncReset);

    syncChips();
    syncReset();
  }

  window.HilleyeLogCatalog = {
    init: function () {
      function initCatalog() {
        initDragAndDropUpload();
        initRoundsFilters();
      }
      initCatalog();
      document.body.addEventListener("htmx:historyRestore", initCatalog);
    }
  };
})();
