"""User panel pages: print, history, account."""
from __future__ import annotations

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .. import jobs as J
from ..auth import UserError, has_perm
from ..security import DEVICE_COOKIE_NAME, login_required, print_access_required, resolve_client_ip
from .common import date_filters, page_args

bp = Blueprint("user", __name__)


def _svc():
    return current_app.extensions["lps"]


@bp.route("/")
@print_access_required
def index():
    return render_template("print.html")


def _current_user_identity() -> tuple[int | None, str]:
    """Return (user_id, username) for a formally authenticated user, or (None, '') otherwise."""
    user = getattr(g, "user", None)
    if user:
        return user.get("id"), (user.get("username") or "").strip()
    return None, ""


def _job_matches_me(job: dict | None, device_id: str) -> bool:
    if not job:
        return False
    user_id, username = _current_user_identity()
    if device_id and job.get("device_id") == device_id:
        return True
    if user_id is not None and job.get("user_id") is not None and job.get("user_id") == user_id:
        return True
    job_user = (job.get("username") or job.get("user") or "").strip()
    if username and job_user and job_user.casefold() == username.casefold():
        return True
    return False


@bp.route("/history")
@print_access_required
def history():
    svc = _svc()
    page, per = page_args()
    status = request.args.get("status") or None
    if status and status not in J.ALL_STATES and status != "ACTIVE":
        status = None
    df, dt = date_filters()
    scope = request.args.get("scope", "me")
    device_id = (
        request.cookies.get(DEVICE_COOKIE_NAME)
        or request.cookies.get("device_id")
        or getattr(g, "device_id", None)
        or ""
    ).strip()
    if scope == "all" and has_perm(g.user, "admin"):
        rows, total = svc.jobs.search(status=status,
                                      text=(request.args.get("q") or "").strip()[:100] or None,
                                      job_id=(request.args.get("job") or "").strip()[:32] or None,
                                      date_from=df, date_to=dt, limit=per, offset=(page - 1) * per)
    else:
        user_id, username = _current_user_identity()
        if not device_id and user_id is None and not username:
            rows, total = [], 0
        else:
            all_rows, _ = svc.jobs.search(status=status,
                                          text=(request.args.get("q") or "").strip()[:100] or None,
                                          job_id=(request.args.get("job") or "").strip()[:32] or None,
                                          date_from=df, date_to=dt, limit=1000, offset=0)
            matched = [job for job in all_rows if _job_matches_me(job, device_id)]
            total = len(matched)
            start = (page - 1) * per
            rows = matched[start : start + per]
    rows = [dict(r, parts=svc.jobs.parts(r)) for r in rows]
    return render_template("history.html", jobs=rows, total=total, page=page, per=per,
                           states=J.ALL_STATES, active_states=J.ACTIVE, args=request.args)


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    svc = _svc()
    msg = err = None
    ip = getattr(g, "client_ip", None) or resolve_client_ip()
    if request.method == "POST":
        new, new2 = request.form.get("new_password") or "", request.form.get("new_password2") or ""
        if new != new2:
            err = "err.password_mismatch"
        else:
            try:
                svc.users.change_own_password(g.user["id"], request.form.get("current_password"), new)
                svc.audit.record(g.actor, "user.change_own_password", g.actor, "OK",
                                 ip=ip)
                # Session is invalidated by the password change; log in again.
                from ..security import login_user
                login_user(svc.users.get(g.user["id"]))
                g.user = svc.users.get(g.user["id"])
                msg = "msg.password_changed"
            except UserError as e:
                err = e.code
                svc.audit.record(g.actor, "user.change_own_password", g.actor, "FAILED",
                                 details={"error": e.code}, ip=ip)
        if msg and request.args.get("force"):
            return redirect(url_for("user.index"))
    return render_template("account.html", msg=msg, err=err,
                           force=bool(request.args.get("force")) or g.user["must_change_password"])
