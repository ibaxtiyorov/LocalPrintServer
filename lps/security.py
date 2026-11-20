"""Session identity, device persistence, CSRF protection, login rate limiting, authorization, headers."""
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
DEVICE_COOKIE_NAME = "lps_device"
DEVICE_COOKIE_MAX_AGE = 10 * 365 * 86400  # 10 years
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# ---------------------------------------------------------------- Client IP
def normalize_ip(raw: str | None) -> str:
    ip = (raw or "").strip()
    if ip.lower().startswith("::ffff:"):
        ip = ip[7:]
    return ip[:64]


def resolve_client_ip() -> str:
    """Resolve client IP from X-Forwarded-For / X-Real-IP (Kerio gateway) or remote_addr."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return normalize_ip(first)
    xri = request.headers.get("X-Real-IP")
    if xri and xri.strip():
        return normalize_ip(xri)
    return normalize_ip(request.remote_addr or "-")


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
                                   request.path, resolve_client_ip())
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
    """Populate g.user / g.device_id / g.client_ip / g.owner_key / g.actor for the current request."""
    svc = current_app.extensions["lps"]
    g.client_ip = resolve_client_ip()
    ua = request.headers.get("User-Agent", "")[:255]

    raw_dev = (request.cookies.get(DEVICE_COOKIE_NAME) or session.get("device_id") or "").strip()
    if DEVICE_ID_RE.match(raw_dev):
        g.device_id = raw_dev
        g.set_device_cookie = request.cookies.get(DEVICE_COOKIE_NAME) != raw_dev
    else:
        g.device_id = secrets.token_hex(12)
        g.set_device_cookie = True
    session["device_id"] = g.device_id

    # Update device record & IP history on state-changing requests, page loads, or unseen devices/IPs.
    dev = svc.db.get_device(g.device_id)
    if dev is None or dev.get("last_ip") != g.client_ip or not request.path.startswith("/api/status"):
        dev = svc.db.touch_device(g.device_id, ip=g.client_ip, user_agent=ua)
    g.device = dev

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
    elif mode == "USERNAME":
        # Name-only printing identity: auto-restore from session or persistent device record
        # unless user explicitly logged out on this session.
        pname = session.get("print_name")
        if not pname and not session.get("name_logged_out") and dev:
            pname = dev.get("last_known_name") or dev.get("custom_name")
            if pname:
                session["print_name"] = pname
        if pname:
            g.print_name = pname
            g.owner_key = "name:" + g.print_name.casefold()
            g.actor = g.print_name
        else:
            g.owner_key = None
            g.actor = "anonymous"
    elif mode == "GUEST":
        gid = session.get("guest_id")
        if not gid:
            gid = g.device_id[:8] if g.device_id else secrets.token_hex(4)
            session["guest_id"] = gid
        g.owner_key = f"guest:{gid}"
        g.actor = (dev.get("custom_name") if dev and dev.get("custom_name") else f"guest-{gid}")
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
    """Set or change the display name without clearing session/device identity."""
    svc = current_app.extensions["lps"]
    old_name = session.get("print_name")
    session.permanent = True
    session.pop("name_logged_out", None)
    session["print_name"] = name
    csrf_token()
    dev_id = getattr(g, "device_id", None) or session.get("device_id")
    if dev_id:
        svc.db.touch_device(dev_id, ip=getattr(g, "client_ip", resolve_client_ip()),
                            user_agent=request.headers.get("User-Agent", "")[:255],
                            last_known_name=name)
        new_owner = "name:" + name.casefold()
        if old_name and old_name.casefold() != name.casefold():
            old_owner = "name:" + old_name.casefold()
            svc.db.execute("UPDATE uploads SET owner_key = ? WHERE device_id = ? OR owner_key = ?",
                           (new_owner, dev_id, old_owner))
            svc.db.execute("UPDATE presets SET owner_key = ? WHERE owner_key = ?",
                           (new_owner, old_owner))


def login_user(user: dict):
    dev_id = getattr(g, "device_id", None) or session.get("device_id")
    lang = session.get("lang")
    session.clear()
    if lang:
        session["lang"] = lang
    if dev_id:
        session["device_id"] = dev_id
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
                g.actor, "authz.denied", request.path, "DENIED", ip=getattr(g, "client_ip", resolve_client_ip()))
            return _deny(403)
        blocked = _password_change_block()
        if blocked is not None:
            return blocked
        return fn(*a, **kw)
    return wrapper


def is_local_request() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",

        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; frame-src 'self' blob:; object-src 'self' blob:; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
    if request.path.startswith("/api/") or request.path.startswith("/admin"):
        resp.headers.setdefault("Cache-Control", "no-store")
    if getattr(g, "set_device_cookie", False) and getattr(g, "device_id", None):
        resp.set_cookie(
            DEVICE_COOKIE_NAME,
            g.device_id,
            max_age=DEVICE_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=bool(current_app.config.get("SESSION_COOKIE_SECURE", False)),
        )
    return resp
