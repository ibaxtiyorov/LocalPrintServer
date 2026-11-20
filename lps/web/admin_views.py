"""Admin panel: dashboard, jobs, printer & maintenance, users, devices, settings, logs, audit."""
from __future__ import annotations

import importlib.metadata as md
import logging
import os
import platform
import secrets
import shutil
import sys
import time

from flask import (Blueprint, Response, current_app, flash, g, redirect, render_template, request,
                   send_file, url_for)

from .. import catalog
from .. import jobs as J
from ..auth import ROLE_ORDER, USERNAME_RE, UserError, public_user
from ..logging_setup import tail_log
from ..options import PrintOptions
from ..platform_info import windows_session_id
from ..printer.base import PrinterError
from ..security import DEVICE_COOKIE_NAME, DEVICE_ID_RE, admin_required, is_local_request, normalise_print_name, resolve_client_ip
from ..settings_service import FILE_TYPES, LOG_LEVELS, LOGIN_MODES, SPEC
from .common import date_filters, fail, json_body, ok, page_args

bp = Blueprint("admin", __name__, url_prefix="/admin")
log = logging.getLogger("lps.admin")
_STARTED = time.time()


def _svc():
    return current_app.extensions["lps"]


def _ip() -> str:
    return getattr(g, "client_ip", None) or resolve_client_ip()


def _audit(action, target=None, result="OK", **details):
    _svc().audit.record(g.actor, action, target, result, details=details or None,
                        ip=_ip())


def _collect_health() -> dict:
    svc = _svc()
    disk_target = str(svc.cfg.data_dir)
    try:
        import psutil
        cpu_pct = round(float(psutil.cpu_percent(interval=0.1)), 1)
        vm = psutil.virtual_memory()
        ram_pct = round(float(vm.percent), 1)
        du = psutil.disk_usage(disk_target)
        disk_free_gb = round(du.free / (1024 ** 3), 2)
        disk_total_gb = round(du.total / (1024 ** 3), 2)
    except Exception:
        cpu_pct = 0.0
        ram_pct = 0.0
        du = shutil.disk_usage(disk_target)
        disk_free_gb = round(du.free / (1024 ** 3), 2)
        disk_total_gb = round(du.total / (1024 ** 3), 2)
    return {
        "cpu_percent": cpu_pct,
        "ram_percent": ram_pct,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
    }


# ====================================================================== pages
@bp.route("/")
@admin_required
def dashboard():
    svc = _svc()
    recent_failed, _ = svc.jobs.search(status=J.FAILED, limit=5)
    return render_template("admin/dashboard.html", status=svc.status.get(force=True),
                           counts=svc.jobs.counts(), states=J.ALL_STATES,
                           recent_failed=recent_failed,
                           health=_collect_health(),
                           update_available=bool(getattr(svc.manager, "update_available", False)))


@bp.post("/trigger-update")
@admin_required
def trigger_update():
    import subprocess
    svc = _svc()
    _audit("system.trigger_update", "update.ps1")
    svc.manager.update_available = False
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    subprocess.Popen(
        ["powershell.exe", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", r"C:\LocalPrintServer\update.ps1"],
        creationflags=flags,
        close_fds=True
    )
    return render_template("admin/updating.html"), 200


@bp.route("/trigger-cancel-update", methods=["POST"])
@admin_required
def cancel_update():
    import subprocess
    from ..config import BASE_DIR
    svc = _svc()
    svc.manager.update_available = False
    remote_hash = getattr(svc.manager, "latest_remote_hash", "") or ""
    if not remote_hash:
        try:
            res = subprocess.run(
                ["git", "-c", "safe.directory=*", "rev-parse", "origin/main"],
                cwd=str(BASE_DIR), timeout=10, capture_output=True, text=True, check=False
            )
            if res.returncode == 0 and (res.stdout or "").strip():
                remote_hash = res.stdout.strip()
            else:
                ls_res = subprocess.run(
                    ["git", "-c", "safe.directory=*", "ls-remote", "origin", "refs/heads/main"],
                    cwd=str(BASE_DIR), timeout=15, capture_output=True, text=True, check=False
                )
                if ls_res.returncode == 0 and (ls_res.stdout or "").strip():
                    remote_hash = ls_res.stdout.strip().split()[0]
        except Exception:
            pass
    if remote_hash:
        svc.jobs.db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('ignored_update_hash', ?)",
            (remote_hash,)
        )
    svc.jobs.db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('update_available', '0')"
    )
    _audit("system.cancel_update", remote_hash or "dismissed")
    return redirect(url_for("admin.dashboard"))


