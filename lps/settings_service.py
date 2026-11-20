"""Runtime application settings editable by administrators (stored in DB).

Only settings the application actually controls are listed here (final
requirements §18). Each has a validator; invalid values are rejected.
"""
from __future__ import annotations

import json
import threading

from . import catalog
from .db import Database, utcnow

FILE_TYPES = ["pdf", "jpeg", "png", "bmp", "gif", "tiff", "webp", "docx", "xlsx", "doc", "xls"]
LOGIN_MODES = ["PASSWORD", "USERNAME", "GUEST"]
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


def _choice(options):
    def v(x):
        if x not in options:
            raise ValueError(f"must be one of {options}")
        return x
    return v


def _int(lo, hi):
    def v(x):
        x = int(x)
        if not lo <= x <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return x
    return v


def _float(lo, hi):
    def v(x):
        x = round(float(x), 1)
        if not lo <= x <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return x
    return v


def _bool(x):
    if isinstance(x, bool):
        return x
    if str(x).lower() in ("1", "true", "on", "yes"):
        return True
    if str(x).lower() in ("0", "false", "off", "no", ""):
        return False
    raise ValueError("must be a boolean")


def _types(x):
    if isinstance(x, str):
        x = [s.strip() for s in x.split(",") if s.strip()]
    x = [s for s in x if s in FILE_TYPES]
    if not x:
        raise ValueError("at least one file type is required")
    return sorted(set(x), key=FILE_TYPES.index)


# key -> (default, validator, group)
SPEC: dict[str, tuple] = {
    "default_paper": ("A4", _choice([p.key for p in catalog.PAPER_SIZES]), "print"),
    "default_media": ("PLAIN", _choice([m.key for m in catalog.MEDIA_TYPES]), "print"),
    "default_quality": ("STANDARD", _choice(catalog.QUALITY_ORDER), "print"),
    "default_color": ("COLOR", _choice(list(catalog.COLOR)), "print"),
    "default_orientation": ("AUTO", _choice(catalog.ORIENTATION_CHOICES), "print"),
    "default_scaling": ("FIT", _choice(catalog.SCALING_CHOICES), "print"),
    "default_margins_mm": (0.0, _float(0, 50), "print"),
    "max_copies": (50, _int(1, catalog.DRIVER_MAX_COPIES), "print"),
    "nup_gap_mm": (4.0, _float(0, 20), "print"),
    "borderless_enabled": (False, _bool, "print"),
    "copies_mode": ("DRIVER", _choice(["DRIVER", "APPLICATION"]), "print"),
    "devmode_merge": (False, _bool, "print"),   # off = exactly the verified path (spec §4)
    "pdf_render_dpi": (300, _int(72, 600), "print"),
    "default_image_dpi": (96, _int(50, 1200), "print"),

    "max_upload_mb": (50, _int(1, 200), "uploads"),
    "allowed_types": (list(FILE_TYPES), _types, "uploads"),
    "max_documents_per_job": (10, _int(1, 50), "uploads"),
    "max_pdf_pages": (300, _int(1, 2000), "uploads"),
    "upload_ttl_minutes": (120, _int(10, 1440), "uploads"),

    # PASSWORD: account + password; USERNAME: name only (no password) for printing;
    # GUEST: anonymous. The admin panel ALWAYS requires an account with a password.
    "user_login_mode": ("PASSWORD", _choice(LOGIN_MODES), "access"),
    "max_active_jobs_per_user": (10, _int(1, 100), "access"),

    "stall_seconds": (90, _int(20, 3600), "queue"),
    "spooler_retry_minutes": (30, _int(1, 1440), "queue"),
    "max_inflight_spooler_jobs": (1, _int(1, 10), "queue"),
    "queue_paused": (False, _bool, "queue"),

    "job_retention_days": (90, _int(1, 3650), "retention"),
    "audit_retention_days": (365, _int(30, 3650), "retention"),
    "log_level": ("INFO", _choice(LOG_LEVELS), "retention"),
}


class Settings:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._listeners = []

    def on_change(self, fn):
        self._listeners.append(fn)

    def all(self) -> dict:
        with self._lock:
            if self._cache is None:
                values = {k: spec[0] for k, spec in SPEC.items()}
                rows = {r["key"]: r["value"] for r in self.db.query("SELECT key, value FROM settings")}
                # v1.0 stored a boolean "require_login"; false meant anonymous guest printing.
                if "user_login_mode" not in rows and rows.get("require_login") == "false":
                    values["user_login_mode"] = "GUEST"
                for row in self.db.query("SELECT key, value FROM settings"):
                    if row["key"] in SPEC:
                        try:
                            values[row["key"]] = SPEC[row["key"]][1](json.loads(row["value"]))
                        except (ValueError, TypeError):
                            pass
                self._cache = values
            return dict(self._cache)

    def get(self, key):
        return self.all()[key]

    def validate(self, updates: dict) -> tuple[dict, dict]:
        clean, errors = {}, {}
        for k, v in updates.items():
            if k not in SPEC:
                errors[k] = "unknown setting"
                continue
            try:
                clean[k] = SPEC[k][1](v)
            except (ValueError, TypeError) as e:
                errors[k] = str(e)
        return clean, errors

    def update(self, updates: dict, actor: str) -> tuple[dict, dict]:
        """Validate and persist. Returns (changed {key: (old, new)}, errors)."""
        clean, errors = self.validate(updates)
        if errors:
            return {}, errors
        current = self.all()
        changed = {k: (current[k], v) for k, v in clean.items() if current[k] != v}
        if changed:
            with self.db.transaction() as c:
                for k, (_, new) in changed.items():
                    c.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?)"
                              " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                              " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                              (k, json.dumps(new), utcnow(), actor))
            with self._lock:
                self._cache = None
            for fn in self._listeners:
                fn(changed)
        return changed, {}
