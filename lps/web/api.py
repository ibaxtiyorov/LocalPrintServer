"""JSON API used by the user panel."""
from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import shutil
import threading
from collections import OrderedDict

from flask import Blueprint, Response, current_app, g, request

from .. import catalog
from .. import jobs as J
from ..auth import has_perm
from ..db import utcnow
from ..documents import (CompositeDocument, Document, DocumentError, detect_type, inspect_upload,
                         open_document, pdf_available, sanitize_filename)
from ..layout import LayoutError
from ..office import OFFICE_TYPES, OfficeError, detect_office, validate_office
from ..options import OptionsError, PrintOptions, validate_options
from ..printer.base import PrinterError
from ..render import prepare, render_preview
from ..security import print_access_required
from .common import fail, json_body, ok

bp = Blueprint("api", __name__, url_prefix="/api")
log = logging.getLogger("lps.api")

PREVIEW_SOURCE_PX = 1000
THUMB_W, THUMB_H = 200, 280
MAX_PRESETS_PER_OWNER = 30
PRESET_KEYS = ("paper", "custom_w_mm", "custom_h_mm", "media", "quality", "color", "orientation",
               "scaling", "margins_mm", "copies", "collate", "reverse", "nup", "borderless",
               "mirror", "rotate180")


def _svc():
    return current_app.extensions["lps"]


# ------------------------------------------------------------------ caches
class _LRU:
    def __init__(self, size: int):
        self.size = size
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def drop(self, upload_id):
        with self._lock:
            for k in [k for k in self._d if k[0] == upload_id]:
                del self._d[k]


_preview_cache = _LRU(60)        # preview-resolution page images
_thumb_cache = _LRU(800)         # PNG bytes of page thumbnails


class _CachedPreviewDoc(Document):
    def __init__(self, inner: Document, upload_id: str):
        self.inner, self.upload_id = inner, upload_id
        self.doc_type, self.page_count = inner.doc_type, inner.page_count

    def page_info(self, index):
        return self.inner.page_info(index)

    def render_page(self, index, max_w, max_h):
        key = (self.upload_id, index)
        im = _preview_cache.get(key)
        if im is None:
            im = self.inner.render_page(index, PREVIEW_SOURCE_PX, PREVIEW_SOURCE_PX)
            _preview_cache.put(key, im)
        return im

    def close(self):
        self.inner.close()


# ------------------------------------------------------------------ helpers
def _upload_for_owner(upload_id) -> dict | None:
    if not isinstance(upload_id, str) or len(upload_id) > 64:
        return None
    r = _svc().db.one("SELECT * FROM uploads WHERE id = ? AND owner_key = ?",
                      (upload_id, g.owner_key))
    return dict(r) if r else None


class _RequestError(Exception):
    def __init__(self, code, status=400, **data):
        super().__init__(code)
        self.code, self.status, self.data = code, status, data


def _documents(body: dict) -> tuple[list[dict], list[str], bool]:
    """Uploads + selection texts of the request, in print order.

    New form: {"documents": [{"upload_id", "page_range"}, ...], "options": {...}}
    v1.0 form: {"upload_id", "options": {"page_range", ...}}  (still accepted)."""
    docs = body.get("documents")
    legacy = docs is None
    if legacy:
        docs = [{"upload_id": body.get("upload_id"),
                 "page_range": (body.get("options") or {}).get("page_range", "")}]
    if not isinstance(docs, list) or not docs:
        raise _RequestError("err.no_file")
    limit = _svc().settings.get("max_documents_per_job")
    if len(docs) > limit:
        raise _RequestError("err.too_many_documents", max=limit)
    uploads, ranges = [], []
    for i, d in enumerate(docs):
        if not isinstance(d, dict):
            raise _RequestError("err.bad_request")
        up = _upload_for_owner(d.get("upload_id"))
        if not up:
            raise _RequestError("err.upload_expired", 404, document=i)
        uploads.append(up)
        ranges.append(str(d.get("page_range") or "")[:200])
    return uploads, ranges, legacy