@bp.route("/jobs")
@admin_required
def jobs_page():
    svc = _svc()
    page, per = page_args(50)
    status = request.args.get("status") or None
    if status and status not in J.ALL_STATES and status != "ACTIVE":
        status = None
    df, dt = date_filters()
    filter_device = (request.args.get("filter_device") or request.args.get("device") or "").strip()[:64] or None
    filter_ip = (request.args.get("filter_ip") or "").strip()[:64] or None
    rows, total = svc.jobs.search(username=(request.args.get("user") or "").strip()[:64] or None,
                                  device_id=filter_device,
                                  client_ip=filter_ip,
                                  status=status,
                                  text=(request.args.get("q") or "").strip()[:100] or None,
                                  job_id=(request.args.get("job") or "").strip()[:32] or None,
                                  date_from=df, date_to=dt, limit=per, offset=(page - 1) * per)
    enriched = []
    for r in rows:
        parts = svc.jobs.parts(r)
        _, ptype, _ = svc.jobs.resolve_part_file(r, 0)
        raw_url = url_for("api.job_raw_file", job_id=r["id"], part_idx=0)
        enriched.append(dict(r, parts=parts, preview_type=ptype, preview_url=raw_url, download_url=raw_url))
    return render_template("admin/jobs.html", jobs=enriched, total=total, page=page, per=per,
                           states=J.ALL_STATES, active_states=J.ACTIVE, args=request.args)


@bp.get("/jobs/<job_id>/preview/<int:part_idx>.<ext>")
@bp.get("/jobs/<job_id>/download")
@bp.get("/jobs/<job_id>/download/<int:part_idx>.<ext>")
@admin_required
def download_job_file(job_id: str, part_idx: int = 0, ext: str = "bin"):
    svc = _svc()
    job = svc.jobs.get(job_id[:32])
    if not job:
        return fail("err.not_found", 404)
    path, ptype, mime = svc.jobs.resolve_part_file(job, part_idx)
    if not path or not path.exists():
        return fail("err.not_found", 404)
    dl_name = f"job-{job['id']}-{part_idx}.{'pdf' if ptype == 'pdf' else path.suffix.lstrip('.') or 'bin'}"
    resp = send_file(path, mimetype=mime, as_attachment=False, download_name=dl_name)
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


job_file_preview = download_job_file


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
    return render_template("admin/users.html", users=users, role_order=ROLE_ORDER, current_user=g.user)


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


@bp.route("/users/edit/<int:user_id>", methods=["POST"])
@admin_required
def user_edit(user_id: int):
    return _handle_user_edit(user_id)


def _handle_user_edit(user_id: int):
    from ..db import utcnow
    svc = _svc()
    target_user = svc.users.get(user_id)
    if not target_user:
        flash(("error", "err.not_found"))
        return redirect(url_for("admin.users_page"))
    current_user = g.user
    is_root_target = target_user["username"].lower() == "admin" or target_user["id"] == 1
    if is_root_target and current_user["username"].lower() != "admin":
        _audit("user.edit", target_user["username"], "FAILED", error="forbidden_root_admin")
        return "Forbidden", 403

    f = request.form
    new_username = (f.get("new_username") if "new_username" in f else f.get("username") or "").strip()
    has_name_field = any(k in f for k in ("new_name", "name", "display_name"))
    new_name = (
        f.get("new_name")
        if "new_name" in f
        else (f.get("name") if "name" in f else f.get("display_name", ""))
    )
    new_password = (f.get("new_password") if "new_password" in f else f.get("password")) or ""
    password2 = f.get("password2")

    try:
        # Strict Root Lock: never allow changing Root Admin username
        if not is_root_target and new_username and new_username != target_user["username"]:
            if not USERNAME_RE.match(new_username):
                raise UserError("err.username_invalid")
            existing = svc.users.by_username(new_username)
            if existing and existing["id"] != user_id:
                raise UserError("err.username_taken")
            svc.db.execute(
                "UPDATE users SET username = ?, updated_at = ? WHERE id = ?",
                (new_username, utcnow(), user_id),
            )
            svc.db.execute(
                "UPDATE jobs SET username = ? WHERE user_id = ?",
                (new_username, user_id),
            )

        if has_name_field:
            svc.users.update_display_name(user_id, new_name or "")

        if new_password or (password2 is not None and password2 != ""):
            if password2 is not None and new_password != password2:
                raise UserError("err.password_mismatch")
            svc.users.set_password(user_id, new_password, must_change=bool(f.get("must_change")))

        _audit("user.edit", target_user["username"], user_id=user_id)
        flash(("ok", "msg.saved"))
    except UserError as e:
        _audit("user.edit", target_user["username"], "FAILED", error=e.code)
        flash(("error", e.code))
    return redirect(url_for("admin.users_page"))


