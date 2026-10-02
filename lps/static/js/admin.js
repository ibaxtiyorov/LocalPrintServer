/* LocalPrintServer – admin panel interactions. */
(function () {
  "use strict";
  const { t, h, api, toast, fmtTime, badge } = window.LPS;

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
      if (btn.hasAttribute("data-reload")) setTimeout(() => location.reload(), 700);
      else refreshQueue();
    } else {
      toast(t(r.data.error || "err.internal"), "error");
    }
  });

  /* ---- live queue panel (Printer page) ---- */
  const panel = document.querySelector("[data-queue-panel]");
  let qTimer = null;

  async function refreshQueue() {
    if (!panel) return;
    clearTimeout(qTimer);
    const r = await api("GET", "/admin/api/queue");
    if (r.ok) {
      const appBody = panel.querySelector("[data-app-queue]");
      const winBody = panel.querySelector("[data-win-queue]");
      const d = r.data;
      appBody.replaceChildren(...(d.app_jobs.length ? d.app_jobs.map((j) => h("tr", null,
        h("td", { class: "mono small" }, j.id),
        h("td", null, j.username),
        h("td", { class: "break" }, j.filename, h("div", { class: "muted small" }, fmtTime(j.created_at))),
        h("td", null, badge(j.status), j.status_detail ? h("div", { class: "muted small" }, j.status_detail) : null,
          j.cancel_requested ? h("div", { class: "small warn-text" }, t("jobs.cancel_requested")) : null),
        h("td", null, j.spooler_job_id || "—"),
        h("td", null, j.cancellable ? h("button", { class: "btn btn-sm btn-danger", type: "button",
          "data-action": "jobs/" + j.id + "/cancel", "data-confirm": t("jobs.confirm_cancel") }, t("jobs.cancel")) : null)))
        : [h("tr", null, h("td", { colspan: 6, class: "muted" }, d.app_paused ? t("maint.app_queue_empty_paused") : t("maint.app_queue_empty")))]));
      if (d.windows_error) {
        winBody.replaceChildren(h("tr", null, h("td", { colspan: 7, class: "err-text" }, t(d.windows_error))));
      } else {
        winBody.replaceChildren(...(d.windows_jobs.length ? d.windows_jobs.map((w) => h("tr", null,
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
      panel.querySelector("[data-queue-updated]").textContent = t("maint.updated", { time: fmtTime(new Date().toISOString()) });
    }
    qTimer = setTimeout(refreshQueue, 3000);
  }

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
    refreshQueue();
    // Dashboard: reload the evidence card periodically (cheap, server-rendered).
    if (document.body.dataset.page === "admin-dashboard") setInterval(() => {
      if (!document.hidden) location.reload();
    }, 30000);
  });
})();