def _validate(raw_opts, uploads: list[dict], ranges: list[str], legacy: bool) -> PrintOptions:
    svc = _svc()
    caps = svc.manager.capabilities()
    if legacy:
        return validate_options(raw_opts, svc.settings.all(), svc.manager.borderless_papers(caps),
                                svc.manager.available_papers(caps), uploads[0]["page_count"])
    return validate_options(raw_opts, svc.settings.all(), svc.manager.borderless_papers(caps),
                            svc.manager.available_papers(caps),
                            [u["page_count"] for u in uploads], ranges)


def _can_see_job(job) -> bool:
    return job is not None and (job["owner_key"] == g.owner_key or has_perm(g.user, "admin"))


def _office_types_available() -> list[str]:
    return _svc().office.available_types()


# ------------------------------------------------------------------ catalog
@bp.get("/catalog")
@print_access_required
def get_catalog():
    svc = _svc()
    s = svc.settings.all()
    caps = svc.manager.capabilities()
    avail = svc.manager.available_papers(caps)
    borderless = svc.manager.borderless_papers(caps)
    office_ok = set(_office_types_available())
    allowed = [t for t in s["allowed_types"]
               if (t != "pdf" or pdf_available()) and (t not in OFFICE_TYPES or t in office_ok)]
    return ok(
        papers=[{"key": p.key, "label": p.label, "w": p.width_mm, "h": p.length_mm,
                 "borderless": p.key in borderless} for p in catalog.PAPER_SIZES if p.key in avail],
        custom={"min_w": catalog.CUSTOM_MIN_W_MM, "max_w": catalog.CUSTOM_MAX_W_MM,
                "min_h": catalog.CUSTOM_MIN_H_MM, "max_h": catalog.CUSTOM_MAX_H_MM},
        media=[{"key": m.key, "label": m.label} for m in catalog.MEDIA_TYPES],
        quality=catalog.QUALITY_ORDER, color=list(catalog.COLOR),
        orientation=catalog.ORIENTATION_CHOICES, scaling=catalog.SCALING_CHOICES,
        nup=catalog.NUP_CHOICES,
        defaults={"paper": s["default_paper"], "media": s["default_media"],
                  "quality": s["default_quality"], "color": s["default_color"],
                  "orientation": s["default_orientation"], "scaling": s["default_scaling"],
                  "margins_mm": s["default_margins_mm"]},
        limits={"max_copies": s["max_copies"], "max_upload_mb": s["max_upload_mb"],
                "max_pdf_pages": s["max_pdf_pages"], "max_documents": s["max_documents_per_job"]},
        allowed_types=allowed,
        office_unavailable=[t for t in s["allowed_types"] if t in OFFICE_TYPES and t not in office_ok],
        can_share_presets=has_perm(g.user, "admin"),
    )


# ------------------------------------------------------------------ uploads
def _store_upload(dest, s) -> tuple[str, str, int, int | None]:
    """Validate (and convert Word/Excel). Returns (doc_type, stored_name, pages, conversion_ms)."""
    svc = _svc()
    with open(dest, "rb") as fh:
        head = fh.read(1024)
    if not head:
        raise DocumentError("err.empty_file")
    if detect_type(head) is not None:
        doc_type, pages = inspect_upload(dest, s["allowed_types"], s["max_pdf_pages"])
        return doc_type, dest.name, pages, None
    kind = detect_office(dest, head)
    if kind is None:
        raise DocumentError("err.unsupported_type")
    if kind != "encrypted" and kind not in s["allowed_types"]:
        raise DocumentError("err.unsupported_type")
    validate_office(dest, kind)                       # macros / encryption / external links
    pdf = dest.with_suffix(".pdf")
    try:
        ms = svc.office.convert(dest, kind, pdf)
        _, pages = inspect_upload(pdf, ["pdf"], s["max_pdf_pages"])
    except BaseException:
        pdf.unlink(missing_ok=True)
        raise
    dest.unlink(missing_ok=True)                      # keep only the converted PDF
    return kind, pdf.name, pages, ms