@bp.route("/users/delete/<int:user_id>", methods=["POST"])
@admin_required
def user_delete(user_id: int):
    svc = _svc()
    target_user = svc.users.get(user_id)
    if not target_user:
        flash(("error", "err.not_found"))
        return redirect(url_for("admin.users_page"))
    current_user = g.user
    if target_user["username"].lower() == "admin" or target_user["id"] == 1 or user_id == current_user["id"]:
        _audit("user.delete", target_user["username"], "FAILED", error="forbidden_root_or_self")
        flash(("error", "err.cannot_disable_self"))
        return "Forbidden", 403
    svc.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    _audit("user.delete", target_user["username"], user_id=user_id)
    flash(("ok", "msg.saved"))
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
    current_user = g.user
    if (target["username"].lower() == "admin" or target["id"] == 1) and current_user["username"].lower() != "admin":
        _audit(f"user.{action}", name, "FAILED", error="forbidden_root_admin")
        return "Forbidden", 403
    try:
        if action == "enable":
            svc.users.set_active(uid, True)
        elif action == "disable":
            if target["username"].lower() == "admin" or target["id"] == 1 or uid == g.user["id"]:
                _audit("user.disable", name, "FAILED", error="err.cannot_disable_self")
                flash(("error", "err.cannot_disable_self"))
                return "Forbidden", 403
            svc.users.set_active(uid, False)
        elif action == "role":
            role = request.form.get("role", "")
            if uid == g.user["id"] and role != "ADMIN":
                raise UserError("err.cannot_demote_self")
            svc.users.set_role(uid, role)
        elif action in ("password", "edit"):
            return _handle_user_edit(uid)
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


@bp.route("/devices")
@admin_required
def devices_page():
    svc = _svc()
    current_admin_id = (
        request.cookies.get(DEVICE_COOKIE_NAME)
        or request.cookies.get("device_id")
        or getattr(g, "device_id", None)
        or ""
    ).strip()
    svc.db.execute(
        "UPDATE devices SET last_known_name = COALESCE("
        "(SELECT username FROM jobs WHERE jobs.device_id = devices.device_id AND username != '' "
        "ORDER BY created_at DESC, seq DESC LIMIT 1), '') "
        "WHERE last_known_name IS NULL OR last_known_name = ''"
    )
    svc.db.execute(
        "DELETE FROM devices WHERE (custom_name IS NULL OR custom_name = '') "
        "AND (last_known_name IS NULL OR last_known_name = '')"
    )
    devices = []
    for device in svc.db.list_devices():
        dev_id = device.get("device_id") or ""
        device["id"] = dev_id
        device["ip"] = device.get("last_ip") or ""
        row = None
        if dev_id:
            row = svc.db.one(
                "SELECT username FROM jobs WHERE device_id = ? AND username != '' "
                "ORDER BY created_at DESC, seq DESC LIMIT 1",
                (dev_id,),
            )
        job_name = row["username"] if row and row["username"] else None
        stored_lname = (device.get("last_known_name") or "").strip() or None
        device["last_known_name"] = job_name or stored_lname
        if (device.get("custom_name") or "").strip() or (device.get("last_known_name") or "").strip():
            devices.append(device)
    return render_template("admin/devices.html", devices=devices, current_admin_id=current_admin_id)


@bp.post("/devices/create")
@admin_required
def device_create():
    svc = _svc()
    raw_id = (request.form.get("device_id") or "").strip()
    if not raw_id:
        raw_id = secrets.token_hex(12)
    if not DEVICE_ID_RE.match(raw_id):
        _audit("device.create", raw_id, "FAILED", error="err.device_id_invalid")
        flash(("error", "err.device_id_invalid"))
        return redirect(url_for("admin.devices_page"))
    raw_name = (request.form.get("custom_name") or "").strip()[:80]
    name = ""
    if raw_name:
        norm = normalise_print_name(raw_name)
        if not norm:
            _audit("device.create", raw_id, "FAILED", error="err.print_name_invalid")
            flash(("error", "err.print_name_invalid"))
            return redirect(url_for("admin.devices_page"))
        name = norm
    ip = (request.form.get("last_ip") or "").strip()[:64] or None
    svc.db.touch_device(raw_id, ip=ip, custom_name=name)
    if name:
        svc.db.set_device_custom_name(raw_id, name)
    _audit("device.create", raw_id, custom_name=name, ip=ip)
    flash(("ok", "msg.device_saved"))
    return redirect(url_for("admin.devices_page"))


