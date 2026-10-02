/* LocalPrintServer – print page: documents → page selection → settings → preview → print. */
(function () {
  "use strict";
  const { t, h, api, toast, fmtTime, badge } = window.LPS;
  const $ = (sel, root) => (root || document).querySelector(sel);
  const form = $("[data-settings-form]");
  const ACCEPT = {
    pdf: ".pdf,application/pdf", jpeg: ".jpg,.jpeg,image/jpeg", png: ".png,image/png",
    bmp: ".bmp,image/bmp", gif: ".gif,image/gif", tiff: ".tif,.tiff,image/tiff", webp: ".webp,image/webp",
    docx: ".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    doc: ".doc,application/msword",
    xlsx: ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    xls: ".xls,application/vnd.ms-excel",
  };
  const state = { catalog: null, docs: [], sheet: 0, sheets: 1, previewSeq: 0, timer: null,
                  printing: false, presets: [], dragFrom: null };

  /* ------------------------------------------------------------- page selection
     Same rules as the server: a selection is a SET of pages, printed in document
     order; "reverse order" is a separate option. null = all pages. */
  function parseRange(text, count) {
    text = (text || "").trim();
    if (!text) return null;
    const out = new Set();
    for (const raw of text.split(",")) {
      const part = raw.trim();
      if (!part) continue;
      const m = /^(\d+)\s*(?:-\s*(\d*))?$/.exec(part);
      if (!m) throw new Error("syntax");
      const a = Number(m[1]);
      const b = m[2] === undefined ? (part.includes("-") ? count : a) : (m[2] === "" ? count : Number(m[2]));
      if (a < 1 || b < a || b > count) throw new Error("range");
      for (let p = a; p <= b; p++) out.add(p);
    }
    if (!out.size) throw new Error("empty");
    return out.size === count ? null : out;
  }

  function formatRange(pages) {
    const s = [...pages].sort((x, y) => x - y);
    const out = [];
    for (let i = 0; i < s.length; i++) {
      let j = i;
      while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++;
      out.push(j > i ? s[i] + "-" + s[j] : String(s[i]));
      i = j;
    }
    return out.join(",");
  }

  const selCount = (d) => (d.pages === null ? d.upload.pages : d.pages.size);
  const rangeText = (d) => (d.pages === null ? "" : formatRange(d.pages));

  /* ------------------------------------------------------------ catalog / form */
  function radioGroup(name, values, labelFn, checked) {
    const box = document.querySelector('[data-radio-group="' + name + '"]');
    box.replaceChildren(...values.map((v) => {
      const id = "r-" + name + "-" + v;
      return h("span", { class: "seg-item" },
        h("input", { type: "radio", name, id, value: v, checked: String(v) === String(checked), "data-opt": true }),
        h("label", { for: id }, labelFn(v)));
    }));
  }

  function fillSelect(sel, items, current) {
    sel.replaceChildren(...items.map((it) => h("option", { value: it.value, selected: it.value === current }, it.label)));
  }

  async function loadCatalog() {
    const r = await api("GET", "/api/catalog");
    if (!r.ok) { showError("[data-upload-error]", r.data.error); return; }
    const c = (state.catalog = r.data);
    const d = c.defaults;
    fillSelect(form.paper, c.papers.map((p) => ({ value: p.key, label: p.label }))
      .concat([{ value: "CUSTOM", label: t("opt.custom_size") }]), d.paper);
    fillSelect(form.media, c.media.map((m) => ({ value: m.key, label: t("media." + m.key) })), d.media);
    radioGroup("quality", c.quality, (v) => t("quality." + v), d.quality);
    radioGroup("color", c.color, (v) => t("color." + v), d.color);
    radioGroup("orientation", c.orientation, (v) => t("orientation." + v), d.orientation);
    radioGroup("scaling", c.scaling, (v) => t("scaling." + v), d.scaling);
    radioGroup("nup", c.nup, (v) => (v === 1 ? t("opt.nup_off") : String(v)), 1);
    form.margins_mm.value = d.margins_mm;
    form.copies.max = c.limits.max_copies;
    const cu = c.custom;
    form.custom_w_mm.min = cu.min_w; form.custom_w_mm.max = cu.max_w; form.custom_w_mm.value = 200;
    form.custom_h_mm.min = cu.min_h; form.custom_h_mm.max = cu.max_h; form.custom_h_mm.value = 250;
    $("[data-custom-hint]").textContent = t("opt.custom_hint", cu);
    $("[data-file-input]").accept = c.allowed_types.map((x) => ACCEPT[x] || "").join(",");
    $("[data-accept-hint]").textContent = t("print.accept_hint", {
      types: c.allowed_types.map((x) => x.toUpperCase()).join(", "), mb: c.limits.max_upload_mb,
    });
    if (c.office_unavailable.length) {
      const hint = $("[data-office-hint]");
      hint.textContent = t("print.office_unavailable", { types: c.office_unavailable.map((x) => x.toUpperCase()).join(", ") });
      hint.hidden = false;
    }
    syncForm();
  }

  function currentPaper() {
    return state.catalog && state.catalog.papers.find((p) => p.key === form.paper.value);
  }

  function syncForm() {
    $("[data-custom-size]").hidden = form.paper.value !== "CUSTOM";
    const paper = currentPaper();
    const blOk = !!(paper && paper.borderless);
    $("[data-borderless-wrap]").hidden = !blOk;
    if (!blOk) form.borderless.checked = false;
    $("[data-borderless-note]").hidden = !form.borderless.checked;
    form.margins_mm.disabled = form.borderless.checked;
    const nup = Number((form.querySelector('input[name="nup"]:checked') || {}).value || 1);
    const actual = form.querySelector('input[name="scaling"][value="ACTUAL"]');
    if (actual) {
      actual.disabled = nup > 1;
      if (nup > 1 && actual.checked) form.querySelector('input[name="scaling"][value="FIT"]').checked = true;
    }
    form.collate.disabled = Number(form.copies.value || 1) < 2;
  }

  function gatherOptions() {
    const fd = new FormData(form);
    const o = {};
    for (const [k, v] of fd.entries()) o[k] = v;
    ["collate", "reverse", "mirror", "rotate180", "borderless"].forEach((k) => (o[k] = form[k].checked));
    o.nup = Number(o.nup || 1);
    o.copies = Number(form.copies.value || 1);
    o.margins_mm = form.borderless.checked ? 0 : Number(form.margins_mm.value || 0);
    if (o.paper !== "CUSTOM") { delete o.custom_w_mm; delete o.custom_h_mm; }
    else { o.custom_w_mm = Number(form.custom_w_mm.value); o.custom_h_mm = Number(form.custom_h_mm.value); }
    return o;
  }

  function applyOptions(o) {
    const setRadio = (name, v) => {
      const el = form.querySelector(`input[name="${name}"][value="${v}"]`);
      if (el) el.checked = true;
    };
    if ([...form.paper.options].some((x) => x.value === o.paper)) form.paper.value = o.paper;
    if (o.custom_w_mm) form.custom_w_mm.value = o.custom_w_mm;
    if (o.custom_h_mm) form.custom_h_mm.value = o.custom_h_mm;
    if ([...form.media.options].some((x) => x.value === o.media)) form.media.value = o.media;
    ["quality", "color", "orientation", "scaling", "nup"].forEach((k) => o[k] !== undefined && setRadio(k, o[k]));
    if (o.copies) form.copies.value = o.copies;
    if (o.margins_mm !== undefined) form.margins_mm.value = o.margins_mm;
    ["collate", "reverse", "mirror", "rotate180", "borderless"].forEach((k) => { if (k in o) form[k].checked = !!o[k]; });
    syncForm();
  }

  function clearFieldErrors() {
    document.querySelectorAll("[data-error-for]").forEach((el) => (el.textContent = ""));
    // Keep the browser's own validation message for fields that are still invalid.
    state.docs.forEach((d) => setRangeError(d, d.invalid ? t("err.page_range") : ""));
  }

  function showFieldErrors(fields) {
    clearFieldErrors();
    const params = state.catalog ? { ...state.catalog.custom, max: state.catalog.limits.max_copies } : {};
    for (const f in fields || {}) {
      const msg = t(fields[f], params);
      const m = /^page_range\.(\d+)$/.exec(f);
      if (m && state.docs[Number(m[1])]) { setRangeError(state.docs[Number(m[1])], msg); continue; }
      const el = [...document.querySelectorAll("[data-error-for]")].find((x) => x.dataset.errorFor.split(" ").includes(f));
      if (el) el.textContent = msg; else toast(msg, "error");
    }
  }

  function showError(sel, key, params) {
    const el = $(sel);
    if (!key) { el.hidden = true; el.textContent = ""; return; }
    el.textContent = t(key, params || {});
    el.hidden = false;
  }

  /* ------------------------------------------------------------------- upload */
  async function uploadFiles(files) {
    if (!state.catalog || !files.length) return;
    showError("[data-upload-error]", null);
    const max = state.catalog.limits.max_documents;
    for (const file of files) {
      if (state.docs.length >= max) { showError("[data-upload-error]", "err.too_many_documents", { max }); break; }
      await uploadOne(file);
    }
  }

  function uploadOne(file) {
    return new Promise((resolve) => {
      const maxMb = state.catalog.limits.max_upload_mb;
      const list = $("[data-upload-list]");
      const bar = h("span");
      const label = h("span", { class: "small" }, t("print.uploading", { name: file.name }));
      const row = h("li", { class: "upload-progress" }, h("div", { class: "bar" }, bar), label);
      if (file.size > maxMb * 1024 * 1024) {
        showError("[data-upload-error]", "err.too_large", { max_mb: maxMb });
        return resolve();
      }
      list.append(row);
      const fd = new FormData();
      fd.append("file", file, file.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/uploads");
      xhr.setRequestHeader("X-CSRF-Token", document.querySelector('meta[name="csrf-token"]').content);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        bar.style.width = Math.round((e.loaded / e.total) * 100) + "%";
        if (e.loaded >= e.total) label.textContent = t("print.processing", { name: file.name });
      };
      xhr.onload = () => {
        row.remove();
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = { error: xhr.status === 413 ? "err.too_large" : "err.internal" }; }
        if (xhr.status === 200 && data.ok) addDocument(data.upload);
        else showError("[data-upload-error]", data.error || "err.internal",
                       { max_mb: maxMb, max_pages: state.catalog.limits.max_pdf_pages, max: state.catalog.limits.max_documents });
        resolve();
      };
      xhr.onerror = () => { row.remove(); showError("[data-upload-error]", "err.network"); resolve(); };
      xhr.send(fd);
    });
  }

  /* ----------------------------------------------------------- document list */
  function addDocument(upload) {
    // invalid: the range field holds text that is not a valid selection; printing is
    // blocked until it is fixed (never print a stale selection the field no longer shows).
    const doc = { upload, pages: null, el: null, built: false, last: null, invalid: false };
    doc.el = buildDocEl(doc);
    state.docs.push(doc);
    renderDocs();
    state.sheet = 0;
    schedulePreview(0);
  }

  function buildDocEl(doc) {
    const li = document.getElementById("doc-item-tpl").content.firstElementChild.cloneNode(true);
    const up = doc.upload;
    $("[data-name]", li).textContent = up.filename;
    $("[data-meta]", li).textContent = t(up.converted ? "print.file_meta_converted" : "print.file_meta", {
      type: up.doc_type.toUpperCase(), pages: up.pages, size: (up.size / 1048576).toFixed(2),
    });
    if (up.pages < 2) $(".doc-pages", li).hidden = true;
    li.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn) onDocAction(doc, btn.dataset.act, btn);
      // Only real thumbnail buttons of THIS card (<body> also carries a data-page attribute).
      const th = e.target.closest(".thumb[data-page]");
      if (th && li.contains(th)) togglePage(doc, Number(th.dataset.page), e.shiftKey);
    });
    $("[data-range]", li).addEventListener("input", (e) => onRangeInput(doc, e.target.value));
    // Drag only from the handle so text inside the item stays selectable.
    const handle = $(".drag-handle", li);
    li.draggable = false;
    handle.addEventListener("mousedown", () => (li.draggable = true));
    li.addEventListener("dragstart", (e) => {
      state.dragFrom = state.docs.indexOf(doc);
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(state.dragFrom));
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", () => { li.draggable = false; li.classList.remove("dragging"); clearDropMarks(); });
    li.addEventListener("dragover", (e) => {
      if (state.dragFrom === null) return;
      e.preventDefault();
      clearDropMarks();
      li.classList.add("drop-target");
    });
    li.addEventListener("drop", (e) => {
      e.preventDefault();
      const from = state.dragFrom, to = state.docs.indexOf(doc);
      state.dragFrom = null;
      clearDropMarks();
      if (from === null || from === to) return;
      const [moved] = state.docs.splice(from, 1);
      state.docs.splice(to, 0, moved);
      renderDocs();
      schedulePreview(0);
    });
    return li;
  }

  function clearDropMarks() {
    document.querySelectorAll(".doc-item.drop-target").forEach((x) => x.classList.remove("drop-target"));
  }

  function renderDocs() {
    const list = $("[data-doc-list]");
    list.replaceChildren(...state.docs.map((d) => d.el));
    state.docs.forEach((d, i) => {
      $("[data-num]", d.el).textContent = String(i + 1);
      $('[data-act="up"]', d.el).disabled = i === 0;
      $('[data-act="down"]', d.el).disabled = i === state.docs.length - 1;
      updateSelectionUi(d);
    });
    $("[data-doc-order-hint]").hidden = state.docs.length < 2;
    if (!state.docs.length) {
      $("[data-preview-img]").hidden = true;
      $("[data-preview-empty]").hidden = false;
      $("[data-preview-nav]").hidden = true;
      $("[data-preview-notes]").replaceChildren();
      $("[data-print-order]").hidden = true;
      $("[data-print-btn]").disabled = true;
    }
  }

  function onDocAction(doc, act, btn) {
    const i = state.docs.indexOf(doc);
    if (act === "remove") {
      api("DELETE", "/api/uploads/" + doc.upload.id);
      state.docs.splice(i, 1);
    } else if (act === "up" && i > 0) {
      [state.docs[i - 1], state.docs[i]] = [state.docs[i], state.docs[i - 1]];
    } else if (act === "down" && i < state.docs.length - 1) {
      [state.docs[i + 1], state.docs[i]] = [state.docs[i], state.docs[i + 1]];
    } else if (act === "toggle-pages") {
      const panel = $("[data-page-panel]", doc.el);
      panel.hidden = !panel.hidden;
      btn.setAttribute("aria-expanded", String(!panel.hidden));
      if (!panel.hidden) buildThumbs(doc);
      return;
    } else if (act === "all") {
      doc.pages = null;
      doc.invalid = false;
    } else if (act === "none") {
      doc.pages = new Set();
      doc.invalid = false;
    } else {
      return;
    }
    renderDocs();
    state.sheet = 0;
    schedulePreview(0);
  }

  function buildThumbs(doc) {
    if (doc.built) return;
    doc.built = true;
    const grid = $("[data-thumbs]", doc.el);
    const frag = document.createDocumentFragment();
    for (let p = 1; p <= doc.upload.pages; p++) {
      frag.append(h("button", { type: "button", class: "thumb", "data-page": p, "aria-pressed": "true",
                                title: t("pages.page_n", { n: p }) },
        h("img", { src: `/api/uploads/${encodeURIComponent(doc.upload.id)}/thumb/${p}`, alt: "",
                   loading: "lazy", decoding: "async", width: 100, height: 140 }),
        h("span", { class: "thumb-num" }, String(p))));
    }
    grid.replaceChildren(frag);
    updateSelectionUi(doc);
  }

  function togglePage(doc, page, shift) {
    const n = doc.upload.pages;
    if (!Number.isInteger(page) || page < 1 || page > n) return;
    const sel = doc.pages === null ? new Set(Array.from({ length: n }, (_, i) => i + 1)) : new Set(doc.pages);
    if (shift && doc.last) {                      // shift-click: apply to the whole span
      const on = !sel.has(page);
      const [a, b] = [Math.min(doc.last, page), Math.max(doc.last, page)];
      for (let p = a; p <= b; p++) on ? sel.add(p) : sel.delete(p);
    } else {
      sel.has(page) ? sel.delete(page) : sel.add(page);
    }
    doc.last = page;
    doc.pages = sel.size === n ? null : sel;
    doc.invalid = false;
    updateSelectionUi(doc);
    state.sheet = 0;
    schedulePreview();
  }

  function onRangeInput(doc, text) {
    try {
      doc.pages = parseRange(text, doc.upload.pages);
      doc.invalid = false;
      setRangeError(doc, "");
      updateSelectionUi(doc, true);
      state.sheet = 0;
      schedulePreview(500);
    } catch (e) {
      doc.invalid = true;
      state.previewSeq++;                          // ignore any preview still in flight
      setRangeError(doc, t("err.page_range"));
      $("[data-print-btn]").disabled = true;
    }
  }

  function setRangeError(doc, msg) {
    const el = $("[data-range-error]", doc.el);
    if (el) el.textContent = msg;
  }

  function updateSelectionUi(doc, fromInput) {
    const n = doc.upload.pages;
    const count = selCount(doc);
    const summary = $("[data-sel-summary]", doc.el);
    summary.textContent = count === 0 ? t("pages.none_selected")
      : doc.pages === null ? t("pages.all_selected", { n })
      : t("pages.some_selected", { pages: formatRange(doc.pages), n: count, total: n });
    summary.className = "small" + (count === 0 ? " err-text" : "");
    if (!fromInput) $("[data-range]", doc.el).value = rangeText(doc);
    if (doc.built) {
      doc.el.querySelectorAll("[data-page]").forEach((b) => {
        const on = doc.pages === null || doc.pages.has(Number(b.dataset.page));
        b.setAttribute("aria-pressed", String(on));
        b.classList.toggle("off", !on);
      });
    }
  }

  /* ------------------------------------------------------------------ preview */
  function requestBody(extra) {
    return Object.assign({
      documents: state.docs.map((d) => ({ upload_id: d.upload.id, page_range: rangeText(d) })),
      options: gatherOptions(),
    }, extra || {});
  }

  function schedulePreview(delay) {
    clearTimeout(state.timer);
    if (!state.docs.length) return;
    state.timer = setTimeout(renderPreview, delay === undefined ? 350 : delay);
  }

  async function renderPreview() {
    if (!state.docs.length) return;
    const btn = $("[data-print-btn]");
    if (state.docs.some((d) => d.invalid)) {
      showError("[data-print-error]", "err.page_range");
      btn.disabled = true;
      return;
    }
    if (state.docs.some((d) => selCount(d) === 0)) {
      showError("[data-print-error]", "err.no_pages_selected");
      btn.disabled = true;
      return;
    }
    const seq = ++state.previewSeq;
    $("[data-preview-spinner]").hidden = false;
    const r = await api("POST", "/api/preview", requestBody({ sheet: state.sheet }));
    if (seq !== state.previewSeq) return;       // a newer request superseded this one
    $("[data-preview-spinner]").hidden = true;
    if (!r.ok) {
      if (r.data.error === "err.upload_expired") {
        toast(t("err.upload_expired"), "error");
        if (r.data.document !== undefined) { state.docs.splice(r.data.document, 1); renderDocs(); schedulePreview(0); }
        return;
      }
      if (r.data.fields) showFieldErrors(r.data.fields); else showError("[data-print-error]", r.data.error, r.data);
      btn.disabled = true;
      return;
    }
    clearFieldErrors();
    showError("[data-print-error]", null);
    const m = r.data.meta;
    state.sheets = m.sheets;
    state.sheet = m.sheet;
    const img = $("[data-preview-img]");
    img.src = r.data.image;
    img.hidden = false;
    $("[data-preview-empty]").hidden = true;
    $("[data-preview-nav]").hidden = m.sheets <= 1;
    $("[data-sheet-label]").textContent = t("print.sheet_of", { n: m.sheet + 1, total: m.sheets });
    const notes = [
      t("print.note_orientation", { o: t("orientation." + m.orientation) }),
      t("print.note_sheets", { pages: m.pages_selected, sheets: m.sheets, physical: m.physical_sheets }),
    ];
    if (m.geometry.source === "estimated") notes.push(t("print.note_estimated"));
    if (m.clipped) notes.push("⚠ " + t("print.note_clipped"));
    $("[data-preview-notes]").replaceChildren(...notes.map((n) => h("li", null, n)));
    renderOrder(m);
    btn.disabled = state.printing;
  }

  function pageLabel(dp, multi) {
    return multi ? t("print.doc_page", { d: dp[0], p: dp[1] }) : t("print.page_short", { p: dp[1] });
  }

  function renderOrder(m) {
    const box = $("[data-print-order]");
    const multi = m.documents > 1;
    const shown = m.order.slice(0, 200);
    $("[data-order-list]").replaceChildren(...shown.map((sheet, i) =>
      h("li", { class: i === m.sheet ? "current" : null },
        h("span", { class: "muted" }, t("print.sheet_n", { n: i + 1 }) + ": "),
        sheet.map((dp) => pageLabel(dp, multi)).join(", "))));
    if (m.order.length > shown.length) $("[data-order-list]").append(h("li", { class: "muted" }, "…"));
    const flat = m.order.flat();
    $("[data-order-summary]").textContent = "(" + flat.slice(0, 12).map((dp) => pageLabel(dp, multi)).join(", ")
      + (flat.length > 12 ? ", …" : "") + ")";
    box.hidden = false;
  }

  /* -------------------------------------------------------------------- print */
  async function submitJob() {
    if (!state.docs.length || state.printing) return;
    if (state.docs.some((d) => d.invalid || selCount(d) === 0)) { renderPreview(); return; }
    state.printing = true;
    const btn = $("[data-print-btn]");
    btn.disabled = true;
    btn.classList.add("busy");
    showError("[data-print-error]", null);
    const r = await api("POST", "/api/jobs", requestBody());
    state.printing = false;
    btn.classList.remove("busy");
    btn.disabled = false;
    if (r.ok) {
      toast(t("print.job_submitted", { id: r.data.job.id }), "ok");
      loadJobs();
    } else if (r.data.fields) {
      showFieldErrors(r.data.fields);
    } else {
      showError("[data-print-error]", r.data.error, r.data);
    }
  }

  /* ------------------------------------------------------------------ presets */
  async function loadPresets(selectId) {
    const r = await api("GET", "/api/presets");
    if (!r.ok) return;
    state.presets = r.data.presets;
    const sel = $("[data-preset-select]");
    sel.replaceChildren(h("option", { value: "" }, t("preset.choose")),
      ...state.presets.map((p) => h("option", { value: p.id, selected: p.id === selectId },
        (p.shared ? "★ " : "") + p.name)));
    updatePresetButtons();
  }

  function updatePresetButtons() {
    const p = state.presets.find((x) => String(x.id) === $("[data-preset-select]").value);
    $("[data-preset-delete]").hidden = !(p && (p.mine || (p.shared && state.catalog && state.catalog.can_share_presets)));
  }

  async function savePreset() {
    const name = (window.prompt(t("preset.name_prompt")) || "").trim();
    if (!name) return;
    const shared = !!(state.catalog && state.catalog.can_share_presets && window.confirm(t("preset.share_confirm")));
    const r = await api("POST", "/api/presets", { name, shared, options: gatherOptions() });
    if (!r.ok) { toast(t(r.data.error, r.data), "error"); return; }
    const p = r.data.presets.find((x) => x.name === name && x.shared === shared);
    await loadPresets(p && p.id);
    toast(t("preset.saved", { name }), "ok");
  }

  async function deletePreset() {
    const id = $("[data-preset-select]").value;
    if (!id || !window.confirm(t("preset.delete_confirm"))) return;
    const r = await api("DELETE", "/api/presets/" + encodeURIComponent(id));
    if (!r.ok) { toast(t(r.data.error), "error"); return; }
    await loadPresets();
  }

  /* -------------------------------------------------------------------- jobs */
  let jobsTimer = null;
  async function loadJobs() {
    clearTimeout(jobsTimer);
    const r = await api("GET", "/api/jobs?scope=recent");
    let anyActive = false;
    if (r.ok) {
      const list = $("[data-job-list]");
      const jobs = r.data.jobs.slice(0, 8);
      if (!jobs.length) list.replaceChildren(h("li", { class: "muted" }, t("print.no_jobs")));
      else list.replaceChildren(...jobs.map((j) => {
        if (j.cancellable) anyActive = true;
        return h("li", { class: "job-item" },
          h("div", { class: "job-main" },
            ...j.parts.map((p) => h("div", { class: "break" }, h("strong", null, p.filename),
              h("span", { class: "muted small" }, " · " + (p.page_range ? t("print.pages_list", { pages: p.page_range }) : t("hist.all_pages"))))),
            h("div", { class: "muted small" }, fmtTime(j.created_at) + " · #" + j.id + " · ×" + j.copies + " · " + j.paper
              + (j.nup > 1 ? " · " + j.nup + "-up" : "")),
            j.error_code ? h("div", { class: "small err-text" }, t(j.error_code)) : null),
          h("div", { class: "job-side" }, badge(j.status),
            j.cancellable ? h("button", { class: "btn btn-sm btn-danger", type: "button", "data-cancel-job": j.id }, t("jobs.cancel")) : null));
      }));
    }
    jobsTimer = setTimeout(loadJobs, anyActive ? 3000 : 15000);
  }

  /* ------------------------------------------------------------------ wiring */
  document.addEventListener("DOMContentLoaded", () => {
    if (!form) return;
    const dz = $("[data-dropzone]");
    const input = $("[data-file-input]");
    dz.addEventListener("click", () => input.click());
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => { const f = [...input.files]; input.value = ""; uploadFiles(f); });
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
    form.addEventListener("change", () => { syncForm(); state.sheet = 0; schedulePreview(); });
    form.addEventListener("input", (e) => { if (e.target.type === "number") { syncForm(); schedulePreview(600); } });
    form.addEventListener("submit", (e) => e.preventDefault());
    $("[data-sheet-prev]").addEventListener("click", () => { if (state.sheet > 0) { state.sheet--; schedulePreview(0); } });
    $("[data-sheet-next]").addEventListener("click", () => { if (state.sheet < state.sheets - 1) { state.sheet++; schedulePreview(0); } });
    $("[data-print-btn]").addEventListener("click", submitJob);
    $("[data-preset-select]").addEventListener("change", (e) => {
      const p = state.presets.find((x) => String(x.id) === e.target.value);
      updatePresetButtons();
      if (p) { applyOptions(p.options); state.sheet = 0; schedulePreview(0); }
    });
    $("[data-preset-save]").addEventListener("click", savePreset);
    $("[data-preset-delete]").addEventListener("click", deletePreset);
    document.addEventListener("lps:jobs-changed", loadJobs);
    loadCatalog().then(() => loadPresets());
    loadJobs();
  });
})();
