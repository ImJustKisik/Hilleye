/* Hilleye // SierraBay12 Log Viewer client script (CSP-safe external script) */
(function () {
  "use strict";

  function initDragAndDropUpload() {
    var dropzone = document.getElementById("logs-dropzone");
    var fileInput = document.getElementById("logs-upload-file");
    var form = document.getElementById("logs-upload-form");
    var statusBox = document.getElementById("logs-upload-status");
    if (!dropzone || !fileInput || !form || dropzone.dataset.bound) return;
    dropzone.dataset.bound = "1";

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

  function initRoundFilterHelpers() {
    var filterForm = document.getElementById("logs-filter-form");
    var ckeyInput = document.getElementById("logs-ckey-input");
    var presetField = document.getElementById("logs-preset-input");

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
        filterForm.submit();
      });
    });

    // Category chip toggle (native <label> toggles checkbox once; submit on change)
    filterForm.querySelectorAll('input[name="cat"]').forEach(function (cb) {
      if (cb.dataset.bound) return;
      cb.dataset.bound = "1";
      cb.addEventListener("change", function () {
        var pageInput = filterForm.querySelector('input[name="page"]');
        if (pageInput) pageInput.value = "1";
        filterForm.submit();
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
        filterForm.submit();
      });
    });
  }

  function initContextModal() {
    var modal = document.getElementById("log-context-modal");
    var modalBody = document.getElementById("log-context-modal-body");
    var modalTitle = document.getElementById("log-context-modal-title");
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
      document.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape" && !modal.hidden) closeModal();
      });
    }

    document.querySelectorAll("[data-context-url]").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        var url = btn.dataset.contextUrl;
        var lineNo = btn.dataset.lineNo || "";
        if (!url) return;
        if (modalTitle) {
          modalTitle.textContent = "Контекст вокруг строки #L" + lineNo;
        }
        modalBody.innerHTML = '<div class="logs-panel-body">Загрузка контекста...</div>';
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
      });
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

  function initAll() {
    initDragAndDropUpload();
    initRoundFilterHelpers();
    initContextModal();
    initCopyButtons();
    scrollFocusedLine();
  }

  document.addEventListener("DOMContentLoaded", initAll);
})();
