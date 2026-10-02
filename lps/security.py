"""Session identity, CSRF protection, login rate limiting, authorization, headers."""
from __future__ import annotations

import functools
import hmac
import re
import secrets
import threading
import time
import unicodedata
from collections import defaultdict, deque

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for

from .auth import has_perm

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ---------------------------------------------------------------- CSRF
def csrf_token() -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


def check_csrf():
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        current_app.logger.warning("CSRF check failed for %s %s from %s", request.method,
                                   request.path, request.remote_addr)
        abort(400, description="csrf")


# ---------------------------------------------------------------- rate limiting
class LoginRateLimiter:
    """Sliding-window limiter for failed logins, per username and per client IP."""

    def __init__(self, per_user: int = 5, per_ip: int = 20, window: int = 900):
        self.per_user, self.per_ip, self.window = per_user, per_ip, window
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, username: str, ip: str) -> int:
        """Seconds until another attempt is allowed (0 = allowed)."""
        now = time.monotonic()
        with self._lock:
            waits = []
            for key, limit in ((f"u:{username.lower()}", self.per_user), (f"ip:{ip}", self.per_ip)):
                q = self._prune(key, now)
                if len(q) >= limit:
                    waits.append(int(self.window - (now - q[0])) + 1)
            return max(waits, default=0)

    def failure(self, username: str, ip: str):
        now = time.monotonic()
        with self._lock:
            self._fails[f"u:{username.lower()}"].append(now)
            self._fails[f"ip:{ip}"].append(now)

    def success(self, username: str):
        with self._lock:
            self._fails.pop(f"u:{username.lower()}", None)


# ---------------------------------------------------------------- identity
def load_identity():
    """Populate g.user / g.owner_key / g.actor for the current request."""
    svc = current_app.extensions["lps"]
    g.user = None
    uid = session.get("uid")
    if uid:
        u = svc.users.get(uid)
        # Disabled users and changed passwords invalidate existing sessions.
        if u and u["is_active"] and session.get("pwv") == u["password_changed_at"]:
            g.user = u
        else:
            session.pop("uid", None)
            session.pop("pwv", None)
    g.print_name = None
    mode = svc.settings.get("user_login_mode")
    if g.user:
        g.owner_key = f"user:{g.user['id']}"
        g.actor = g.user["username"]
    elif mode == "USERNAME" and session.get("print_name"):
        # Name-only printing identity: never an account, never admin rights.
        g.print_name = session["print_name"]
        g.owner_key = "name:" + g.print_name.casefold()
        g.actor = g.print_name
    elif mode == "GUEST":
        gid = session.get("guest_id")
        if not gid:
            gid = secrets.token_hex(4)
            session["guest_id"] = gid
        g.owner_key = f"guest:{gid}"
        g.actor = f"guest-{gid}"
    else:
        g.owner_key = None
        g.actor = "anonymous"


# Letters of any script, digits, space . - _ and apostrophes (Uzbek O'tkir / Oʻtkir, O'Brien).
NAME_RE = re.compile(r"^[^\W_][\w .'’ʼ\-]{1,39}$")


def normalise_print_name(raw: str | None) -> str | None:
    """Username for name-only mode: 2-40 letters/digits/space/.-_ (any script)."""
    name = unicodedata.normalize("NFC", " ".join(str(raw or "").split()))
    return name if NAME_RE.match(name) else None


def login_print_name(name: str):
    lang = session.get("lang")
    session.clear()
    if lang:
        session["lang"] = lang
    session.permanent = True
    session["print_name"] = name
    csrf_token()


def login_user(user: dict):
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["pwv"] = user["password_changed_at"]
    csrf_token()   # rotate CSRF token with the new session


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.path.startswith("/admin/api/")


def _deny(status: int):
    if _wants_json():
        key = "err.login_required" if status == 401 else "err.forbidden"
        return jsonify(ok=False, error=key), status
    if status == 401:
        return redirect(url_for("auth.login", next=request.full_path))
    abort(403)


def _password_change_block():
    """A user whose password was reset must change it before doing anything else,
    through the browser pages AND through the JSON API."""
    if g.user and g.user["must_change_password"]:
        if _wants_json():
            return jsonify(ok=False, error="err.password_change_required"), 403
        return redirect(url_for("user.account", force=1))
    return None


def may_print() -> bool:
    if g.user:
        return has_perm(g.user, "print")
    return g.owner_key is not None


def print_access_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not may_print():
            return _deny(401 if not g.user else 403)
        blocked = _password_change_block()
        if blocked is not None:
            return blocked
        return fn(*a, **kw)
    return wrapper


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return _deny(401)
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return _deny(401)
        if not has_perm(g.user, "admin"):
            current_app.extensions["lps"].audit.record(
                g.actor, "authz.denied", request.path, "DENIED", ip=request.remote_addr)
            return _deny(403)
        blocked = _password_change_block()
        if blocked is not None:
            return blocked
        return fn(*a, **kw)
    return wrapper


def is_local_request() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    if request.path.startswith("/api/") or request.path.startswith("/admin"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp
