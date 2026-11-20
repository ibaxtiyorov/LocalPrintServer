"""Username-only mode, guest migration, device persistence, and print presets."""
import json
import re

from lps import jobs as J

from .conftest import ADMIN_PW, Client, make_color_pdf, upload_ok, wait_status


def name_login(app, name, headers=None):
    c = Client(app)
    r = c.c.post("/login", data={"login_type": "name", "print_name": name, "csrf_token": c.token},
                 headers=headers or {})
    page = c.c.get("/", headers=headers or {}).get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', page)
    if m:
        c.token = m.group(1)
    return c, r


def test_username_only_printing(app, svc):
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")
    page = app.test_client().get("/login").get_data(as_text=True)
    assert 'name="print_name"' in page and 'name="password"' in page   # both forms available
    c, r = name_login(app, "  Oʻtkir   Karimov ")
    assert r.status_code == 302
    up = upload_ok(c, make_color_pdf(2), "a.pdf")
    jid = c.post("/api/jobs", json={"documents": [{"upload_id": up["id"]}]}).get_json()["job"]["id"]
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["username"] == "Oʻtkir Karimov" and job["owner_key"] == "name:oʻtkir karimov"
    hist = c.get("/history").get_data(as_text=True)
    assert "Oʻtkir Karimov" in hist and "name only" in hist
    rows, _ = svc.audit.search(action="auth.name_login")
    assert rows[0]["actor"] == "Oʻtkir Karimov"


def test_name_only_never_grants_admin(app, svc):
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")
    c, _ = name_login(app, "admin")                       # typing an admin's name...
    assert c.get("/admin/").status_code == 302             # ...still needs a password login
    assert c.post("/admin/api/spooler/restart").status_code == 401
    assert c.get("/account").status_code == 302


def test_password_login_still_works_in_username_mode(app, svc):
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")
    c = Client(app)
    assert c.login("admin", ADMIN_PW).status_code == 302
    assert c.get("/admin/").status_code == 200


def test_invalid_names_and_wrong_mode(app, svc):
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")
    for bad in ("", "a", "<script>", "x" * 41, "_name"):
        c, r = name_login(app, bad)
        assert r.status_code == 400
        assert c.get("/").status_code == 302                # still not signed in
    svc.settings.update({"user_login_mode": "PASSWORD"}, "test")
    c, r = name_login(app, "Valid Name")
    assert r.status_code == 400 and c.get("/").status_code == 302


def test_logout_clears_name(app, svc):
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")
    c, _ = name_login(app, "Bob")
    assert c.get("/").status_code == 200
    c.c.post("/logout", data={"csrf_token": c.token})
    assert c.get("/").status_code == 302


def test_v1_require_login_false_becomes_guest(svc):
    svc.db.execute("INSERT INTO settings(key, value, updated_at) VALUES ('require_login', 'false', 'x')")
    svc.settings._cache = None
    assert svc.settings.get("user_login_mode") == "GUEST"


# ------------------------------------------------------------------ presets
def test_presets_save_apply_delete(alice, svc):
    r = alice.post("/api/presets", json={"name": "Photos 10x15", "options": {
        "paper": "P10X15", "media": "PREMIUM_GLOSSY", "quality": "HIGH", "nup": 1, "copies": 2,
        "page_range": "1-3", "documents": [{"upload_id": "x"}]}})
    presets = r.get_json()["presets"]
    p = next(x for x in presets if x["name"] == "Photos 10x15")
    assert p["options"]["paper"] == "P10X15" and p["options"]["quality"] == "HIGH"
    assert "page_range" not in p["options"] and "documents" not in p["options"]
    assert p["mine"] and not p["shared"]
    # saving the same name again replaces it
    alice.post("/api/presets", json={"name": "Photos 10x15", "options": {"paper": "A4"}})
    mine = [x for x in alice.get("/api/presets").get_json()["presets"] if x["name"] == "Photos 10x15"]
    assert len(mine) == 1 and mine[0]["options"]["paper"] == "A4"
    assert alice.delete(f"/api/presets/{mine[0]['id']}").get_json()["ok"]