@bp.post("/devices/<device_id>/<action>")
@admin_required
def device_action(device_id, action):
    svc = _svc()
    dev = svc.db.get_device(device_id)
    if not dev:
        flash(("error", "err.not_found"))
        return redirect(url_for("admin.devices_page"))
    if action == "rename":
        raw_name = (request.form.get("custom_name") or "").strip()[:80]
        name = ""
        if raw_name:
            norm = normalise_print_name(raw_name)
            if not norm:
                _audit("device.rename", device_id, "FAILED", error="err.print_name_invalid")
                flash(("error", "err.print_name_invalid"))
                return redirect(url_for("admin.devices_page"))
            name = norm
        svc.db.set_device_custom_name(device_id, name)
        if name:
            svc.db.execute("UPDATE jobs SET username = ? WHERE device_id = ?", (name, device_id))
        _audit("device.rename", device_id, custom_name=name)
        flash(("ok", "msg.saved"))
    elif action == "delete":
        current_admin_id = (
            request.cookies.get(DEVICE_COOKIE_NAME)
            or request.cookies.get("device_id")
            or getattr(g, "device_id", None)
            or ""
        ).strip()
        if device_id == current_admin_id:
            flash(("error", "err.cannot_disable_self"))
        else:
            svc.db.delete_device(device_id)
            _audit("device.delete", device_id)
            flash(("ok", "msg.saved"))
    else:
        flash(("error", "err.bad_request"))
    return redirect(url_for("admin.devices_page"))


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


@bp.get("/api/health")
@admin_required
def api_health():
    return ok(**_collect_health())


@bp.post("/api/job/<job_id>/reprint")
@admin_required
def api_reprint_job(job_id):
    svc = _svc()
    job = svc.jobs.get(job_id[:32])
    if not job:
        return fail("err.not_found", 404)
    orig_parts = svc.jobs.parts(job)
    if not orig_parts:
        return fail("err.not_found", 404)
    opts = svc.jobs.options(job)
    new_parts, copied = [], []
    try:
        for idx, p in enumerate(orig_parts):
            src_path, ptype, _ = svc.jobs.resolve_part_file(job, idx)
            if src_path is None or not src_path.exists():
                for name in copied:
                    (svc.cfg.jobs_dir / name).unlink(missing_ok=True)
                return fail("err.upload_expired", 404, message="Исходный файл истек (старше 7 дней) или удален.")
            suffix = ".pdf" if ptype == "pdf" else (src_path.suffix or ".bin")
            stored = secrets.token_hex(12) + suffix
            shutil.copyfile(src_path, svc.cfg.jobs_dir / stored)
            copied.append(stored)
            doc_type = p.get("doc_type") or job.get("doc_type") or ("pdf" if ptype == "pdf" else "png")
            new_parts.append({
                **p,
                "doc_type": doc_type,
                "stored_name": stored,
                "upload_stored_name": p.get("upload_stored_name") or p.get("stored_name") or src_path.name,
            })
        new_job_id = svc.jobs.create(
            user_id=job.get("user_id"),
            username=job.get("username") or g.actor,
            owner_key=job.get("owner_key") or f"user:{g.user['id']}",
            device_id=job.get("device_id"),
            client_ip=job.get("client_ip") or _ip(),
            opts=opts,
            parts=new_parts,
        )
    except Exception:
        for name in copied:
            (svc.cfg.jobs_dir / name).unlink(missing_ok=True)
        log.exception("Failed to create reprint job for %s", job["id"])
        return fail("err.internal", 500)
    svc.manager.enqueue(new_job_id)
    new_job = svc.jobs.get(new_job_id)
    _audit("job.reprint", new_job_id, source_job_id=job["id"])
    return ok(
        job_id=new_job_id,
        source_job_id=job["id"],
        job=J.public_job(new_job, include_admin=True),
        message=__import__("lps.i18n", fromlist=["translate"]).translate("js.reprint_ok"),
    )


