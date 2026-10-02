"""Audit log of security-relevant and administrative actions.

Never pass passwords or secrets in `details`.
"""
from __future__ import annotations

import json
import logging

from .db import Database, utcnow

log = logging.getLogger("lps.audit")

_SECRET_KEYS = {"password", "password1", "password2", "new_password", "current_password",
                "csrf_token", "secret"}


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def record(self, actor: str, action: str, target: str | None = None, result: str = "OK",
               details: dict | str | None = None, ip: str | None = None):
        if isinstance(details, dict):
            details = json.dumps({k: v for k, v in details.items() if k not in _SECRET_KEYS},
                                 ensure_ascii=False, default=str)
        self.db.execute(
            "INSERT INTO audit_log(ts, actor, actor_ip, action, target, result, details)"
            " VALUES (?,?,?,?,?,?,?)",
            (utcnow(), actor or "-", ip, action, target, result, details))
        log.info("AUDIT actor=%s ip=%s action=%s target=%s result=%s", actor, ip, action,
                 target, result)

    def search(self, actor=None, action=None, result=None, date_from=None, date_to=None,
               text=None, limit=200, offset=0):
        where, params = [], []
        if actor:
            where.append("actor LIKE ?"); params.append(f"%{actor}%")
        if action:
            where.append("action = ?"); params.append(action)
        if result:
            where.append("result = ?"); params.append(result)
        if date_from:
            where.append("ts >= ?"); params.append(date_from)
        if date_to:
            where.append("ts < ?"); params.append(date_to)
        if text:
            where.append("(target LIKE ? OR details LIKE ?)"); params += [f"%{text}%"] * 2
        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        total = self.db.one(sql.replace("SELECT *", "SELECT COUNT(*) AS n"), params)["n"]
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset])
        return [dict(r) for r in rows], total

    def actions(self) -> list[str]:
        return [r["action"] for r in self.db.query(
            "SELECT DISTINCT action FROM audit_log ORDER BY action")]

    def purge_older_than(self, cutoff_iso: str) -> int:
        return self.db.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff_iso,)).rowcount