def test_presets_validated_and_private(alice, bob):
    r = alice.post("/api/presets", json={"name": "bad", "options": {"paper": "B3"}})
    assert r.status_code == 400
    assert alice.post("/api/presets", json={"name": " ", "options": {}}).status_code == 400
    alice.post("/api/presets", json={"name": "Mine", "options": {}})
    assert not [p for p in bob.get("/api/presets").get_json()["presets"] if p["name"] == "Mine"]
    pid = alice.get("/api/presets").get_json()["presets"][0]["id"]
    assert bob.delete(f"/api/presets/{pid}").status_code == 404


def test_shared_presets_admin_only(admin, alice, svc):
    assert alice.post("/api/presets", json={"name": "S", "shared": True, "options": {}}).status_code == 403
    admin.post("/api/presets", json={"name": "Office default", "shared": True,
                                     "options": {"quality": "DRAFT", "color": "MONO"}})
    shared = [p for p in alice.get("/api/presets").get_json()["presets"] if p["shared"]]
    assert shared and shared[0]["name"] == "Office default" and not shared[0]["mine"]
    assert alice.delete(f"/api/presets/{shared[0]['id']}").status_code == 404
    assert admin.delete(f"/api/presets/{shared[0]['id']}").get_json()["ok"]
    rows, _ = svc.audit.search(action="preset.share")
    assert rows


def test_preset_options_stored_as_json(alice, svc):
    alice.post("/api/presets", json={"name": "N9", "options": {"nup": 9, "scaling": "FIT"}})
    row = svc.db.one("SELECT options_json FROM presets WHERE name = 'N9'")
    assert json.loads(row["options_json"])["nup"] == 9


# ------------------------------------------------------------------ v1.1.0 device & repeat_on_sheet
def test_device_cookie_ip_history_and_change_name(app, svc):
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")
    first_r = app.test_client().get("/login")
    cookies = [v for k, v in first_r.headers if k.lower() == "set-cookie" and "lps_device=" in v]
    assert cookies and "315360000" in cookies[0] and "HttpOnly" in cookies[0]

    hdrs1 = {"X-Forwarded-For": "::ffff:192.168.10.55, 10.0.0.1"}
    c, r = name_login(app, "Azizbek", headers=hdrs1)
    assert r.status_code == 302

    up = upload_ok(c, make_color_pdf(2), "doc.pdf")
    c.post("/api/presets", json={"name": "MyRepeat", "options": {"nup": 4, "repeat_on_sheet": True}},
           headers=hdrs1)
    prev = c.post("/api/preview", json={
        "documents": [{"upload_id": up["id"]}],
        "options": {"nup": 4, "repeat_on_sheet": True}}).get_json()
    assert prev["meta"]["sheets"] == 2
    assert prev["meta"]["order"] == [[[1, 1], [1, 1], [1, 1], [1, 1]],
                                     [[1, 2], [1, 2], [1, 2], [1, 2]]]

    resp = c.post("/api/jobs", json={
        "documents": [{"upload_id": up["id"]}],
        "options": {"nup": 4, "repeat_on_sheet": True}},
        headers={"X-Real-IP": "192.168.10.99"}).get_json()
    jid = resp["job"]["id"]
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["repeat_on_sheet"] == 1
    assert job["nup"] == 4
    assert job["page_count"] == 2
    assert job["client_ip"] == "192.168.10.99"
    assert job["device_id"]

    dev = svc.db.get_device(job["device_id"])
    assert dev["last_known_name"] == "Azizbek"
    assert dev["custom_name"] == ""
    assert dev["last_ip"] == "192.168.10.99"
    assert "192.168.10.55" in dev["ip_history"] and "192.168.10.99" in dev["ip_history"]

    # Admin sets custom_name manually
    svc.db.set_device_custom_name(job["device_id"], "Lab-PC-Dedicated")

    # Change name without clearing session or losing history/uploads/presets
    # Frontend name change MUST NOT overwrite admin custom_name
    r_cn = c.c.post("/change-name", data={"print_name": "Azizbek Rahimov", "csrf_token": c.token})
    assert r_cn.status_code == 302
    dev_after = svc.db.get_device(job["device_id"])
    assert dev_after["custom_name"] == "Lab-PC-Dedicated"
    assert dev_after["last_known_name"] == "Azizbek Rahimov"
    jobs_list = c.get("/api/jobs").get_json()["jobs"]
    assert any(j["id"] == jid for j in jobs_list)
    presets = c.get("/api/presets").get_json()["presets"]
    assert any(p["name"] == "MyRepeat" and p["options"]["repeat_on_sheet"] is True for p in presets)

    # Auto-restore custom_name on fresh session when lps_device cookie is sent
    fresh = app.test_client()
    fresh.set_cookie("lps_device", job["device_id"])
    home = fresh.get("/")
    assert home.status_code == 200
    # Auto-restore on fresh session restores the last printed name
    assert "Azizbek Rahimov" in home.get_data(as_text=True)


