/* LocalPrintServer – shared browser helpers (no external dependencies). */
(function () {
  "use strict";
  const I18N = JSON.parse(document.getElementById("i18n-data").textContent || "{}");
  const LANG = document.body.dataset.lang || "en";
  const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  function t(key, params) {
    let s = I18N[key] || key;
    if (params) for (const k in params) s = s.split("{" + k + "}").join(String(params[k]));
    return s;
  }

  /* h("div", {class: "x", onclick: fn}, "text", childNode) – safe DOM builder */
  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) {
      const c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) c.forEach((x) => x != null && el.append(x instanceof Node ? x : String(x)));
      else el.append(c instanceof Node ? c : String(c));
    }
    return el;
  }

  async function api(method, url, body) {
    const opts = { method, headers: { "X-CSRF-Token": CSRF, Accept: "application/json" }, credentials: "same-origin" };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      return { ok: false, status: 0, data: { ok: false, error: "err.network" } };
    }
    let data = null;
    try { data = await resp.json(); } catch (e) { data = { ok: false, error: resp.status === 413 ? "err.too_large" : "err.internal" }; }
    if (resp.status === 401 && !url.includes("/status")) {
      window.location.href = "/login?next=" + encodeURIComponent(location.pathname);
    }
    return { ok: resp.ok && data && data.ok, status: resp.status, data: data || {} };
  }

  function toast(msg, kind) {
    const host = document.querySelector("[data-toast-host]");
    if (!host) return;
    const el = h("div", { class: "toast toast-" + (kind || "info"), role: "status" }, msg);
    host.append(el);
    setTimeout(() => el.classList.add("show"), 10);
    setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, kind === "error" ? 7000 : 4000);
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const locale = LANG === "uz" ? "uz-Latn-UZ" : LANG === "ru" ? "ru-RU" : "en-GB";
    try {
      return d.toLocaleString(locale, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch (e) { return d.toLocaleString(); }
  }

  function localizeTimes(root) {
    (root || document).querySelectorAll("time.ts[datetime]").forEach((el) => {
      el.textContent = fmtTime(el.getAttribute("datetime"));
    });
  }

  function badge(status) {
    return h("span", { class: "badge st-" + String(status).toLowerCase() }, t("state." + status));
  }

  /* ---- header status pill + print-page banner ---- */
  const PROBLEM_STATES = ["WAITING", "ERROR", "UNAVAILABLE", "SPOOLER_ERROR", "PAUSED"];
  async function refreshStatus() {
    const pill = document.querySelector("[data-status-pill]");
    if (!pill) return;
    const r = await api("GET", "/api/status");
    if (!r.ok) { pill.hidden = true; return; }
    const st = r.data.status;
    pill.hidden = false;
    pill.className = "status-pill state-" + st.state.toLowerCase();
    pill.querySelector("[data-status-text]").textContent = t("status." + st.state);
    pill.title = st.reasons.map((k) => t(k)).join("\n");
    const banner = document.querySelector("[data-status-banner]");
    if (banner) {
      if (PROBLEM_STATES.includes(st.state)) {
        banner.replaceChildren(h("strong", null, t("status." + st.state) + ": "), st.reasons.map((k) => t(k)).join(" "));
        banner.className = "status-banner state-" + st.state.toLowerCase();
        banner.hidden = false;
      } else banner.hidden = true;
    }
    document.dispatchEvent(new CustomEvent("lps:status", { detail: st }));
  }

  document.addEventListener("DOMContentLoaded", () => {
    localizeTimes();
    const toggle = document.querySelector("[data-nav-toggle]");
    if (toggle) toggle.addEventListener("click", () => {
      const nav = document.querySelector("[data-nav]");
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.querySelectorAll("pre[data-copy]").forEach((pre) => {
      pre.title = t("js.click_to_copy");
      pre.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(pre.textContent.trim()); toast(t("js.copied"), "ok"); } catch (e) { /* clipboard needs https/localhost */ }
      });
    });
    document.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-cancel-job]");
      if (!btn) return;
      if (!confirm(t("jobs.confirm_cancel"))) return;
      btn.disabled = true;
      const r = await api("POST", "/api/jobs/" + encodeURIComponent(btn.dataset.cancelJob) + "/cancel");
      if (r.ok) {
        toast(t("js.cancel_" + r.data.result), "ok");
        if (document.body.dataset.page === "print") document.dispatchEvent(new CustomEvent("lps:jobs-changed"));
        else setTimeout(() => location.reload(), 800);
      }
      else { toast(t(r.data.error), "error"); btn.disabled = false; }
    });
    if (document.querySelector("[data-status-pill]") && document.body.dataset.page !== "") {
      refreshStatus();
      setInterval(refreshStatus, 15000);
    }
  });

  window.LPS = { t, h, api, toast, fmtTime, localizeTimes, badge, refreshStatus, lang: LANG };
})();
