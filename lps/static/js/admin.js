/* LocalPrintServer – admin panel interactions & silent background auto-refresh. */
(function () {
  "use strict";
  const { t, h, api, toast, fmtTime, localizeTimes, badge } = window.LPS;

  let adminPollingInterval = null;
  let pollInFlight = false;

  function stopAdminAutoRefresh() {
    if (adminPollingInterval !== null) {
      clearInterval(adminPollingInterval);
      adminPollingInterval = null;
    }
  }

  /* ---- helpers for seamless, non-disruptive DOM updates ---- */
  function hasTextSelectionInside(el) {
    if (!el || !window.getSelection) return false;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim()) return false;
    return (sel.anchorNode && el.contains(sel.anchorNode)) ||
           (sel.focusNode && el.contains(sel.focusNode));
  }

  function hasActiveInteraction(el) {
    if (!el) return false;
    const active = document.activeElement;
    if (active && active !== document.body && el.contains(active)) {
      const tag = active.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || tag === "BUTTON" || active.isContentEditable) {
        return true;
      }
    }
    if (el.querySelector("details[open]")) return true;
    if (el.querySelector("[data-maint-action][disabled]")) return true;
    const inputs = el.querySelectorAll("input, textarea, select");
    for (let i = 0; i < inputs.length; i++) {
      const inp = inputs[i];
      if (inp.type === "hidden") continue;
      if (inp.tagName === "SELECT") {
        const opts = inp.options;
        for (let j = 0; j < opts.length; j++) {
          if (opts[j].selected !== opts[j].defaultSelected) return true;
        }
      } else if (inp.type === "checkbox" || inp.type === "radio") {
        if (inp.checked !== inp.defaultChecked) return true;
      } else if (inp.value !== inp.defaultValue) {
        return true;
      }
    }
    return hasTextSelectionInside(el);
  }

  function softSwapElement(currentEl, nextEl) {
    if (!currentEl || !nextEl) return false;
    if (hasActiveInteraction(currentEl)) return false;
    if (localizeTimes) localizeTimes(nextEl);
    if (currentEl.innerHTML === nextEl.innerHTML) return false;
    const scrollTop = currentEl.scrollTop;
    const scrollLeft = currentEl.scrollLeft;
    currentEl.innerHTML = nextEl.innerHTML;
    if (scrollTop) currentEl.scrollTop = scrollTop;
    if (scrollLeft) currentEl.scrollLeft = scrollLeft;
    return true;
  }

  async function fetchAdminDoc() {
    if (!location.pathname.startsWith("/admin")) {
      stopAdminAutoRefresh();
      return null;
    }
    try {
      const resp = await fetch(location.href, {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "text/html", "X-Requested-With": "XMLHttpRequest" },
        cache: "no-store",
      });
      if (resp.status === 401 || resp.status === 403 || (resp.redirected && resp.url.includes("/login"))) {
        stopAdminAutoRefresh();
        return null;
      }
      if (!resp.ok) return null;
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      if (localizeTimes) localizeTimes(doc);
      return doc;
    } catch (e) {
      return null;
    }
  }

  /* ---- Tab 1: Сводка (Summary / Dashboard) ---- */
  async function refreshHealth() {
    const cpuEl = document.getElementById("health-cpu");
    const ramEl = document.getElementById("health-ram");
    const diskEl = document.getElementById("health-disk");
    if (!cpuEl && !ramEl && !diskEl) return;
    const r = await api("GET", "/admin/api/health");
    if (r.status === 401 || r.status === 403) {
      stopAdminAutoRefresh();
      return;
    }
    if (r.ok && r.data) {
      if (cpuEl && typeof r.data.cpu_percent === "number") cpuEl.textContent = r.data.cpu_percent + "%";
      if (ramEl && typeof r.data.ram_percent === "number") ramEl.textContent = r.data.ram_percent + "%";
      if (diskEl && typeof r.data.disk_free_gb === "number") diskEl.textContent = r.data.disk_free_gb + " ГБ";
    }
  }

  async function loadSummary() {
    await refreshHealth();
    const doc = await fetchAdminDoc();
    if (!doc) return;
    softSwapElement(document.querySelector(".grid-2 > .card:first-child"), doc.querySelector(".grid-2 > .card:first-child"));
    const curCounts = document.querySelector("[data-job-counts-grid]");
    const nextCounts = doc.querySelector("[data-job-counts-grid]");
    if (curCounts && nextCounts) softSwapElement(curCounts, nextCounts);
    const curCards = document.querySelectorAll(".grid-2 > div > section.card");
    const nextCards = doc.querySelectorAll(".grid-2 > div > section.card");
    if (curCards.length === nextCards.length) {
      for (let i = 1; i < curCards.length; i++) {
        if (curCards[i].id === "system-health-card") continue;
        softSwapElement(curCards[i], nextCards[i]);
      }
    }
    const curBanner = document.getElementById("update-banner");
    const nextBanner = doc.getElementById("update-banner");
    if (!curBanner && nextBanner) {
      const grid = document.querySelector(".grid-2");
      if (grid && grid.parentNode) grid.parentNode.insertBefore(nextBanner, grid);
    } else if (curBanner && !nextBanner) {
      curBanner.remove();
    }
  }

  /* ---- Tab 2: Задания (Jobs) ---- */
  async function loadJobs() {
    const modal = document.getElementById("lightbox-modal");
    if (modal && modal.style.display === "flex") return;
    const doc = await fetchAdminDoc();
    if (!doc) return;
    softSwapElement(document.querySelector(".card-head .btn-row"), doc.querySelector(".card-head .btn-row"));
    const curTbody = document.querySelector(".jobs-table tbody");
    const nextTbody = doc.querySelector(".jobs-table tbody");
    if (curTbody && nextTbody) {
      softSwapElement(curTbody, nextTbody);
    } else {
      softSwapElement(document.querySelector("section.card"), doc.querySelector("section.card"));
    }
    softSwapElement(document.querySelector(".pager"), doc.querySelector(".pager"));
  }

  /* ---- Tab 3: Принтер (Printer & Live Queue) ---- */
  const panel = document.querySelector("[data-queue-panel]");
  const CANCELABLE_STATES = ["ACTIVE", "PRINTING", "SPOOLING", "SUBMITTED", "QUEUED", "RENDERING", "WAITING", "RECEIVED", "VALIDATING"];

  async function refreshQueue() {
    if (!panel) {
      if (detectActiveAdminTab() === "admin-jobs") {
        await loadJobs();
      }
      return;
    }
    const r = await api("GET", "/admin/api/queue?scope=all");
    if (r.status === 401 || r.status === 403) {
      stopAdminAutoRefresh();
      return;
    }
    if (r.ok) {
      const appBody = panel.querySelector("[data-app-queue]");
      const winBody = panel.querySelector("[data-win-queue]");
      const d = r.data;
      if (appBody && !hasActiveInteraction(appBody)) {
        const nextApp = document.createElement("tbody");
        nextApp.append(...(d.app_jobs.length ? d.app_jobs.map((j) => h("tr", null,
          h("td", { class: "mono small" }, j.id),
          h("td", null, j.username),
          h("td", { class: "break" }, j.filename, h("div", { class: "muted small" }, fmtTime(j.created_at))),
          h("td", null, badge(j.status), j.status_detail ? h("div", { class: "muted small" }, j.status_detail) : null,
            j.cancel_requested ? h("div", { class: "small warn-text" }, t("jobs.cancel_requested")) : null),
          h("td", null, j.spooler_job_id || "—"),
          h("td", null, (j.cancellable || CANCELABLE_STATES.includes(j.status))
            ? h("button", { class: "btn btn-sm btn-danger", type: "button",
                "data-cancel-job-id": j.id }, "Отменить")
            : null)))
          : [h("tr", null, h("td", { colspan: 6, class: "muted" }, d.app_paused ? t("maint.app_queue_empty_paused") : t("maint.app_queue_empty")))]));
        if (appBody.innerHTML !== nextApp.innerHTML) appBody.replaceChildren(...nextApp.childNodes);
      }
      if (winBody && !hasActiveInteraction(winBody)) {
        const nextWin = document.createElement("tbody");
        if (d.windows_error) {
          nextWin.append(h("tr", null, h("td", { colspan: 7, class: "err-text" }, t(d.windows_error))));
        } else {
          nextWin.append(...(d.windows_jobs.length ? d.windows_jobs.map((w) => h("tr", null,
            h("td", null, w.job_id),
            h("td", { class: "break" }, w.document, w.app_job ? h("div", { class: "muted small" }, t("maint.app_job", { id: w.app_job })) : h("div", { class: "muted small" }, t("maint.foreign_job"))),
            h("td", null, w.user, w.machine ? h("div", { class: "muted small" }, w.machine) : null),
            h("td", null, (w.flags.length ? w.flags.join(", ") : t("maint.flags_none")) + (w.status_text ? " – " + w.status_text : "")),
            h("td", null, w.pages_printed + " / " + (w.total_pages || "?")),
            h("td", null, w.position),
            h("td", null, h("button", { class: "btn btn-sm btn-danger", type: "button",
              "data-action": "windows-jobs/" + w.job_id + "/cancel", "data-confirm": t("maint.confirm_cancel_windows") }, t("jobs.cancel")))))
            : [h("tr", null, h("td", { colspan: 7, class: "muted" }, t("maint.windows_queue_empty")))]));
        }
        if (winBody.innerHTML !== nextWin.innerHTML) winBody.replaceChildren(...nextWin.childNodes);
      }
      const upd = panel.querySelector("[data-queue-updated]");
      if (upd) upd.textContent = t("maint.updated", { time: fmtTime(new Date().toISOString()) });
    }
  }

  async function loadPrinter() {
    await refreshQueue();
    const doc = await fetchAdminDoc();
    if (!doc) return;
    softSwapElement(document.querySelector(".grid-2 > .card:first-child"), doc.querySelector(".grid-2 > .card:first-child"));
    const curDiag = document.querySelector("main > section.card:last-of-type dl.kv");
    const nextDiag = doc.querySelector("main > section.card:last-of-type dl.kv");
    softSwapElement(curDiag, nextDiag);
  }

  /* ---- Tab 4: Пользователи (Users) ---- */
  async function loadUsers() {
    const tbody = document.querySelector(".grid-2 section.card:first-child .table tbody");
    if (tbody && hasActiveInteraction(tbody)) return;
    const doc = await fetchAdminDoc();
    if (!doc) return;
    softSwapElement(tbody, doc.querySelector(".grid-2 section.card:first-child .table tbody"));
  }

  /* ---- Tab 5: Устройства (Devices) ---- */
  async function loadDevices() {
    const leftCard = document.querySelector(".grid-2 section.card:first-child");
    if (leftCard && hasActiveInteraction(leftCard)) return;
    const doc = await fetchAdminDoc();
    if (!doc) return;
    const nextLeftCard = doc.querySelector(".grid-2 section.card:first-child");
    if (!leftCard || !nextLeftCard) return;
    softSwapElement(leftCard.querySelector(".card-head .muted"), nextLeftCard.querySelector(".card-head .muted"));
    const curTbody = leftCard.querySelector(".devices-table tbody");
    const nextTbody = nextLeftCard.querySelector(".devices-table tbody");
    if (curTbody && nextTbody) {
      softSwapElement(curTbody, nextTbody);
    } else {
      softSwapElement(leftCard, nextLeftCard);
    }
  }

  /* ---- Tab 6: Настройки (Settings) ---- */
  async function loadSettings() {
    const form = document.querySelector("form.settings-page");
    if (!form || hasActiveInteraction(form)) return;
    const doc = await fetchAdminDoc();
    if (!doc) return;
    softSwapElement(form.querySelector("dl.kv.small"), doc.querySelector("form.settings-page dl.kv.small"));
  }

  /* ---- Tab 7: Системный журнал (System Log) ---- */
  async function loadLogs() {
    const doc = await fetchAdminDoc();
    if (!doc) return;
    softSwapElement(document.querySelector(".card-head .muted"), doc.querySelector(".card-head .muted"));
    const curPre = document.querySelector("pre.logview");
    const nextPre = doc.querySelector("pre.logview");
    if (curPre && nextPre && !hasTextSelectionInside(curPre)) {
      if (curPre.innerHTML !== nextPre.innerHTML) {
        const prevTop = curPre.scrollTop;
        curPre.innerHTML = nextPre.innerHTML;
        curPre.scrollTop = prevTop;
      }
    }
  }

  /* ---- Tab 8: Журнал аудита (Audit Log) ---- */
  async function loadAudit() {
    const doc = await fetchAdminDoc();
    if (!doc) return;
    softSwapElement(document.querySelector(".card-head .muted"), doc.querySelector(".card-head .muted"));
    softSwapElement(document.querySelector(".table tbody"), doc.querySelector(".table tbody"));
    softSwapElement(document.querySelector(".pager"), doc.querySelector(".pager"));
  }

  /* ---- Active Tab Detection & Global 5s Silent Polling ---- */
  function detectActiveAdminTab() {
    const pageId = (document.body && document.body.dataset.page) || "";
    if (pageId.startsWith("admin-")) return pageId;
    const activeLink = document.querySelector(".subnav a.active");
    if (!activeLink) return "";
    const href = activeLink.getAttribute("href") || "";
    if (href.endsWith("/admin/") || href.endsWith("/admin")) return "admin-dashboard";
    if (href.includes("/admin/jobs")) return "admin-jobs";
    if (href.includes("/admin/printer")) return "admin-printer";
    if (href.includes("/admin/users")) return "admin-users";
    if (href.includes("/admin/devices")) return "admin-devices";
    if (href.includes("/admin/settings")) return "admin-settings";
    if (href.includes("/admin/logs")) return "admin-logs";
    if (href.includes("/admin/audit")) return "admin-audit";
    return "";
  }

  async function pollActiveAdminTab() {
    if (document.hidden || pollInFlight) return;
    if (!location.pathname.startsWith("/admin")) {
      stopAdminAutoRefresh();
      return;
    }
    const tab = detectActiveAdminTab();
    if (!tab) return;
    pollInFlight = true;
    try {
      switch (tab) {
        case "admin-dashboard":
          await loadSummary();
          break;
        case "admin-jobs":
          await loadJobs();
          break;
        case "admin-printer":
          await loadPrinter();
          break;
        case "admin-users":
          await loadUsers();
          break;
        case "admin-devices":
          await loadDevices();
          break;
        case "admin-settings":
          await loadSettings();
          break;
        case "admin-logs":
          await loadLogs();
          break;
        case "admin-audit":
          await loadAudit();
          break;
      }
    } finally {
      pollInFlight = false;
    }
  }

  function startAdminAutoRefresh() {
    stopAdminAutoRefresh();
    if (!location.pathname.startsWith("/admin")) return;
    adminPollingInterval = setInterval(pollActiveAdminTab, 5000);
  }

  /* ---- Remote Job Cancellation: <button data-cancel-job-id="…"> ---- */
  document.addEventListener("click", async (ev) => {
    const cancelBtn = ev.target.closest("[data-cancel-job-id]");
    if (!cancelBtn) return;
    ev.preventDefault();
    const jobId = cancelBtn.dataset.cancelJobId;
    if (!jobId) return;
    if (!confirm("Вы уверены, что хотите отменить это задание?")) return;
    cancelBtn.disabled = true;
    if ( document.activeElement === cancelBtn ) cancelBtn.blur();
    const r = await api("POST", "/admin/api/job/" + encodeURIComponent(jobId) + "/cancel", {});
    cancelBtn.disabled = false;
    if (r.ok) {
      toast((r.data && r.data.message) || t("js.action_ok"), "ok");
      await refreshQueue();
    } else {
      toast((r.data && r.data.message) || t((r.data && r.data.error) || "err.internal"), "error");
    }
  });

  /* ---- One-Click Job Reprint: <button data-reprint-job-id="…"> ---- */
  document.addEventListener("click", async (ev) => {
    const reprintBtn = ev.target.closest("[data-reprint-job-id]");
    if (!reprintBtn) return;
    ev.preventDefault();
    const jobId = reprintBtn.dataset.reprintJobId;
    if (!jobId) return;
    reprintBtn.disabled = true;
    if (document.activeElement === reprintBtn) reprintBtn.blur();
    const r = await api("POST", "/admin/api/job/" + encodeURIComponent(jobId) + "/reprint", {});
    reprintBtn.disabled = false;
    if (r.ok) {
      toast((r.data && r.data.message) || t("js.reprint_ok") || t("Повторить"), "ok");
      await refreshQueue();
    } else {
      toast((r.data && r.data.message) || t((r.data && r.data.error) || "err.internal"), "error");
    }
  });

  /* Buttons: <button data-action="printer/pause" data-confirm="…" data-body='{"x":1}' data-reload> */
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    ev.preventDefault();
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    let body;
    try { body = btn.dataset.body ? JSON.parse(btn.dataset.body) : {}; } catch (e) { body = {}; }
    const label = btn.textContent;
    btn.disabled = true;
    if (btn.dataset.busy) btn.textContent = btn.dataset.busy;
    const r = await api("POST", "/admin/api/" + btn.dataset.action, body);
    btn.disabled = false;
    btn.textContent = label;
    if (r.ok) {
      let msg = t("js.action_ok");
      if (r.data.result && typeof r.data.result === "object") msg = t("js.cancel_all_result", r.data.result);
      else if (r.data.result) msg = t("js.cancel_" + r.data.result);
      if (r.data.job_id) msg = t("js.test_page_queued", { id: r.data.job_id });
      toast(msg, "ok");
      if (btn.hasAttribute("data-reload")) pollActiveAdminTab();
      else refreshQueue();
    } else {
      toast(t(r.data.error || "err.internal"), "error");
    }
  });

  /* ---- zero-paper driver check (Printer page) ---- */
  function fmtAdjusted(adj) {
    const keys = Object.keys(adj || {});
    if (!keys.length) return h("span", { class: "muted" }, "—");
    return h("span", { class: "warn-text" }, keys.map((k) => k + ": " + adj[k][0] + " → " + adj[k][1]).join("; "));
  }

  async function runDriverCheck(btn) {
    const box = document.querySelector("[data-driver-check-result]");
    btn.disabled = true;
    btn.classList.add("busy");
    const r = await api("POST", "/admin/api/driver-check", {});
    btn.disabled = false;
    btn.classList.remove("busy");
    if (!r.ok) { toast(t(r.data.error || "err.internal"), "error"); return; }
    const res = r.data.result;
    const mm = (a) => (a ? a[0] + " × " + a[1] : "—");
    const paperRows = res.papers.map((p) => h("tr", null,
      h("td", null, p.label),
      h("td", null, mm(p.nominal_mm)),
      h("td", null, p.error ? h("span", { class: "err-text" }, t(p.error)) : h("span", { class: p.size_ok ? "" : "err-text" }, mm(p.dc_mm) + (p.size_ok ? " ✔" : " ✘"))),
      h("td", null, mm(p.printable_mm)),
      h("td", null, p.offset_px ? p.offset_px.join(", ") : "—"),
      h("td", null, p.dpi ? p.dpi.join(" × ") : "—"),
      h("td", null, fmtAdjusted(p.driver_adjusted))));
    const mediaRows = res.media.map((m) => h("tr", null,
      h("td", null, m.label),
      h("td", null, t("quality." + m.quality)),
      h("td", null, m.requested.join(" / ")),
      h("td", null, m.error ? h("span", { class: "err-text" }, t(m.error)) : (m.dpi ? m.dpi.join(" × ") : "—")),
      h("td", null, fmtAdjusted(m.driver_adjusted))));
    const s = res.summary;
    box.replaceChildren(
      h("div", { class: "alert " + (s.adjusted || s.errors || s.size_mismatches ? "alert-warn" : "alert-ok") },
        t("maint.driver_check_summary", { adjusted: s.adjusted, errors: s.errors, mismatches: s.size_mismatches, merge: res.merge ? "on" : "off" })),
      h("h3", null, t("opt.paper")),
      h("div", { class: "table-wrap" }, h("table", { class: "table small" },
        h("thead", null, h("tr", null, [t("opt.paper"), t("maint.nominal"), t("maint.dc_size"), t("maint.printable"), "offset px", "dpi", t("maint.changed_by_driver")].map((x) => h("th", null, x)))),
        h("tbody", null, paperRows))),
      h("h3", null, t("opt.media") + " × " + t("opt.quality")),
      h("div", { class: "table-wrap" }, h("table", { class: "table small" },
        h("thead", null, h("tr", null, [t("opt.media"), t("opt.quality"), t("maint.requested"), "DC dpi", t("maint.changed_by_driver")].map((x) => h("th", null, x)))),
        h("tbody", null, mediaRows))));
  }

  /* ---- Word/Excel converter test (Printer page) ---- */
  async function runOfficeTest(btn) {
    const box = document.querySelector("[data-office-result]");
    btn.disabled = true;
    btn.classList.add("busy");
    const r = await api("POST", "/admin/api/office-test", {});
    btn.disabled = false;
    btn.classList.remove("busy");
    if (!r.ok) { toast(t(r.data.error || "err.internal"), "error"); return; }
    box.replaceChildren(...r.data.results.map((x) => h("div", { class: "alert " + (x.ok ? "alert-ok" : "alert-error") },
      h("strong", null, x.kind.toUpperCase() + ": "),
      x.ok ? t("office.test_ok", { engine: t("office.engine." + x.engine), ms: x.ms, pages: x.pages })
           : t(x.error) + (x.detail ? " — " + x.detail : ""))));
  }

  document.addEventListener("DOMContentLoaded", () => {
    const otBtn = document.querySelector("[data-run-office-test]");
    if (otBtn) otBtn.addEventListener("click", () => runOfficeTest(otBtn));
    const dcBtn = document.querySelector("[data-run-driver-check]");
    if (dcBtn) dcBtn.addEventListener("click", () => runDriverCheck(dcBtn));
    document.querySelectorAll("[data-maint-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const action = btn.dataset.maintAction;
        const confirmMsg = action === "head_cleaning"
          ? "Вы уверены, что хотите запустить прочистку печатающей головки? Эта процедура расходует чернила."
          : "Вы уверены, что хотите запустить проверку дюз? Эта процедура расходует чернила.";
        if (!confirm(confirmMsg)) return;

        const origHTML = btn.innerHTML;
        const origBg = btn.style.backgroundColor;
        const origColor = btn.style.color;
        const origBorder = btn.style.borderColor;
        const origOpacity = btn.style.opacity;

        btn.disabled = true;
        btn.style.opacity = "1";
        btn.innerHTML = "⏳ Обработка...";

        const r = await api("POST", "/admin/api/printer/maintenance", { action });

        if (r.ok) {
          btn.style.backgroundColor = "#28a745";
          btn.style.borderColor = "#28a745";
          btn.style.color = "#fff";
          btn.innerHTML = "✔ Готово";
          toast(r.data.message || t("js.action_ok"), "ok");
          refreshQueue();
        } else {
          btn.style.backgroundColor = "#dc3545";
          btn.style.borderColor = "#dc3545";
          btn.style.color = "#fff";
          btn.innerHTML = "❌ Ошибка";
          toast((r.data && r.data.message) || t((r.data && r.data.error) || "err.internal"), "error");
        }

        setTimeout(() => {
          btn.innerHTML = origHTML;
          btn.style.backgroundColor = origBg;
          btn.style.borderColor = origBorder;
          btn.style.color = origColor;
          btn.style.opacity = origOpacity;
          btn.disabled = false;
          btn.removeAttribute("disabled");
        }, 4000);
      });
    });
    refreshHealth();
    refreshQueue();
    startAdminAutoRefresh();

    /* ---- Lifecycle cleanup when leaving Admin or logging out ---- */
    window.addEventListener("pagehide", stopAdminAutoRefresh);
    window.addEventListener("beforeunload", stopAdminAutoRefresh);
    document.addEventListener("submit", (ev) => {
      const f = ev.target;
      if (f && f.action && f.action.includes("/logout")) {
        stopAdminAutoRefresh();
      }
    });
    document.addEventListener("click", (ev) => {
      const link = ev.target.closest("a[href]");
      if (!link) return;
      const href = link.getAttribute("href") || "";
      if (href.startsWith("/") && !href.startsWith("/admin")) {
        stopAdminAutoRefresh();
      }
    });
  });
})();
