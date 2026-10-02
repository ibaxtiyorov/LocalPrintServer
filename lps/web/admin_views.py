"""Admin panel: dashboard, jobs, printer & maintenance, users, settings, logs, audit."""
from __future__ import annotations

import importlib.metadata as md
import logging
import os
import platform
import secrets
import shutil
import sys
import time

from flask import (Blueprint, current_app, flash, g, redirect, render_template, request,
                   url_for)

from .. import catalog
from .. import jobs as J
from ..auth import ROLE_ORDER, UserError, public_user
from ..logging_setup import tail_log
from ..options import PrintOptions
from ..platform_info import windows_session_id
from ..printer.base import PrinterError
from ..security import admin_required, is_local_request
from ..settings_service import FILE_TYPES, LOG_LEVELS, LOGIN_MODES, SPEC
from .common import date_filters, fail, json_body, ok, page_args

bp = Blueprint("admin", __name__, url_prefix="/admin")
log = logging.getLogger("lps.admin")
_STARTED = time.time()


def _svc():
    return current_app.extensions["lps"]


def _audit(action, target=None, result="OK", **details):
    _svc().audit.record(g.actor, action, target, result, details=details or None,
                        ip=request.remote_addr)


# ====================================================================== pages
@bp.route("/")
@admin_required
def dashboard():
    svc = _svc()
    recent_failed, _ = svc.jobs.search(status=J.FAILED, limit=5)
    return render_template("admin/dashboard.html", status=svc.status.get(force=True),
                           counts=svc.jobs.counts(), states=J.ALL_STATES,
                           recent_failed=recent_failed)


@bp.route("/jobs")
@admin_required
def jobs_page():
    svc = _svc()
    page, per = page_args(50)
    status = request.args.get("status") or None
    if status and status not in J.ALL_STATES and status != "ACTIVE":
        status = None
    df, dt = date_filters()
    rows, total = svc.jobs.search(username=(request.args.get("user") or "").strip()[:64] or None,
                                  status=status,
                                  text=(request.args.get("q") or "").strip()[:100] or None,
                                  job_id=(request.args.get("job") or "").strip()[:32] or None,
                                  date_from=df, date_to=dt, limit=per, offset=(page - 1) * per)
    rows = [dict(r, parts=svc.jobs.parts(r)) for r in rows]
    return render_template("admin/jobs.html", jobs=rows, total=total, page=page, per=per,
                           states=J.ALL_STATES, active_states=J.ACTIVE, args=request.args)


@bp.route("/printer")
@admin_required
def printer_page():
    svc = _svc()
    details, error = None, None
    try:
        details = svc.backend.printer_details()
    except PrinterError as e:
        error = e.code
    except Exception:
        log.exception("printer_details failed")
        error = "err.internal"
    caps = (details or {}).get("capabilities") or {}
    paper_ids = set(caps.get("paper_ids") or [])
    names = caps.get("paper_names") or {}
    paper_rows = [{"key": p.key, "label": p.label, "dm": p.dm_paper,
                   "advertised": (p.dm_paper in paper_ids) if paper_ids else None,
                   "driver_name": names.get(p.dm_paper) or names.get(str(p.dm_paper)),
                   "borderless_id": (caps.get("borderless") or {}).get(p.key)}
                  for p in catalog.PAPER_SIZES]
    session_id = windows_session_id()
    can_launch = is_local_request() and session_id not in (0, None)
    return render_template(
        "admin/printer.html", details=details, error=error, paper_rows=paper_rows,
        media=catalog.MEDIA_TYPES, status=svc.status.get(force=True),
        prop_cmd=svc.backend.properties_command(False),
        pref_cmd=svc.backend.properties_command(True), can_launch=can_launch,
        session_id=session_id, diagnostics=_diagnostics(session_id),
        office=svc.office.status())


