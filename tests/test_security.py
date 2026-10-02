"""Authentication, authorization, CSRF, rate limiting, session invalidation, audit."""
import re

import pytest

from .conftest import ADMIN_PW, USER_PW, Client, csrf_of, make_png, upload


def test_anonymous_redirected_and_api_401(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/admin/").headers["Location"].startswith("/login")
    r = c.get("/api/jobs")
    assert r.status_code == 401 and r.get_json()["error"] == "err.login_required"


def test_login_bad_password_generic_message(app):
    c = Client(app)
    r = c.login("alice", "wrong-password-xx")
    assert r.status_code == 401
    assert "Wrong username or password" in r.get_data(as_text=True)
    r2 = c.login("nobody", "whatever-password")
    assert r2.status_code == 401        # same response for unknown user


def test_login_rate_limit(app, svc):
    c = Client(app)
    for _ in range(5):
        c.login("alice", "bad-password-000")
    r = c.login("alice", USER_PW)        # correct password but locked out
    assert r.status_code == 401
    assert "Too many failed attempts" in r.get_data(as_text=True)
    rows, _ = svc.audit.search(action="auth.login", result="RATE_LIMITED")
    assert rows


def test_csrf_required(app, alice):
    r = alice.c.post("/api/uploads", data={}, headers={})            # no token
    assert r.status_code == 400 and r.get_json()["error"] == "err.csrf"
    r = alice.c.post("/logout", data={"csrf_token": "forged"})
    assert r.status_code == 400
    r = alice.c.post("/login", data={"username": "alice", "password": USER_PW})
    assert r.status_code == 400


def test_user_cannot_access_admin(alice, svc):
    assert alice.get("/admin/").status_code == 403
    r = alice.post("/admin/api/spooler/restart")
    assert r.status_code == 403 and r.get_json()["error"] == "err.forbidden"
    assert alice.post("/admin/api/jobs/cancel-all").status_code == 403
    rows, _ = svc.audit.search(action="authz.denied")
    assert rows and rows[0]["actor"] == "alice"


def test_admin_pages_render(admin):
    for path in ["/admin/", "/admin/jobs", "/admin/printer", "/admin/users", "/admin/settings",
                 "/admin/logs", "/admin/audit", "/admin/api/queue", "/admin/api/status"]:
        assert admin.get(path).status_code == 200, path


def test_setup_only_local_and_only_once(app, tmp_path):
    c = app.test_client()
    # an admin already exists -> redirect
    assert c.get("/setup").status_code == 302
    svc = app.extensions["lps"]
    svc.db.execute("DELETE FROM users")
    r = c.get("/setup", environ_base={"REMOTE_ADDR": "192.168.20.50"})
    assert r.status_code == 403
    tok = csrf_of(c)
    r = c.post("/setup", data={"username": "boss", "password": "Very-Secret-99", "password2":
                               "Very-Secret-99", "csrf_token": tok},
               environ_base={"REMOTE_ADDR": "192.168.20.50"})
    assert r.status_code == 403 and svc.users.count_admins() == 0
    tok = csrf_of(c)
    r = c.post("/setup", data={"username": "boss", "password": "Very-Secret-99", "password2":
                               "Very-Secret-99", "csrf_token": tok})
    assert r.status_code == 302 and svc.users.count_admins() == 1


def test_disable_user_kills_session(app, admin, alice, svc):
    assert alice.get("/history").status_code == 200
    uid = svc.users.by_username("alice")["id"]
    r = admin.post(f"/admin/users/{uid}/disable", data={"csrf_token": admin.token})
    assert r.status_code == 302
    assert alice.get("/history").status_code == 302      # back to login
    assert Client(app).login("alice", USER_PW).status_code == 401


def test_admin_password_reset_forces_change_and_invalidates(app, admin, bob, svc):
    uid = svc.users.by_username("bob")["id"]
    admin.post(f"/admin/users/{uid}/password",
               data={"csrf_token": admin.token, "password": "Brand-New-Pass-1",
                     "password2": "Brand-New-Pass-1", "must_change": "on"})
    assert bob.get("/history").status_code == 302         # old session invalid
    c = Client(app)
    r = c.login("bob", "Brand-New-Pass-1")
    assert "/account" in r.headers["Location"]
    assert c.get("/").headers["Location"].startswith("/account")   # forced to change first


def test_last_admin_protected(admin, svc):
    uid = svc.users.by_username("admin")["id"]
    admin.post(f"/admin/users/{uid}/disable", data={"csrf_token": admin.token})
    assert svc.users.get(uid)["is_active"] == 1
    page = admin.get("/admin/users").get_data(as_text=True)
    assert "cannot disable your own account" in page          # flash message shown
    admin.post(f"/admin/users/{uid}/role", data={"csrf_token": admin.token, "role": "USER"})
    assert svc.users.get(uid)["role"] == "ADMIN"
    assert "cannot remove your own administrator role" in admin.get("/admin/users").get_data(as_text=True)


def test_passwords_hashed_and_never_logged(app, admin, svc):
    admin.post("/admin/users/create", data={"csrf_token": admin.token, "username": "carol",
                                            "password": "Carol-Pass-777", "password2": "Carol-Pass-777",
                                            "role": "USER"})
    u = svc.users.by_username("carol")
    assert u and u["password_hash"].startswith("scrypt:") and "Carol-Pass-777" not in u["password_hash"]
    rows, _ = svc.audit.search(limit=1000)
    assert not any("Carol-Pass-777" in (r["details"] or "") for r in rows)
    log_text = svc.log_file.read_text(encoding="utf-8")
    assert "Carol-Pass-777" not in log_text and ADMIN_PW not in log_text


def test_security_headers(alice):
    r = alice.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_session_cookie_flags(app):
    c = app.test_client()
    r = c.get("/login")
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_errors_do_not_leak_tracebacks(app, alice, monkeypatch):
    svc = app.extensions["lps"]
    monkeypatch.setattr(svc.jobs, "search", lambda **kw: 1 / 0)
    r = alice.get("/api/jobs")
    assert r.status_code == 500
    body = r.get_data(as_text=True)
    assert "ZeroDivisionError" not in body and "Traceback" not in body
    r = alice.get("/history")
    assert r.status_code == 500 and "Traceback" not in r.get_data(as_text=True)


def test_guest_mode_isolation(app, svc):
    svc.settings.update({"user_login_mode": "GUEST"}, "test")
    g1, g2 = Client(app), Client(app)
    r = upload(g1, make_png(), "a.png")
    assert r.status_code == 200
    up = r.get_json()["upload"]["id"]
    r = g2.post("/api/preview", json={"upload_id": up, "options": {}})
    assert r.status_code == 404            # other browser cannot use it
    assert g1.get("/admin/").status_code == 302      # admin still needs login


def test_upload_size_limit(app, alice, svc):
    svc.settings.update({"max_upload_mb": 1}, "test")
    r = upload(alice, b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024), "big.pdf")
    assert r.status_code == 413 and r.get_json()["error"] == "err.too_large"
    assert not list(svc.cfg.uploads_dir.iterdir())     # nothing left behind


