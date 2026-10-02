"""Users, roles and password handling.

Passwords are hashed with scrypt (werkzeug). Plaintext passwords are never
stored, logged or returned. Roles map to permission sets so new roles can be
added without touching the route code.
"""
from __future__ import annotations

import re
import threading

from werkzeug.security import check_password_hash, generate_password_hash

from .db import Database, utcnow

ROLES: dict[str, set[str]] = {
    "USER": {"print", "own_jobs"},
    "ADMIN": {"print", "own_jobs", "admin"},
}
ROLE_ORDER = ["USER", "ADMIN"]

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
PASSWORD_MIN, PASSWORD_MAX = 10, 256
_HASH_METHOD = "scrypt"
# Used to equalise timing when the user does not exist.
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing", method=_HASH_METHOD)


class UserError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def has_perm(user: dict | None, perm: str) -> bool:
    return bool(user) and user["is_active"] and perm in ROLES.get(user["role"], set())


def validate_password(password: str, username: str = "") -> None:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise UserError("err.password_length")
    if username and password.lower() == username.lower():
        raise UserError("err.password_is_username")
    if len(set(password)) < 4:
        raise UserError("err.password_weak")


def public_user(u: dict) -> dict:
    return {k: u[k] for k in ("id", "username", "display_name", "role", "is_active",
                              "must_change_password", "created_at", "last_login_at")}


class UserService:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()

    def get(self, user_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return dict(r) if r else None

    def by_username(self, username: str) -> dict | None:
        r = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        return dict(r) if r else None

    def list(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM users ORDER BY username")]

    def count_admins(self, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS n FROM users WHERE role = 'ADMIN'"
        if active_only:
            sql += " AND is_active = 1"
        return self.db.one(sql)["n"]

    def create(self, username: str, password: str, role: str, display_name: str = "",
               must_change: bool = False) -> int:
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise UserError("err.username_invalid")
        if role not in ROLES:
            raise UserError("err.role_invalid")
        validate_password(password, username)
        display_name = (display_name or "").strip()[:80]
        now = utcnow()
        with self._lock:
            if self.by_username(username):
                raise UserError("err.username_taken")
            cur = self.db.execute(
                "INSERT INTO users(username, display_name, password_hash, role, is_active,"
                " must_change_password, created_at, updated_at, password_changed_at)"
                " VALUES (?,?,?,?,1,?,?,?,?)",
                (username, display_name, generate_password_hash(password, method=_HASH_METHOD),
                 role, int(must_change), now, now, now))
        return cur.lastrowid

    def authenticate(self, username: str, password: str) -> dict | None:
        u = self.by_username((username or "").strip()) if username else None
        if u is None:
            check_password_hash(_DUMMY_HASH, password or "")
            return None
        if not check_password_hash(u["password_hash"], password or ""):
            return None
        if not u["is_active"]:
            return None
        self.db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), u["id"]))
        return u

    def set_password(self, user_id: int, password: str, must_change: bool) -> None:
        u = self.get(user_id)
        if not u:
            raise UserError("err.not_found")
        validate_password(password, u["username"])
        now = utcnow()
        # password_changed_at also invalidates all existing sessions of this user.
        self.db.execute("UPDATE users SET password_hash = ?, must_change_password = ?,"
                        " password_changed_at = ?, updated_at = ? WHERE id = ?",
                        (generate_password_hash(password, method=_HASH_METHOD), int(must_change),
                         now, now, user_id))

    def change_own_password(self, user_id: int, current: str, new: str) -> None:
        u = self.get(user_id)
        if not u or not check_password_hash(u["password_hash"], current or ""):
            raise UserError("err.current_password")
        if current == new:
            raise UserError("err.password_same")
        self.set_password(user_id, new, must_change=False)

    def set_active(self, user_id: int, active: bool) -> None:
        u = self.get(user_id)
        if not u:
            raise UserError("err.not_found")
        if not active and u["role"] == "ADMIN" and u["is_active"] and self.count_admins() <= 1:
            raise UserError("err.last_admin")
        now = utcnow()
        # Changing password_changed_at also kills the user's live sessions.
        self.db.execute("UPDATE users SET is_active = ?, updated_at = ?, password_changed_at ="
                        " CASE WHEN ? = 0 THEN ? ELSE password_changed_at END WHERE id = ?",
                        (int(active), now, int(active), now, user_id))

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise UserError("err.role_invalid")
        u = self.get(user_id)
        if not u:
            raise UserError("err.not_found")
        if u["role"] == "ADMIN" and role != "ADMIN" and u["is_active"] and self.count_admins() <= 1:
            raise UserError("err.last_admin")
        self.db.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                        (role, utcnow(), user_id))

    def update_display_name(self, user_id: int, display_name: str) -> None:
        self.db.execute("UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
                        ((display_name or "").strip()[:80], utcnow(), user_id))
