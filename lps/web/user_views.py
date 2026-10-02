"""User panel pages: print, history, account."""
from __future__ import annotations

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .. import jobs as J
from ..auth import UserError
from ..security import login_required, print_access_required
from .common import date_filters, page_args

bp = Blueprint("user", __name__)


def _svc():
    return current_app.extensions["lps"]


@bp.route("/")
@print_access_required
def index():
    return render_template("print.html")


@bp.route("/history")
@print_access_required
def history():
    svc = _svc()
    page, per = page_args()
    status = request.args.get("status") or None
    if status and status not in J.ALL_STATES and status != "ACTIVE":
        status = None
    df, dt = date_filters()
    rows, total = svc.jobs.search(owner_key=g.owner_key, status=status,
                                  text=(request.args.get("q") or "").strip()[:100] or None,
                                  job_id=(request.args.get("job") or "").strip()[:32] or None,
                                  date_from=df, date_to=dt, limit=per, offset=(page - 1) * per)
    rows = [dict(r, parts=svc.jobs.parts(r)) for r in rows]
    return render_template("history.html", jobs=rows, total=total, page=page, per=per,
                           states=J.ALL_STATES, active_states=J.ACTIVE, args=request.args)


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    svc = _svc()
    msg = err = None
    if request.method == "POST":
        new, new2 = request.form.get("new_password") or "", request.form.get("new_password2") or ""
        if new != new2:
            err = "err.password_mismatch"
        else:
            try:
                svc.users.change_own_password(g.user["id"], request.form.get("current_password"), new)
                svc.audit.record(g.actor, "user.change_own_password", g.actor, "OK",
                                 ip=request.remote_addr)
                # Session is invalidated by the password change; log in again.
                from ..security import login_user
                login_user(svc.users.get(g.user["id"]))
                g.user = svc.users.get(g.user["id"])
                msg = "msg.password_changed"
            except UserError as e:
                err = e.code
                svc.audit.record(g.actor, "user.change_own_password", g.actor, "FAILED",
                                 details={"error": e.code}, ip=request.remote_addr)
        if msg and request.args.get("force"):
            return redirect(url_for("user.index"))
    return render_template("account.html", msg=msg, err=err,
                           force=bool(request.args.get("force")) or g.user["must_change_password"])
