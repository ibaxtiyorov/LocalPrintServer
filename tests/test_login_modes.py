"""Username-only mode, guest migration and print presets."""
import json
import re

from lps import jobs as J

from .conftest import ADMIN_PW, Client, make_color_pdf, upload_ok, wait_status


def name_login(app, name):
    c = Client(app)
    r = c.c.post("/login", data={"login_type": "name", "print_name": name, "csrf_token": c.token})
    page = c.c.get("/").get_data(as_text=True)
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