@bp.post("/api/job/<job_id>/cancel")
@admin_required
def api_cancel_job(job_id):
    import subprocess
    svc = _svc()
    job = svc.jobs.get(job_id[:32])
    if not job:
        return fail("err.not_found", 404)
    spooler_id = job.get("spooler_job_id")
    printer_name = svc.backend.printer_name or svc.cfg.get("printer_name") or "EPSON L1800 Series"
    svc.jobs.update(job["id"], cancel_requested=1)
    if spooler_id:
        if svc.backend.kind == "simulated":
            try:
                svc.backend.cancel_spooler_job(int(spooler_id))
            except Exception:
                pass
        else:
            try:
                import win32print
                for access in (win32print.PRINTER_ALL_ACCESS, win32print.PRINTER_ACCESS_USE):
                    try:
                        hprinter = win32print.OpenPrinter(printer_name, {"DesiredAccess": access})
                    except Exception:
                        continue
                    try:
                        win32print.SetJob(hprinter, int(spooler_id), 0, None, win32print.JOB_CONTROL_CANCEL)
                    except Exception:
                        pass
                    try:
                        win32print.SetJob(hprinter, int(spooler_id), 0, None, win32print.JOB_CONTROL_DELETE)
                    except Exception:
                        pass
                    finally:
                        win32print.ClosePrinter(hprinter)
                    break
            except Exception:
                pass
            try:
                safe_printer = str(printer_name).replace("'", "''")
                subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"Get-PrintJob -PrinterName '{safe_printer}' -ErrorAction SilentlyContinue | Where-Object Id -eq {int(spooler_id)} | Remove-PrintJob -Confirm:$false -ErrorAction SilentlyContinue",
                    ],
                    timeout=15,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception:
                log.exception("PowerShell Remove-PrintJob failed for job=%s spooler_id=%s", job["id"], spooler_id)
    svc.jobs.transition(job["id"], "CANCELED", detail=f"canceled by {g.actor}", cancel_requested=1)
    _audit("job.cancel", job["id"], spooler_job_id=spooler_id, status="CANCELED")
    return ok(job_id=job["id"], status="CANCELED", message="Задание успешно отменено.")


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


@bp.post("/api/printer/maintenance")
@admin_required
def api_printer_maintenance():
    import subprocess
    svc = _svc()
    body = json_body()
    maint_action = str(body.get("action") or "").strip().lower()
    if maint_action not in ("nozzle_check", "head_cleaning"):
        return fail("err.bad_request", 400, message="Invalid maintenance action.")
    printer_name = svc.backend.printer_name
    from ..config import load_config; live_cfg = load_config(); cmd_map = svc.cfg.get("maintenance_commands") or live_cfg.get("maintenance_commands") or {}
    cmd = (cmd_map.get(maint_action) if isinstance(cmd_map, dict) else None) or svc.cfg.get(f"{maint_action}_command") or live_cfg.get(f"{maint_action}_command") or ""
    try:
        log.info("Executing printer maintenance action='%s' on printer='%s' by actor='%s'",
                 maint_action, printer_name, g.actor)
        if cmd:
            res = subprocess.run(
                cmd if not (svc.backend.kind == "simulated" and isinstance(cmd, str) and "epson_maint.ps1" in cmd) else f"{cmd} -DryRun",
                shell=isinstance(cmd, str),
                timeout=60,
                capture_output=True,
                text=True,
                check=True,
            )
            output = (res.stdout or "").strip()[:300]
        elif svc.backend.kind != "simulated":
            safe_name = printer_name.replace("'", "''")
            res = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Get-CimInstance -ClassName Win32_Printer -Filter \"Name='{safe_name}'\" | Select-Object -ExpandProperty Name",
                ],
                timeout=30,
                capture_output=True,
                text=True,
                check=False,
            )
            output = (res.stdout or "").strip()[:300]
        else:
            output = "simulated"
        _audit(f"printer.{maint_action}", printer_name, output=output or "ok")
        msg = (
            "Проверка дюз запущена."
            if maint_action == "nozzle_check"
            else "Прочистка печатающей головки запущена."
        )
        return ok(action=maint_action, message=msg)
    except Exception as e:
        log.exception("Printer maintenance '%s' failed on '%s'", maint_action, printer_name)
        _audit(f"printer.{maint_action}", printer_name, "FAILED", error=str(e)[:200])
        return fail("err.internal", 500, message=f"Ошибка выполнения обслуживания: {e}")


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
                             device_id=getattr(g, "device_id", None),
                             client_ip=_ip(), filename="test-page.png",
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
