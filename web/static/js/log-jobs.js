/* Shared job polling and freshness hints. No inline script or HTML interpolation. */
(function () {
  "use strict";
  var indexBox = document.getElementById("logs-index-status");
  var jobBox = document.getElementById("logs-job-status");
  if (!indexBox || !jobBox) return;
  var key = "hilleye-jobs:" + indexBox.dataset.userId;
  var jobs = [];
  try { jobs = JSON.parse(sessionStorage.getItem(key) || "[]"); } catch (_) { jobs = []; }
  if (!Array.isArray(jobs)) jobs = [];
  var reloading = false;
  function save() { try { sessionStorage.setItem(key, JSON.stringify(jobs.slice(-10))); } catch (_) {} }
  function show(message, error) {
    jobBox.hidden = false;
    jobBox.className = "logs-alert " + (error ? "logs-alert-warn" : "logs-alert-info");
    jobBox.textContent = message;
  }
  async function readJson(response) {
    var body = await response.json();
    if (!response.ok) throw new Error(body.detail || ("HTTP " + response.status));
    return body;
  }
  document.addEventListener("submit", async function (event) {
    var form = event.target;
    var path = new URL(form.action, location.href).pathname;
    if (!["/logs/refresh", "/logs/reindex", "/logs/admin/maps/rebuild"].includes(path)) return;
    event.preventDefault();
    var button = form.querySelector('button[type="submit"]');
    var originalLabel = button ? button.textContent : "";
    var data = new FormData(form);
    data.set("background", "1");
    try {
      var result = await readJson(await fetch(form.action, {
        method: "POST", body: data, credentials: "same-origin", headers: {"Accept": "application/json"}
      }));
      if (result.job_id && !jobs.includes(result.job_id)) jobs.push(result.job_id);
      save();
      show("Задача поставлена в очередь. Можно продолжать работу.", false);
      pollJobs();
    } catch (error) { show(error.message, true); }
    finally { if (button) { button.disabled = false; button.textContent = originalLabel; } }
  }, true);
  async function pollJobs() {
    for (var id of jobs.slice()) {
      try {
        var response = await fetch("/api/v1/logs/jobs/" + encodeURIComponent(id), {credentials: "same-origin", cache: "no-store"});
        if (response.status === 404) { jobs = jobs.filter(function (item) { return item !== id; }); save(); continue; }
        var job = await readJson(response);
        if (job.state === "succeeded" || job.state === "failed") {
          jobs = jobs.filter(function (item) { return item !== id; }); save();
          if (job.state === "failed") show(job.error || "Задача завершилась с ошибкой.", true);
          else if (!reloading) { reloading = true; location.reload(); }
        } else show(job.progress || "Задача ожидает выполнения.", false);
      } catch (error) { show("Не удалось получить статус задачи. Повторим запрос.", true); }
    }
  }
  async function pollIndex() {
    try {
      var status = await readJson(await fetch("/api/v1/logs/status", {credentials: "same-origin", cache: "no-store"}));
      var dates = (status.sources || []).map(function (source) { return source.last_success_at || 0; }).filter(Boolean);
      var last = dates.length ? new Date(Math.min.apply(null, dates) * 1000).toLocaleString() : "ещё не обновлялся";
      var busy = status.queued || (status.running && status.running.length);
      indexBox.hidden = false;
      indexBox.className = "logs-alert " + (status.status === "degraded" ? "logs-alert-warn" : "logs-alert-info");
      indexBox.textContent = "Индекс: " + last + ". " +
        (status.status === "degraded" ? "Источник недоступен или обработан с ошибкой; результаты могут быть неполными. " : "") +
        (status.pending_bytes ? "Есть ещё не проиндексированные данные. " : "") +
        (busy ? "Фоновые задачи выполняются или ожидают запуска." : "");
    } catch (_) {
      indexBox.hidden = false;
      indexBox.textContent = "Не удалось проверить свежесть индекса.";
    }
  }
  document.addEventListener("htmx:beforeSwap", function (event) {
    if ([408, 429].includes(event.detail.xhr.status)) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
  pollJobs(); pollIndex();
  setInterval(pollJobs, 2000);
  setInterval(pollIndex, 5000);
})();