def test_admin_device_management(app, admin, svc):
    r = admin.c.post("/admin/devices/create", data={
        "device_id": "lab-pc-01", "custom_name": "Lab PC 1", "csrf_token": admin.token})
    assert r.status_code == 302
    dev = svc.db.get_device("lab-pc-01")
    assert dev and dev["custom_name"] == "Lab PC 1"

    from lps.jobs import PrintOptions
    svc.db.touch_device("ghost-visitor-99", ip="10.0.0.99", custom_name="")
    svc.db.touch_device("nameless-printer-02", ip="10.0.0.56", custom_name="")
    svc.db.touch_device("name-entered-no-jobs", ip="10.0.0.57", custom_name="", last_known_name="EnteredNameOnly")
    svc.jobs.create(user_id=None, username="Dilshod", owner_key="name:dilshod",
                    device_id="lab-pc-01", client_ip="10.0.0.55",
                    opts=PrintOptions(), parts=[{"filename": "doc.pdf", "doc_type": "pdf", "stored_name": "x.pdf", "file_size": 100, "page_count": 1}])
    svc.jobs.create(user_id=None, username="AutoFilledUser", owner_key="name:autofilleduser",
                    device_id="nameless-printer-02", client_ip="10.0.0.56",
                    opts=PrintOptions(), parts=[{"filename": "doc2.pdf", "doc_type": "pdf", "stored_name": "y.pdf", "file_size": 100, "page_count": 1}])
    page = admin.get("/admin/devices").get_data(as_text=True)
    assert "lab-pc-01" in page and "Lab PC 1" in page
    assert "ghost-visitor-99" not in page
    assert svc.db.get_device("ghost-visitor-99") is None
    assert 'value="AutoFilledUser"' in page
    assert "name-entered-no-jobs" in page
    assert 'value="EnteredNameOnly"' in page
    assert "Last printed as: <strong>Dilshod</strong>" in page
    assert "flex-grow: 1; min-width: 130px;" in page
    assert "white-space: normal; word-break: break-word;" in page
    assert 'href="/admin/jobs?filter_device=lab-pc-01"' in page
    jobs_by_dev = admin.get("/admin/jobs?filter_device=lab-pc-01").get_data(as_text=True)
    assert "Dilshod" in jobs_by_dev

    r2 = admin.c.post("/admin/devices/lab-pc-01/rename", data={
        "custom_name": "Lab Workstation 1", "csrf_token": admin.token})
    assert r2.status_code == 302
    assert svc.db.get_device("lab-pc-01")["custom_name"] == "Lab Workstation 1"

    r3 = admin.c.post("/admin/devices/lab-pc-01/delete", data={"csrf_token": admin.token})
    assert r3.status_code == 302
    assert svc.db.get_device("lab-pc-01") is None


