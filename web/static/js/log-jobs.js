/* Shared job polling and freshness hints. No inline script or HTML interpolation. */
(function () {
  "use strict";
  var initialIndexBox = document.getElementById("logs-index-status");
  if (!initialIndexBox || !document.getElementById("logs-job-status")) return;
  var key = "hilleye-jobs:" + initialIndexBox.dataset.userId;
  var jobs = [];
  try { jobs = JSON.parse(sessionStorage.getItem(key) || "[]"); } catch (_) { jobs = []; }
  if (!Array.isArray(jobs)) jobs = [];
  var reloading = false;
  function save() { try { sessionStorage.setItem(key, JSON.stringify(jobs.slice(-10))); } catch (_) {} }
  function show(message, error) {
    var jobBox = document.getElementById("logs-job-status");
    if (!jobBox) return;
    jobBox.hidden = false;
    jobBox.className = "logs-alert " + (error ? "logs-alert-warn" : "logs-alert-info");
    if (jobBox.textContent !== message) jobBox.textContent = message;
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
    var data = new FormData(form);
    data.set("background", "1");
    if (button) button.disabled = true;
    try {
      var result = await readJson(await fetch(form.action, {
        method: "POST", body: data, credentials: "same-origin", headers: {"Accept": "application/json"}
      }));
      if (!result.job_id) {
        if (result.ok === false) show("Индекс обновлён частично. Ошибок: " + result.errors + ".", true);
        else if (!reloading) { reloading = true; location.reload(); }
        return;
      }
      if (!jobs.includes(result.job_id)) jobs.push(result.job_id);
      save();
      show("Задача поставлена в очередь. Можно продолжать работу.", false);
    } catch (error) { show(error.message, true); }
    finally { if (button) button.disabled = false; }
  }, true);
  async function pollJobs() {
    for (var id of jobs.slice()) {
      try {
        var response = await fetch("/api/v1/logs/jobs/" + encodeURIComponent(id), {credentials: "same-origin", cache: "no-store"});
        if (response.status === 404) { jobs = jobs.filter(function (item) { return item !== id; }); save(); continue; }
        var job = await readJson(response);
        if (["succeeded", "partial", "failed"].includes(job.state)) {
          jobs = jobs.filter(function (item) { return item !== id; }); save();
          if (job.state === "failed") show(job.error || "Задача завершилась с ошибкой.", true);
          else if (job.state === "partial") show("Индекс обновлён частично. Ошибок: " + job.result.errors + ".", true);
          else if (!reloading) { reloading = true; location.reload(); }
        } else show(job.progress || "Задача ожидает выполнения.", false);
      } catch (error) { show("Не удалось получить статус задачи. Повторим запрос.", true); }
    }
  }
  function showIndex(freshness, details, warning) {
    var indexBox = document.getElementById("logs-index-status");
    var diagnostics = document.getElementById("logs-index-diagnostics");
    if (!indexBox) return;
    var alert = diagnostics || indexBox;
    var message = diagnostics ? details : [freshness, details].filter(Boolean).join(". ");
    if (diagnostics) {
      indexBox.hidden = !freshness;
      if (indexBox.textContent !== freshness) indexBox.textContent = freshness;
    }
    alert.hidden = !message;
    alert.className = "logs-alert " + (warning ? "logs-alert-warn" : "logs-alert-info");
    if (alert.textContent !== message) alert.textContent = message;
  }
  async function pollIndex() {
    try {
      var status = await readJson(await fetch("/api/v1/logs/status", {credentials: "same-origin", cache: "no-store"}));
      var dates = (status.sources || []).filter(function (source) { return source.status !== "disabled"; })
        .map(function (source) { return source.last_success_at || 0; });
      var last = dates.length && dates.every(Boolean)
        ? new Date(Math.min.apply(null, dates) * 1000).toLocaleString() : "первый проход ещё не завершён";
      var degraded = status.status === "degraded";
      var details = [
        degraded ? "Результаты могут быть неполными из-за ошибки источника." : "",
        status.pending_bytes ? "Есть ещё не проиндексированные данные." : "",
        status.pending_imports ? "Импорты в очереди: " + status.pending_imports + "." : "",
        status.busy ? "Фоновые задачи выполняются или ожидают запуска." : ""
      ].filter(Boolean).join(" ");
      showIndex("Индекс: " + last, details, degraded);
    } catch (_) {
      showIndex("", "Не удалось проверить свежесть индекса.", true);
    }
  }
  document.addEventListener("htmx:beforeSwap", function (event) {
    if ([408, 429].includes(event.detail.xhr.status)) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
  async function poll(fn, interval) {
    try { await fn(); }
    finally { setTimeout(function () { poll(fn, interval); }, interval); }
  }
  poll(pollJobs, 2000);
  poll(pollIndex, 5000);
})();