@bp.post("/uploads")
@print_access_required
def upload():
    svc = _svc()
    s = svc.settings.all()
    limit = s["max_upload_mb"] * 1024 * 1024
    if request.content_length and request.content_length > limit + 64 * 1024:
        return fail("err.too_large", 413, max_mb=s["max_upload_mb"])
    f = request.files.get("file")
    if f is None or not f.filename:
        return fail("err.no_file")
    upload_id = secrets.token_hex(12)
    dest = svc.cfg.uploads_dir / (upload_id + ".bin")
    size = 0
    name = sanitize_filename(f.filename)
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = f.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise DocumentError("err.too_large")
                out.write(chunk)
        doc_type, stored, pages, conv_ms = _store_upload(dest, s)
    except (DocumentError, OfficeError) as e:
        dest.unlink(missing_ok=True)
        level = logging.WARNING if isinstance(e, OfficeError) else logging.INFO
        log.log(level, "Upload rejected from %s (%s): %s", g.actor, name, e)
        status = {"err.too_large": 413, "err.office_unavailable": 503, "err.office_busy": 503,
                  "err.office_timeout": 504}.get(e.code, 400)
        return fail(e.code, status, max_mb=s["max_upload_mb"], max_pages=s["max_pdf_pages"])
    except Exception:
        dest.unlink(missing_ok=True)
        dest.with_suffix(".pdf").unlink(missing_ok=True)
        raise
    svc.db.execute("INSERT INTO uploads(id, owner_key, filename, stored_name, doc_type, size,"
                   " page_count, created_at, conversion_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                   (upload_id, g.owner_key, name, stored, doc_type, size, pages, utcnow(), conv_ms))
    log.info("Upload %s by %s: %s (%s, %d page(s), %d bytes%s)", upload_id, g.actor, name,
             doc_type, pages, size, f", converted in {conv_ms} ms" if conv_ms is not None else "")
    return ok(upload={"id": upload_id, "filename": name, "doc_type": doc_type,
                      "pages": pages, "size": size, "converted": conv_ms is not None})


@bp.delete("/uploads/<upload_id>")
@print_access_required
def delete_upload(upload_id):
    up = _upload_for_owner(upload_id)
    if not up:
        return fail("err.not_found", 404)
    svc = _svc()
    (svc.cfg.uploads_dir / up["stored_name"]).unlink(missing_ok=True)
    svc.db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    _preview_cache.drop(upload_id)
    _thumb_cache.drop(upload_id)
    return ok()


@bp.get("/uploads/<upload_id>/thumb/<int:page>")
@print_access_required
def thumbnail(upload_id, page):
    """Small PNG of one page (1-based) for the page-selection grid."""
    up = _upload_for_owner(upload_id)
    if not up or not 1 <= page <= up["page_count"]:
        return fail("err.not_found", 404)
    key = (upload_id, "thumb", page)
    png = _thumb_cache.get(key)
    if png is None:
        svc = _svc()
        try:
            with open_document(svc.cfg.uploads_dir / up["stored_name"], up["doc_type"],
                               pdf_render_dpi=96) as doc:
                im = doc.render_page(page - 1, THUMB_W, THUMB_H)
        except DocumentError as e:
            return fail(e.code, 400)
        im = im.copy()
        im.thumbnail((THUMB_W, THUMB_H))
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        png = buf.getvalue()
        _thumb_cache.put(key, png)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


# ------------------------------------------------------------------ preview / jobs
def _open_preview_doc(uploads: list[dict]) -> CompositeDocument:
    svc = _svc()
    s = svc.settings.all()
    docs = []
    try:
        for up in uploads:
            inner = open_document(svc.cfg.uploads_dir / up["stored_name"], up["doc_type"],
                                  pdf_render_dpi=s["pdf_render_dpi"],
                                  default_image_dpi=s["default_image_dpi"])
            docs.append(_CachedPreviewDoc(inner, up["id"]))
        return CompositeDocument(docs)
    except BaseException:
        for d in docs:
            d.close()
        raise


@bp.post("/preview")
@print_access_required
def preview():
    svc = _svc()
    body = json_body()
    try:
        uploads, ranges, legacy = _documents(body)
        opts = _validate(body.get("options") or {}, uploads, ranges, legacy)
    except _RequestError as e:
        return fail(e.code, e.status, **e.data)
    except OptionsError as e:
        return fail("err.invalid_options", 400, fields=e.errors)
    try:
        sheet = int(body.get("sheet", 0))
    except (TypeError, ValueError):
        sheet = 0
    s = svc.settings.all()
    try:
        with _open_preview_doc(uploads) as doc:
            prep = prepare(doc, opts)
            sheet = max(0, min(sheet, len(prep.plan.sheets) - 1))
            geo = svc.manager.preview_geometry(opts, prep.orientation)
            png, meta = render_preview(doc, prep, geo, sheet, s["nup_gap_mm"])
            # Exact output order: per sheet, the (document, page) pairs placed on it.
            order = [[list(doc.locate(i)) for i in sh] for sh in prep.plan.sheets[:2000]]
    except DocumentError as e:
        return fail(e.code, 400)
    except LayoutError:
        return fail("err.margins", 400, fields={"margins_mm": "err.margins"})
    order = [[[d + 1, p + 1] for d, p in sh] for sh in order]
    return ok(image="data:image/png;base64," + base64.b64encode(png).decode("ascii"),
              meta={**meta, "pages_selected": len(opts.pages),
                    "physical_sheets": meta["sheets"] * opts.copies,
                    "order": order, "documents": len(uploads)})


@bp.post("/jobs")
@print_access_required
def create_job():
    svc = _svc()
    body = json_body()
    try:
        uploads, ranges, legacy = _documents(body)
        opts = _validate(body.get("options") or {}, uploads, ranges, legacy)
    except _RequestError as e:
        return fail(e.code, e.status, **e.data)
    except OptionsError as e:
        return fail("err.invalid_options", 400, fields=e.errors)
    if svc.jobs.count_active(g.owner_key) >= svc.settings.get("max_active_jobs_per_user"):
        return fail("err.too_many_jobs", 429)
    parts, copied = [], []
    try:
        for up in uploads:
            src = svc.cfg.uploads_dir / up["stored_name"]
            if not src.exists():
                raise _RequestError("err.upload_expired", 404)
            stored = secrets.token_hex(12) + src.suffix
            shutil.copyfile(src, svc.cfg.jobs_dir / stored)
            copied.append(stored)
            parts.append({"filename": up["filename"], "doc_type": up["doc_type"],
                          "stored_name": stored, "file_size": up["size"],
                          "page_count": up["page_count"]})
        job_id = svc.jobs.create(user_id=g.user["id"] if g.user else None, username=g.actor,
                                 owner_key=g.owner_key, client_ip=request.remote_addr,
                                 parts=parts, opts=opts)
    except _RequestError as e:
        for name in copied:
            (svc.cfg.jobs_dir / name).unlink(missing_ok=True)
        return fail(e.code, e.status)
    except BaseException:
        for name in copied:
            (svc.cfg.jobs_dir / name).unlink(missing_ok=True)
        raise
    log.info("Job %s created by %s from %s: %s [%s] pages=%d", job_id, g.actor,
             request.remote_addr, " + ".join(p["filename"] for p in parts), opts.summary(),
             len(opts.pages))
    svc.manager.enqueue(job_id)
    job = svc.jobs.get(job_id)
    if job["status"] == J.FAILED:
        return fail(job["error_code"] or "err.internal", 400, job=J.public_job(job))
    return ok(job=J.public_job(job))


@bp.get("/jobs")
@print_access_required
def list_jobs():
    svc = _svc()
    scope = request.args.get("scope", "recent")
    rows, _ = svc.jobs.search(owner_key=g.owner_key,
                              status="ACTIVE" if scope == "active" else None, limit=20)
    return ok(jobs=[J.public_job(r) for r in rows])


@bp.get("/jobs/<job_id>")
@print_access_required
def get_job(job_id):
    job = _svc().jobs.get(job_id[:32])
    if not _can_see_job(job):
        return fail("err.not_found", 404)
    return ok(job=J.public_job(job, include_admin=has_perm(g.user, "admin")))


@bp.post("/jobs/<job_id>/cancel")
@print_access_required
def cancel_job(job_id):
    svc = _svc()
    job = svc.jobs.get(job_id[:32])
    if not _can_see_job(job):
        return fail("err.not_found", 404)
    try:
        result = svc.manager.cancel(job["id"], g.actor)
    except PrinterError as e:
        svc.audit.record(g.actor, "job.cancel", job["id"], "FAILED", details={"error": e.code},
                         ip=request.remote_addr)
        return fail(e.code, 409)
    svc.audit.record(g.actor, "job.cancel", job["id"], "OK", details={"result": result},
                     ip=request.remote_addr)
    return ok(result=result, job=J.public_job(svc.jobs.get(job["id"])))


# ------------------------------------------------------------------ presets
def _preset_dict(r) -> dict:
    return {"id": r["id"], "name": r["name"], "shared": r["owner_key"] == "*",
            "options": json.loads(r["options_json"]),
            "mine": r["owner_key"] == g.owner_key}


@bp.get("/presets")
@print_access_required
def list_presets():
    rows = _svc().db.query("SELECT * FROM presets WHERE owner_key IN (?, '*') ORDER BY"
                           " owner_key = '*' DESC, name COLLATE NOCASE", (g.owner_key,))
    return ok(presets=[_preset_dict(r) for r in rows])


@bp.post("/presets")
@print_access_required
def save_preset():
    svc = _svc()
    body = json_body()
    name = " ".join(str(body.get("name") or "").split())[:40]
    if not name:
        return fail("err.preset_name")
    shared = bool(body.get("shared"))
    if shared and not has_perm(g.user, "admin"):
        return fail("err.forbidden", 403)
    # Store only validated print settings (never page selections or documents).
    raw = {k: v for k, v in (body.get("options") or {}).items() if k in PRESET_KEYS}
    caps = svc.manager.capabilities()
    try:
        o = validate_options(raw, svc.settings.all(), svc.manager.borderless_papers(caps),
                             svc.manager.available_papers(caps), 1)
    except OptionsError as e:
        return fail("err.invalid_options", 400, fields=e.errors)
    options = {k: v for k, v in o.to_dict().items() if k in PRESET_KEYS}
    owner = "*" if shared else g.owner_key
    count = svc.db.one("SELECT COUNT(*) AS n FROM presets WHERE owner_key = ?", (owner,))["n"]
    if count >= MAX_PRESETS_PER_OWNER:
        return fail("err.too_many_presets", 409, max=MAX_PRESETS_PER_OWNER)
    with svc.db.transaction() as c:
        c.execute("DELETE FROM presets WHERE owner_key = ? AND name = ?", (owner, name))
        c.execute("INSERT INTO presets(owner_key, name, options_json, created_by, created_at)"
                  " VALUES (?,?,?,?,?)", (owner, name, json.dumps(options), g.actor, utcnow()))
    if shared:
        svc.audit.record(g.actor, "preset.share", name, "OK", ip=request.remote_addr)
    return list_presets()


@bp.delete("/presets/<int:preset_id>")
@print_access_required
def delete_preset(preset_id):
    svc = _svc()
    r = svc.db.one("SELECT * FROM presets WHERE id = ?", (preset_id,))
    if not r or not (r["owner_key"] == g.owner_key
                     or (r["owner_key"] == "*" and has_perm(g.user, "admin"))):
        return fail("err.not_found", 404)
    svc.db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
    if r["owner_key"] == "*":
        svc.audit.record(g.actor, "preset.delete_shared", r["name"], "OK", ip=request.remote_addr)
    return ok()


# ------------------------------------------------------------------ status
@bp.get("/status")
@print_access_required
def status():
    from ..status import StatusService
    return ok(status=StatusService.public(_svc().status.get()))
