(function () {
  "use strict";

  var initCopyButtons;

  function initAdminMapBuildPolling() {
    var banner = document.getElementById("logs-map-build-status");
    if (!banner || banner.dataset.mapBuilding !== "1") return;
    var detailEl = document.getElementById("logs-map-build-detail");
    async function poll() {
      try {
        var response = await fetch("/logs/maps/manifest", { credentials: "same-origin" });
        if (response.ok) {
          var data = await response.json();
          var status = data && data.build_status;
          if (status && status.state !== "running") {
            window.location.reload();
            return;
          }
          if (status && detailEl && status.detail) detailEl.textContent = status.detail;
        }
      } catch (_) {
        // Retry after the current request has settled.
      }
      setTimeout(poll, 2000);
    }
    setTimeout(poll, 2000);
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

  window.HilleyeLogAdmin = {
    init: function (copyButtons) {
      initCopyButtons = copyButtons;
      initAdminMapBuildPolling();
      initAdminDashboard();
      document.body.addEventListener("htmx:afterSwap", function (ev) {
        var target = ev.detail && ev.detail.target;
        if (target && target.id === "admin-audit-container") initAdminAuditHelpers();
      });
    }
  };
})();
