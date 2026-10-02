"""Login, logout and first-run administrator setup."""
from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for

from ..auth import UserError
from ..security import is_local_request, login_print_name, login_user, normalise_print_name

bp = Blueprint("auth", __name__)


def _svc():
    return current_app.extensions["lps"]


def _safe_next(target: str | None) -> str:
    if not target:
        return url_for("user.index")
    p = urlparse(target)
    # Browsers treat "\" like "/", so "/\evil.com" would become "//evil.com".
    if (p.scheme or p.netloc or not target.startswith("/") or target.startswith("//")
            or "\\" in target or any(ord(ch) < 32 for ch in target)):
        return url_for("user.index")
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    svc = _svc()
    if svc.users.count_admins(active_only=False) == 0:
        return redirect(url_for("auth.setup"))
    error, username = None, ""
    mode = svc.settings.get("user_login_mode")
    name_error, print_name = None, ""
    if request.method == "POST" and request.form.get("login_type") == "name":
        # Username-only printing identity (no password). Never grants admin rights.
        print_name = (request.form.get("print_name") or "")[:80]
        name = normalise_print_name(print_name)
        if mode != "USERNAME":
            abort(400)
        if not name:
            name_error = "err.print_name_invalid"
        else:
            login_print_name(name)
            svc.audit.record(name, "auth.name_login", name, "OK", ip=request.remote_addr)
            nxt = _safe_next(request.args.get("next"))
            return redirect(nxt if not nxt.startswith("/admin") else url_for("user.index"))
        return render_template("login.html", error=None, username="", mode=mode,
                               name_error=name_error, print_name=print_name), 400
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:64]
        password = request.form.get("password") or ""
        ip = request.remote_addr or "-"
        wait = svc.limiter.retry_after(username, ip)
        if wait:
            error = ("err.rate_limited", {"minutes": max(1, (wait + 59) // 60)})
            svc.audit.record(username or "-", "auth.login", username, "RATE_LIMITED", ip=ip)
        else:
            user = svc.users.authenticate(username, password)
            if user:
                svc.limiter.success(username)
                login_user(user)
                svc.audit.record(user["username"], "auth.login", user["username"], "OK", ip=ip)
                if user["must_change_password"]:
                    return redirect(url_for("user.account", force=1))
                return redirect(_safe_next(request.args.get("next")))
            svc.limiter.failure(username, ip)
            svc.audit.record(username or "-", "auth.login", username, "FAILED", ip=ip)
            error = ("err.login_failed", {})
    return render_template("login.html", error=error, username=username, mode=mode,
                           name_error=None, print_name=""), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    if g.user or g.get("print_name"):
        _svc().audit.record(g.actor, "auth.logout", g.actor, "OK", ip=request.remote_addr)
    lang = session.get("lang")
    session.clear()
    if lang:
        session["lang"] = lang
    return redirect(url_for("auth.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Creates the first administrator. Only while no admin exists AND only from the
    server itself (http://127.0.0.1:5000/setup), so LAN clients can never claim it."""
    svc = _svc()
    if svc.users.count_admins(active_only=False) > 0:
        return redirect(url_for("auth.login"))
    if not is_local_request():
        return render_template("setup_remote.html"), 403
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        p1, p2 = request.form.get("password") or "", request.form.get("password2") or ""
        if p1 != p2:
            error = "err.password_mismatch"
        else:
            try:
                uid = svc.users.create(username, p1, "ADMIN",
                                       request.form.get("display_name", ""))
                svc.audit.record(username, "setup.create_admin", username, "OK",
                                 ip=request.remote_addr)
                login_user(svc.users.get(uid))
                return redirect(url_for("admin.dashboard"))
            except UserError as e:
                error = e.code
    return render_template("setup.html", error=error)


@bp.route("/lang/<code>")
def set_lang(code):
    from ..i18n import LANGUAGES
    if code not in LANGUAGES:
        abort(404)
    session["lang"] = code
    return redirect(_safe_next(request.args.get("next")))