def _diagnostics(session_id):
    svc = _svc()
    vers = {}
    for pkg in ("Flask", "Werkzeug", "pillow", "pymupdf", "pywin32", "waitress"):
        try:
            vers[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            vers[pkg] = "not installed"
    try:
        du = shutil.disk_usage(svc.cfg.data_dir)
        disk = f"{du.free / 2**30:.1f} GiB free of {du.total / 2**30:.1f} GiB"
    except OSError:
        disk = "?"
    db_size = svc.cfg.db_path.stat().st_size if svc.cfg.db_path.exists() else 0
    m = svc.manager
    return {
        "python": sys.version.split()[0], "platform": platform.platform(), "packages": vers,
        "pid": os.getpid(), "windows_session": session_id,
        "service_mode": session_id == 0, "uptime_min": round((time.time() - _STARTED) / 60, 1),
        "bind": f"{svc.cfg['host']}:{svc.cfg['port']}", "backend": svc.backend.kind,
        "printer_name": svc.backend.printer_name, "data_dir": str(svc.cfg.data_dir),
        "db_size_kb": round(db_size / 1024), "disk": disk, "threads": m.threads_alive(),
        "worker_heartbeat": m.worker_heartbeat.isoformat() if m.worker_heartbeat else None,
        "monitor_heartbeat": m.monitor_heartbeat.isoformat() if m.monitor_heartbeat else None,
        "spooler_down_since": m.spooler_down_since.isoformat() if m.spooler_down_since else None,
        "last_spooler_restart": (m.last_spooler_restart.isoformat()
                                 if m.last_spooler_restart else None),
    }


@bp.route("/users")
@admin_required
def users_page():
    users = [public_user(u) for u in _svc().users.list()]
    return render_template("admin/users.html", users=users, role_order=ROLE_ORDER)


@bp.post("/users/create")
@admin_required
def user_create():
    svc = _svc()
    f = request.form
    username = (f.get("username") or "").strip()
    if (f.get("password") or "") != (f.get("password2") or ""):
        flash(("error", "err.password_mismatch"))
        return redirect(url_for("admin.users_page"))
    try:
        uid = svc.users.create(username, f.get("password") or "", f.get("role", "USER"),
                               f.get("display_name", ""), must_change=bool(f.get("must_change")))
        _audit("user.create", username, role=f.get("role"), user_id=uid)
        flash(("ok", "msg.user_created"))
    except UserError as e:
        _audit("user.create", username, "FAILED", error=e.code)
        flash(("error", e.code))
    return redirect(url_for("admin.users_page"))


@bp.post("/users/<int:uid>/<action>")
@admin_required
def user_action(uid, action):
    svc = _svc()
    target = svc.users.get(uid)
    if not target:
        flash(("error", "err.not_found"))
        return redirect(url_for("admin.users_page"))
    name = target["username"]
    try:
        if action == "enable":
            svc.users.set_active(uid, True)
        elif action == "disable":
            if uid == g.user["id"]:
                raise UserError("err.cannot_disable_self")
            svc.users.set_active(uid, False)
        elif action == "role":
            role = request.form.get("role", "")
            if uid == g.user["id"] and role != "ADMIN":
                raise UserError("err.cannot_demote_self")
            svc.users.set_role(uid, role)
        elif action == "password":
            p1, p2 = request.form.get("password") or "", request.form.get("password2") or ""
            if p1 != p2:
                raise UserError("err.password_mismatch")
            svc.users.set_password(uid, p1, must_change=bool(request.form.get("must_change")))
        elif action == "rename":
            svc.users.update_display_name(uid, request.form.get("display_name", ""))
        else:
            raise UserError("err.bad_request")
        _audit(f"user.{action}", name, role=request.form.get("role") if action == "role" else None)
        flash(("ok", "msg.saved"))
    except UserError as e:
        _audit(f"user.{action}", name, "FAILED", error=e.code)
        flash(("error", e.code))
    return redirect(url_for("admin.users_page"))


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_page():
    svc = _svc()
    errors = {}
    if request.method == "POST":
        updates = {}
        for key, (default, _v, _grp) in SPEC.items():
            if key == "queue_paused":
                continue            # controlled from Maintenance
            if isinstance(default, bool):
                updates[key] = key in request.form
            elif key == "allowed_types":
                updates[key] = request.form.getlist("allowed_types")
            elif key in request.form:
                updates[key] = request.form.get(key)
        changed, errors = svc.settings.update(updates, g.actor)
        if errors:
            _audit("settings.update", None, "FAILED", errors=errors)
            flash(("error", "err.invalid_settings"))
        else:
            if changed:
                _audit("settings.update", ",".join(changed),
                       changes={k: {"old": o, "new": n} for k, (o, n) in changed.items()})
            flash(("ok", "msg.saved"))
            return redirect(url_for("admin.settings_page"))
    return render_template("admin/settings.html", values=svc.settings.all(), errors=errors,
                           spec=SPEC, catalog=catalog, file_types=FILE_TYPES,
                           log_levels=LOG_LEVELS, login_modes=LOGIN_MODES, cfg=svc.cfg.values)


@bp.route("/logs")
@admin_required
def logs_page():
    svc = _svc()
    level = request.args.get("level") or None
    q = (request.args.get("q") or "").strip()[:100] or None
    try:
        n = max(50, min(5000, int(request.args.get("n", 500))))
    except ValueError:
        n = 500
    lines = tail_log(svc.log_file, n, level, q)
    return render_template("admin/logs.html", lines=lines, args=request.args,
                           levels=["DEBUG", "INFO", "WARNING", "ERROR"])


@bp.route("/audit")
@admin_required
def audit_page():
    svc = _svc()
    page, per = page_args(50)
    df, dt = date_filters()
    rows, total = svc.audit.search(actor=(request.args.get("actor") or "").strip()[:64] or None,
                                   action=request.args.get("action") or None,
                                   result=request.args.get("result") or None,
                                   text=(request.args.get("q") or "").strip()[:100] or None,
                                   date_from=df, date_to=dt, limit=per, offset=(page - 1) * per)
    return render_template("admin/audit.html", rows=rows, total=total, page=page, per=per,
                           actions=svc.audit.actions(), args=request.args)


# ====================================================================== API
@bp.get("/api/status")
@admin_required
def api_status():
    return ok(status=_svc().status.get(force=request.args.get("force") == "1"))


@bp.get("/api/queue")
@admin_required
def api_queue():
    svc = _svc()
    active = [J.public_job(j, include_admin=True) for j in svc.jobs.by_status(J.ACTIVE)]
    win, err = [], None
    try:
        win = [sj.to_dict() for sj in svc.backend.list_jobs()]
    except PrinterError as e:
        err = e.code
    app_ids = {j["spooler_job_id"]: j["id"] for j in active if j["spooler_job_id"]}
    for w in win:
        w["app_job"] = app_ids.get(w["job_id"]) or (
            w["document"][4:16] if w["document"].startswith("LPS-") else None)
    return ok(app_jobs=active, windows_jobs=win, windows_error=err,
              app_paused=svc.settings.get("queue_paused"))


@bp.post("/api/jobs/<job_id>/cancel")
@admin_required
def api_cancel(job_id):
    svc = _svc()
    job = svc.jobs.get(job_id[:32])
    if not job:
        return fail("err.not_found", 404)
    try:
        r = svc.manager.cancel(job["id"], g.actor)
    except PrinterError as e:
        _audit("job.cancel", job["id"], "FAILED", error=e.code)
        return fail(e.code, 409)
    _audit("job.cancel", job["id"], result=r)
    return ok(result=r)


@bp.post("/api/jobs/cancel-all")
@admin_required
def api_cancel_all():
    r = _svc().manager.cancel_all(g.actor)
    _audit("queue.cancel_all", "app-queue", **r)
    return ok(result=r)


@bp.post("/api/windows-jobs/<int:wid>/cancel")
@admin_required
def api_cancel_windows(wid):
    svc = _svc()
    # If the Windows job belongs to an app job, go through the normal cancel path.
    for j in svc.jobs.by_status(J.IN_SPOOLER):
        if j["spooler_job_id"] == wid:
            return api_cancel(j["id"])
    try:
        svc.backend.cancel_spooler_job(wid)
    except PrinterError as e:
        _audit("spooler.cancel_job", str(wid), "FAILED", error=e.code)
        return fail(e.code, 409)
    _audit("spooler.cancel_job", str(wid))
    return ok()


@bp.post("/api/queue/<action>")
@admin_required
def api_app_queue(action):
    if action not in ("pause", "resume"):
        return fail("err.bad_request")
    _svc().settings.update({"queue_paused": action == "pause"}, g.actor)
    _audit(f"queue.{action}", "app-queue")
    return ok()


@bp.post("/api/printer/<action>")
@admin_required
def api_printer(action):
    svc = _svc()
    try:
        if action == "pause":
            svc.backend.set_paused(True)
        elif action == "resume":
            svc.backend.set_paused(False)
        elif action == "purge":
            svc.backend.purge()
            svc.manager.note_spooler_restart()   # tracked jobs vanish; mark outcome unknown
        elif action == "refresh":
            svc.manager.invalidate_geometry()
            svc.status.get(force=True)
        elif action in ("properties", "preferences"):
            if not is_local_request() or windows_session_id() in (0, None):
                return fail("err.properties_remote", 409)
            svc.backend.launch_properties(action == "preferences")
        else:
            return fail("err.bad_request")
    except PrinterError as e:
        _audit(f"printer.{action}", svc.backend.printer_name, "FAILED", error=e.code,
               detail=e.detail[:300])
        return fail(e.code, 409)
    if action != "refresh":
        _audit(f"printer.{action}", svc.backend.printer_name)
    return ok()


@bp.post("/api/spooler/restart")
@admin_required
def api_spooler_restart():
    svc = _svc()
    try:
        svc.backend.restart_spooler()
    except PrinterError as e:
        _audit("spooler.restart", "Spooler", "FAILED", error=e.code, detail=e.detail[:300])
        return fail(e.code, 409)
    svc.manager.note_spooler_restart()
    _audit("spooler.restart", "Spooler")
    return ok()


@bp.post("/api/driver-check")
@admin_required
def api_driver_check():
    from ..diagnostics import driver_check
    svc = _svc()
    try:
        result = driver_check(svc.backend, svc.settings.get("devmode_merge"))
    except PrinterError as e:
        _audit("printer.driver_check", svc.backend.printer_name, "FAILED", error=e.code)
        return fail(e.code, 409)
    _audit("printer.driver_check", svc.backend.printer_name, **result["summary"])
    return ok(result=result)


@bp.post("/api/office-test")
@admin_required
def api_office_test():
    """Convert built-in sample .docx/.xlsx files (no printing) to verify the converter."""
    import tempfile
    from pathlib import Path
    from ..documents import PdfDocument
    from ..office import OfficeError
    from ..office_samples import make_docx, make_xlsx
    svc = _svc()
    results = []
    with tempfile.TemporaryDirectory(dir=svc.office.tmp_root) as tmp:
        for kind, data in (("docx", make_docx(2)), ("xlsx", make_xlsx(2))):
            src, out = Path(tmp) / f"sample.{kind}", Path(tmp) / f"sample_{kind}.pdf"
            src.write_bytes(data)
            engine, _ = svc.office.engine_for(kind)
            try:
                ms = svc.office.convert(src, kind, out)
                with PdfDocument(out) as d:
                    results.append({"kind": kind, "engine": engine, "ok": True, "ms": ms,
                                    "pages": d.page_count})
            except OfficeError as e:
                results.append({"kind": kind, "engine": engine, "ok": False, "error": e.code,
                                "detail": e.detail[:500]})
    _audit("office.test", "converter", "OK" if all(r["ok"] for r in results) else "FAILED",
           results=[{k: r.get(k) for k in ("kind", "engine", "ok", "error")} for r in results])
    return ok(results=results)


@bp.post("/api/test-page")
@admin_required
def api_test_page():
    from ..testpage import make_test_page
    svc = _svc()
    color = bool(json_body().get("color"))
    stored = secrets.token_hex(12) + ".bin"
    path = svc.cfg.jobs_dir / stored
    make_test_page(path, svc.backend.printer_name, color, g.actor)
    opts = PrintOptions(paper="A4", media="PLAIN", quality="DRAFT",
                        color="COLOR" if color else "MONO", orientation="PORTRAIT",
                        scaling="ACTUAL", pages=[0])
    job_id = svc.jobs.create(user_id=g.user["id"], username=g.actor, owner_key=g.owner_key,
                             client_ip=request.remote_addr, filename="test-page.png",
                             doc_type="png", stored_name=stored, file_size=path.stat().st_size,
                             page_count=1, opts=opts)
    svc.manager.enqueue(job_id)
    _audit("printer.test_page", job_id, color=color)
    return ok(job_id=job_id)


@bp.post("/api/sim")
@admin_required
def api_sim():
    svc = _svc()
    if svc.backend.kind != "simulated":
        return fail("err.not_supported", 409)
    body = json_body()
    if "usb" in body:
        svc.backend.set_usb(bool(body["usb"]))
    if "spooler" in body:
        svc.backend.set_spooler(bool(body["spooler"]))
    _audit("sim.control", "simulator", **body)
    return ok()
