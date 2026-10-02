"""LocalPrintServer — LAN print management for the EPSON L1800."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import i18n
from .audit import AuditLog
from .auth import ROLES, UserService, has_perm
from .config import Config, load_config
from .db import Database
from .jobs import JobStore
from .logging_setup import set_level, setup_logging
from .office import OfficeConverter
from .printer import create_backend
from .printer.base import PrinterBackend
from .security import (LoginRateLimiter, check_csrf, csrf_token, load_identity,
                       security_headers)
from .settings_service import Settings
from .status import StatusService
from .worker import PrintManager

__version__ = "1.1.0"
log = logging.getLogger("lps")


@dataclass
class Services:
    cfg: Config
    db: Database
    settings: Settings
    users: UserService
    audit: AuditLog
    jobs: JobStore
    backend: PrinterBackend
    manager: PrintManager
    status: StatusService
    limiter: LoginRateLimiter
    log_file: object
    office: OfficeConverter


def _secret_key(cfg: Config) -> bytes:
    """Random per-installation key, stored in the data dir (never in source)."""
    path = cfg.data_dir / "secret_key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(48))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_bytes()


def create_app(cfg: Config | None = None, *, start_background: bool = True,
               backend: PrinterBackend | None = None) -> Flask:
    cfg = cfg or load_config()
    log_file = setup_logging(cfg.log_dir, cfg["log_level"])
    base = os.path.dirname(__file__)
    app = Flask(__name__, template_folder=os.path.join(base, "templates"),
                static_folder=os.path.join(base, "static"))
    app.secret_key = _secret_key(cfg)
    app.config.update(
        SESSION_COOKIE_NAME="lps_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(cfg["session_cookie_secure"]),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=cfg["session_hours"]),
        MAX_CONTENT_LENGTH=cfg["hard_max_upload_mb"] * 1024 * 1024,
        TEMPLATES_AUTO_RELOAD=False,
        JSON_AS_ASCII=False,
    )
    i18n.load()

    db = Database(cfg.db_path)
    settings = Settings(db)
    set_level(settings.get("log_level"))
    settings.on_change(lambda ch: set_level(ch["log_level"][1]) if "log_level" in ch else None)
    audit = AuditLog(db)
    store = JobStore(db)
    backend = backend or create_backend(cfg)
    manager = PrintManager(cfg, settings, store, backend, audit)
    svc = Services(cfg=cfg, db=db, settings=settings, users=UserService(db), audit=audit,
                   jobs=store, backend=backend, manager=manager, status=StatusService(manager),
                   limiter=LoginRateLimiter(), log_file=log_file, office=OfficeConverter(cfg))
    app.extensions["lps"] = svc

    from .web.admin_views import bp as admin_bp
    from .web.api import bp as api_bp
    from .web.auth_views import bp as auth_bp
    from .web.user_views import bp as user_bp
    for bp in (auth_bp, user_bp, api_bp, admin_bp):
        app.register_blueprint(bp)

    @app.before_request
    def _before():
        i18n.select_language()
        load_identity()
        check_csrf()

    app.after_request(security_headers)

    @app.context_processor
    def _ctx():
        return {"t": i18n.translate, "lang": g.get("lang", "en"), "languages": i18n.LANGUAGES,
                "csrf_token": csrf_token, "user": g.get("user"), "actor": g.get("actor"),
                "is_admin": has_perm(g.get("user"), "admin"), "roles": list(ROLES),
                "can_print": g.get("owner_key") is not None, "print_name": g.get("print_name"),
                "login_mode": settings.get("user_login_mode"), "version": __version__,
                "backend_kind": backend.kind,
                "js_translations": lambda: i18n.table(g.get("lang", "en"))}

    @app.errorhandler(HTTPException)
    def _http_error(e: HTTPException):
        key = {400: "err.bad_request", 403: "err.forbidden", 404: "err.not_found",
               405: "err.bad_request", 413: "err.too_large", 429: "err.rate_limited"
               }.get(e.code, "err.internal")
        if e.code == 400 and e.description == "csrf":
            key = "err.csrf"
        if request.path.startswith("/api/") or request.path.startswith("/admin/api/"):
            return jsonify(ok=False, error=key), e.code
        return render_template("error.html", code=e.code, message_key=key), e.code

    @app.errorhandler(Exception)
    def _unhandled(e: Exception):
        # Full traceback goes to the log only; users get a generic message.
        log.exception("Unhandled error on %s %s", request.method, request.path)
        if request.path.startswith("/api/") or request.path.startswith("/admin/api/"):
            return jsonify(ok=False, error="err.internal"), 500
        return render_template("error.html", code=500, message_key="err.internal"), 500

    if start_background:
        manager.start()
    log.info("LocalPrintServer %s initialised (backend=%s, printer=%r, data=%s)", __version__,
             backend.kind, backend.printer_name, cfg.data_dir)
    return app