def test_v2_to_v3_schema_migration_in_place(tmp_path):
    import sqlite3
    from lps.db import Database
    db_file = tmp_path / "legacy_v2.db"
    conn = sqlite3.connect(db_file)
    conn.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, owner_key TEXT, created_at TEXT);
        CREATE TABLE uploads (id TEXT PRIMARY KEY, owner_key TEXT);
    """)
    conn.close()
    db = Database(db_file)
    assert db.one("SELECT value FROM meta WHERE key = 'schema_version'")["value"] == "3"
    job_cols = {r["name"] for r in db.query("PRAGMA table_info(jobs)")}
    up_cols = {r["name"] for r in db.query("PRAGMA table_info(uploads)")}
    assert "device_id" in job_cols and "repeat_on_sheet" in job_cols
    assert "device_id" in up_cols


def test_admin_login_from_change_name_and_logout(app, svc):
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")
    c, _ = name_login(app, "555")
    # User clicks Change name (/change-name) and submits admin password login form
    r = c.c.post("/change-name", data={
        "username": "admin", "password": ADMIN_PW, "csrf_token": c.token})
    assert r.status_code == 302
    assert c.get("/admin/").status_code == 200
    # Refresh CSRF token after session rotation and verify logout does not auto-login back as 555
    admin_page = c.get("/admin/").get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', admin_page)
    if m:
        c.token = m.group(1)
    c.c.post("/logout", data={"csrf_token": c.token})
    assert c.get("/").status_code == 302


def test_file_expired_badge_when_physical_file_deleted(app, admin, alice, svc):
    up = upload_ok(alice, make_color_pdf(1), "archive_check.pdf")
    jid = alice.post("/api/jobs", json={"documents": [{"upload_id": up["id"]}]}).get_json()["job"]["id"]
    wait_status(svc, jid, {J.COMPLETED})
    # Removing document card from Print UI after printing must NOT break file_exists
    alice.delete(f"/api/uploads/{up['id']}")
    assert svc.jobs.get(jid)["file_exists"] is True
    assert '<span class="badge muted">File Expired (7 Days)</span>' not in alice.get("/history").get_data(as_text=True)
    # Delete physical file from uploads_dir to simulate 7-day cleanup.ps1
    for f in svc.cfg.uploads_dir.iterdir():
        f.unlink(missing_ok=True)
    assert svc.jobs.get(jid)["file_exists"] is False
    assert '<span class="badge muted">File Expired (7 Days)</span>' in alice.get("/history").get_data(as_text=True)
    assert '<span class="badge muted">File Expired (7 Days)</span>' in admin.get("/admin/jobs").get_data(as_text=True)


def test_file_exists_legacy_v110_backward_compatibility(svc):
    import json
    from datetime import datetime, timedelta, timezone
    from lps.options import PrintOptions

    # Legacy job from 3 days ago without upload_stored_name whose file exists in uploads_dir
    (svc.cfg.uploads_dir / "legacy_upload_3d.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * 1225)
    parts_3d = [{"filename": "legacy_3d.pdf", "doc_type": "pdf", "stored_name": "deadbeef1234.pdf",
                 "file_size": 1234, "page_count": 1}]
    jid_3d = svc.jobs.create(user_id=None, username="guest", owner_key="guest:1",
                             client_ip="127.0.0.1", opts=PrintOptions(), parts=parts_3d)
    ts_3d = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    svc.db.execute("UPDATE jobs SET created_at = ?, parts_json = ? WHERE id = ?",
                   (ts_3d, json.dumps(parts_3d), jid_3d))
    assert svc.jobs.get(jid_3d)["file_exists"] is True

    # Legacy job from 10 days ago without upload_stored_name and no physical file on disk
    parts_10d = [{"filename": "legacy_10d.pdf", "doc_type": "pdf", "stored_name": "deadbeef9999.pdf",
                  "file_size": 5678, "page_count": 1}]
    jid_10d = svc.jobs.create(user_id=None, username="guest", owner_key="guest:1",
                              client_ip="127.0.0.1", opts=PrintOptions(), parts=parts_10d)
    ts_10d = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    svc.db.execute("UPDATE jobs SET created_at = ?, parts_json = ? WHERE id = ?",
                   (ts_10d, json.dumps(parts_10d), jid_10d))
    assert svc.jobs.get(jid_10d)["file_exists"] is False


def test_admin_update_banner_and_trigger(app, admin, svc, monkeypatch):
    import subprocess
    assert 'action="/admin/trigger-update"' not in admin.get("/admin/").get_data(as_text=True)
    svc.manager.update_available = True
    dash = admin.get("/admin/").get_data(as_text=True)
    assert "Update downloaded and ready to install." in dash
    assert 'action="/admin/trigger-update"' in dash
    assert 'id="update-banner"' in dash
    assert "alert.id !== 'update-banner'" in dash

    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    r = admin.c.post("/admin/trigger-update", data={"csrf_token": admin.token})
    assert r.status_code == 200
    assert "Server is updating and restarting. Please refresh in 10 seconds." in r.get_data(as_text=True)
    assert svc.manager.update_available is False
    assert calls and calls[0][0][-1] == "C:\\LocalPrintServer\\update.ps1"
    assert calls[0][1].get("close_fds") is True

    svc.manager.update_available = True
    svc.manager.latest_remote_hash = "deadbeef1234567890"
    dash2 = admin.get("/admin/").get_data(as_text=True)
    assert 'action="/admin/trigger-cancel-update"' in dash2
    r_cancel = admin.c.post("/admin/trigger-cancel-update", data={"csrf_token": admin.token})
    assert r_cancel.status_code in (302, 303)
    assert svc.manager.update_available is False
    row = svc.jobs.db.one("SELECT value FROM meta WHERE key = 'ignored_update_hash'")
    assert row and row["value"] == "deadbeef1234567890" 


def test_admin_jobs_smart_lightbox_previews(app, admin, alice, svc):
    from .conftest import make_png
    up_img = upload_ok(alice, make_png(), "photo_preview.png")
    jid_img = alice.post("/api/jobs", json={"documents": [{"upload_id": up_img["id"]}]}).get_json()["job"]["id"]
    wait_status(svc, jid_img, {J.COMPLETED})

    up_pdf = upload_ok(alice, make_color_pdf(1), "report.pdf")
    jid_pdf = alice.post("/api/jobs", json={"documents": [{"upload_id": up_pdf["id"]}]}).get_json()["job"]["id"]
    wait_status(svc, jid_pdf, {J.COMPLETED})

    # Simulate a converted Word (.docx) job whose stored file in uploads_dir is a converted .pdf
    docx_pdf_name = "aaaa1111bbbb2222cccc3333.pdf"
    (svc.cfg.uploads_dir / docx_pdf_name).write_bytes(make_color_pdf(1))
    from lps.options import PrintOptions
    jid_docx = svc.jobs.create(
        user_id=None, username="alice", owner_key="user:2", client_ip="127.0.0.1",
        opts=PrintOptions(),
        parts=[{"filename": "contract.docx", "doc_type": "docx", "stored_name": "temp_gone.pdf",
                "upload_stored_name": "aaaa1111bbbb2222cccc3333.bin", "file_size": 500, "page_count": 1}]
    )

    html = admin.get("/admin/jobs").get_data(as_text=True)
    assert 'id="lightbox-modal"' in html
    assert 'id="lightbox-close"' in html
    assert 'id="lightbox-content"' in html
    assert f'data-file-url="/api/jobs/{jid_img}/raw/0" data-file-type="image" class="job-preview-thumb"' in html
    assert f'data-file-url="/api/jobs/{jid_pdf}/raw/0" data-file-type="pdf"' in html
    assert f'data-file-url="/api/jobs/{jid_docx}/raw/0" data-file-type="pdf"' in html

    r_img = admin.get(f"/api/jobs/{jid_img}/raw/0")
    assert r_img.status_code == 200 and r_img.mimetype == "image/png"

    r_pdf = admin.get(f"/api/jobs/{jid_pdf}/raw/0")
    assert r_pdf.status_code == 200 and r_pdf.mimetype == "application/pdf"

    r_docx = admin.get(f"/api/jobs/{jid_docx}/raw/0")
    assert r_docx.status_code == 200 and r_docx.mimetype == "application/pdf"
    assert r_docx.data.startswith(b"%PDF-")






def test_user_deletion_and_root_admin_invincibility(app, admin, svc):
    from .conftest import Client
    root_id = svc.users.by_username("admin")["id"]
    sec_id = svc.users.create("admin2", "Second-Admin-99", "ADMIN", "Second Admin")
    usr_id = svc.users.create("tempuser", "Temp-User-Pass-99", "USER", "Temp User")

    # Root admin cannot disable or delete self
    assert admin.c.post(f"/admin/users/{root_id}/disable", data={"csrf_token": admin.token}).status_code == 403
    assert admin.c.post(f"/admin/users/delete/{root_id}", data={"csrf_token": admin.token}).status_code == 403

    # Secondary admin cannot disable or delete Root Admin ('admin') or self
    sec_client = Client(app)
    sec_client.login("admin2", "Second-Admin-99")
    assert sec_client.c.post(f"/admin/users/{root_id}/disable", data={"csrf_token": sec_client.token}).status_code == 403
    assert sec_client.c.post(f"/admin/users/delete/{root_id}", data={"csrf_token": sec_client.token}).status_code == 403
    assert sec_client.c.post(f"/admin/users/{root_id}/password", data={"csrf_token": sec_client.token, "password": "New-Pass-12345", "password2": "New-Pass-12345"}).status_code == 403
    assert sec_client.c.post(f"/admin/users/{root_id}/role", data={"csrf_token": sec_client.token, "role": "USER"}).status_code == 403
    assert sec_client.c.post(f"/admin/users/delete/{sec_id}", data={"csrf_token": sec_client.token}).status_code == 403
    sec_page = sec_client.get("/admin/users").get_data(as_text=True)
    assert f"/admin/users/{root_id}/password" not in sec_page
    assert f"/admin/users/delete/{root_id}" not in sec_page
    assert f"/admin/users/delete/{sec_id}" not in sec_page
    assert svc.users.get(root_id)["is_active"] == 1

    # Standard user and secondary admin can be deleted by Root Admin
    r_del = admin.c.post(f"/admin/users/delete/{usr_id}", data={"csrf_token": admin.token})
    assert r_del.status_code == 302
    assert svc.users.get(usr_id) is None
    r_del2 = admin.c.post(f"/admin/users/delete/{sec_id}", data={"csrf_token": admin.token})
    assert r_del2.status_code == 302
    assert svc.users.get(sec_id) is None


def test_user_edit_and_root_username_lock(app, admin, svc):
    root_id = svc.users.by_username("admin")["id"]
    uid = svc.users.create("editable1", "Initial-Pass-10", "USER", "Initial Name")

    # Root admin username cannot be changed via POST /admin/users/edit/<root_id>, but display_name can
    r_root = admin.c.post(f"/admin/users/edit/{root_id}", data={
        "csrf_token": admin.token,
        "username": "hackedroot",
        "display_name": "Chief Administrator",
    })
    assert r_root.status_code == 302
    root_after = svc.users.get(root_id)
    assert root_after["username"] == "admin"
    assert root_after["display_name"] == "Chief Administrator"

    # Secondary user can have username, display_name, and password updated
    r_usr = admin.c.post(f"/admin/users/edit/{uid}", data={
        "csrf_token": admin.token,
        "username": "renamed1",
        "display_name": "Renamed User",
        "password": "Updated-Pass-20",
        "password2": "Updated-Pass-20",
    })
    assert r_usr.status_code == 302
    usr_after = svc.users.get(uid)
    assert usr_after["username"] == "renamed1"
    assert usr_after["display_name"] == "Renamed User"
    assert svc.users.authenticate("renamed1", "Updated-Pass-20") is not None

    page = admin.get("/admin/users").get_data(as_text=True)
    assert 'readonly class="text-muted"' in page
    assert 'class="btn btn-sm btn-secondary muted"' in page
    assert "removeAttribute('open')" in page


def test_job_history_and_cancel_isolated_strictly_by_device_id(app, svc):
    from .conftest import Client, make_png, upload_ok
    svc.settings.update({"user_login_mode": "USERNAME"}, "test")

    # Client A on Device A
    client_a, r_a = name_login(app, "SharedName")
    assert r_a.status_code == 302
    up_a = upload_ok(client_a, make_png(), "alice_secret.png")
    resp_a = client_a.post("/api/jobs", json={"documents": [{"upload_id": up_a["id"]}]}).get_json()
    jid_a = resp_a["job"]["id"]

    # Client B on Device B: separate client/cookie jar, same print_name (spoof attempt)
    client_b, r_b = name_login(app, "SharedName")
    assert r_b.status_code == 302

    # Verify device IDs are indeed different
    job_a_data = svc.jobs.get(jid_a)
    dev_a = job_a_data["device_id"]
    r_check = client_b.get("/api/jobs")
    assert r_check.status_code == 200

    # Client B cannot see Job A via /api/jobs
    b_jobs = client_b.get("/api/jobs").get_json()["jobs"]
    assert all(j["id"] != jid_a for j in b_jobs)

    # Client B cannot see Job A via /history
    b_hist = client_b.get("/history").get_data(as_text=True)
    assert "alice_secret.png" not in b_hist
    assert jid_a not in b_hist

    # Client B cannot see or inspect Job A via /api/jobs/<jid>
    assert client_b.get(f"/api/jobs/{jid_a}").status_code == 404

    # Client B cannot download raw files of Job A
    assert client_b.get(f"/api/jobs/{jid_a}/raw/0").status_code == 404

    # Client B cannot cancel Job A
    assert client_b.post(f"/api/jobs/{jid_a}/cancel").status_code == 404
    assert svc.jobs.get(jid_a)["cancel_requested"] == 0

    # Client A CAN see Job A in /api/jobs and /history
    a_jobs = client_a.get("/api/jobs").get_json()["jobs"]
    assert any(j["id"] == jid_a for j in a_jobs)
    a_hist = client_a.get("/history").get_data(as_text=True)
    assert "alice_secret.png" in a_hist
    assert jid_a in a_hist

    # Client A CAN cancel Job A
    cancel_resp = client_a.post(f"/api/jobs/{jid_a}/cancel").get_json()
    assert cancel_resp["ok"] is True
    assert svc.jobs.get(jid_a)["cancel_requested"] == 1



def test_admin_health_and_one_click_reprint(app, admin, alice, svc):
    from .conftest import make_png, upload_ok

    # 1. Verify /admin/api/health and dashboard health widget
    rh = admin.get("/admin/api/health").get_json()
    assert rh["ok"] is True
    assert isinstance(rh["cpu_percent"], (int, float))
    assert isinstance(rh["ram_percent"], (int, float))
    assert isinstance(rh["disk_free_gb"], (int, float)) and rh["disk_free_gb"] > 0
    dash = admin.get("/admin/").get_data(as_text=True)
    assert 'id="health-cpu"' in dash and 'id="health-ram"' in dash and 'id="health-disk"' in dash

    # 2. Verify One-Click Reprint preserves exact original print settings
    up = upload_ok(alice, make_png(), "reprint_test.png")
    orig_jid = alice.post("/api/jobs", json={
        "documents": [{"upload_id": up["id"]}],
        "options": {"paper": "A5", "copies": 3, "nup": 2, "repeat_on_sheet": True, "color": "MONO"},
    }).get_json()["job"]["id"]
    wait_status(svc, orig_jid, {J.COMPLETED})

    jobs_html = admin.get("/admin/jobs").get_data(as_text=True)
    assert f'data-reprint-job-id="{orig_jid}"' in jobs_html

    rr = admin.post(f"/admin/api/job/{orig_jid}/reprint").get_json()
    assert rr["ok"] is True
    new_jid = rr["job_id"]
    assert new_jid != orig_jid
    new_job = wait_status(svc, new_jid, {J.COMPLETED})
    assert new_job["paper"] == "A5"
    assert new_job["copies"] == 3
    assert new_job["nup"] == 2
    assert new_job["repeat_on_sheet"] == 1
    assert new_job["color"] == "MONO"



def test_user_reprint_security_lock_and_cropper_modal(app, alice, bob, svc):
    from .conftest import make_color_pdf, make_png, upload_ok

    # 1. Verify Cropper.js CDN & modal markup on main print page
    index_html = alice.get("/").get_data(as_text=True)
    assert "cropper.min.css" in index_html and "cropper.min.js" in index_html
    assert 'id="cropper-modal"' in index_html
    assert 'id="cropper-rotate-btn"' in index_html
    assert 'id="cropper-apply-btn"' in index_html

    # 2. Verify PDF upload & print still works normally
    up_pdf = upload_ok(alice, make_color_pdf(2), "normal_doc.pdf")
    jid_pdf = alice.post("/api/jobs", json={
        "documents": [{"upload_id": up_pdf["id"]}],
        "options": {"paper": "A4", "copies": 2},
    }).get_json()["job"]["id"]
    wait_status(svc, jid_pdf, {J.COMPLETED})

    # 3. Verify User History UI renders localized Reprint button (EN, RU, UZ)
    hist_en = alice.get("/history").get_data(as_text=True)
    assert f'data-reprint-job="{jid_pdf}"' in hist_en
    assert "Reprint" in hist_en
    alice.get("/lang/ru")
    assert "Повторить" in alice.get("/history").get_data(as_text=True)
    alice.get("/lang/uz")
    assert "Takrorlash" in alice.get("/history").get_data(as_text=True)
    alice.get("/lang/en")

    # 4. SECURITY LOCK: Bob cannot reprint Alice's job
    r_bob = bob.post(f"/api/job/{jid_pdf}/reprint")
    assert r_bob.status_code == 403

    # 5. Alice CAN reprint her own job with exact original settings
    r_alice = alice.post(f"/api/job/{jid_pdf}/reprint")
    assert r_alice.status_code == 200
    reprint_data = r_alice.get_json()
    assert reprint_data["ok"] is True
    new_jid = reprint_data["job_id"]
    assert new_jid != jid_pdf
    new_job = wait_status(svc, new_jid, {J.COMPLETED})
    assert new_job["paper"] == "A4"
    assert new_job["copies"] == 2