@pytest.mark.parametrize("name", ["..\\..\\..\\Windows\\win.ini.png", "../../../etc/win.ini.png",
                                  "C:\\Users\\me\\win.ini.png"])
def test_path_traversal_filename_is_harmless(alice, svc, name):
    # Werkzeug >= 3.1.4 drops backslashes in multipart filenames, older versions pass
    # them through. Either way the result must be a harmless display name, and the file
    # must be stored under a random name inside uploads/.
    r = upload(alice, make_png(), name)
    assert r.status_code == 200
    shown = r.get_json()["upload"]["filename"]
    assert shown.endswith("win.ini.png")
    assert not any(c in shown for c in ("/", "\\", ":")) and not shown.startswith(".")
    stored = list(svc.cfg.uploads_dir.iterdir())
    assert len(stored) == 1 and re.fullmatch(r"[0-9a-f]{24}\.bin", stored[0].name)
    assert stored[0].resolve().parent == svc.cfg.uploads_dir.resolve()


def test_must_change_password_also_blocks_api(app, admin, svc):
    uid = svc.users.by_username("bob")["id"]
    admin.post(f"/admin/users/{uid}/password",
               data={"csrf_token": admin.token, "password": "Temp-Password-99",
                     "password2": "Temp-Password-99", "must_change": "on"})
    c = Client(app)
    c.login("bob", "Temp-Password-99")
    r = upload(c, make_png(), "a.png")
    assert r.status_code == 403 and r.get_json()["error"] == "err.password_change_required"
    assert c.get("/api/jobs").status_code == 403
    assert c.get("/account").status_code == 200          # the change-password page still works


def test_config_json_with_bom_is_accepted(tmp_path):
    """install.ps1 (Windows PowerShell 5.1 Set-Content -Encoding UTF8) writes a BOM."""
    from lps.config import load_config
    p = tmp_path / "config.json"
    p.write_bytes(b"\xef\xbb\xbf" + b'{"port": 5001, "data_dir": "' + str(tmp_path / "d").replace("\\", "/").encode() + b'"}')
    assert load_config(p)["port"] == 5001


def _settings_form(svc, **changes):
    from lps.settings_service import SPEC
    form = {}
    for k, (default, _v, _g) in SPEC.items():
        v = svc.settings.get(k)
        if k == "queue_paused":
            continue
        if isinstance(default, bool):
            if v:
                form[k] = "on"
        elif k == "allowed_types":
            form[k] = v
        else:
            form[k] = str(v)
    form.update(changes)
    return {k: v for k, v in form.items() if v is not None}


def test_admin_settings_validation_via_web_form(admin, svc):
    before = svc.settings.all()
    r = admin.post("/admin/settings", data={"csrf_token": admin.token,
                                            **_settings_form(svc, max_copies="5000",
                                                             default_paper="B3")})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Some settings are not valid" in body
    assert svc.settings.all() == before                     # nothing saved on error

    r = admin.post("/admin/settings", data={"csrf_token": admin.token,
                                            **_settings_form(svc, max_copies="7",
                                                             user_login_mode="GUEST")})
    assert r.status_code == 302
    assert svc.settings.get("max_copies") == 7 and svc.settings.get("user_login_mode") == "GUEST"
    rows, _ = svc.audit.search(action="settings.update", result="OK")
    assert rows and "max_copies" in rows[0]["target"]


def test_user_cannot_change_settings(alice, svc):
    r = alice.post("/admin/settings", data={"csrf_token": alice.token, "max_copies": "999"})
    assert r.status_code == 403 and svc.settings.get("max_copies") == 50
