"""Login / language redirects must never leave the site (open-redirect protection)."""
import pytest

from .conftest import USER_PW, Client

EVIL = ["//evil.com", "/\\evil.com", "\\\\evil.com", "https://evil.com", "http:/evil.com",
        "/\t/evil.com", "javascript:alert(1)"]


@pytest.mark.parametrize("target", EVIL)
def test_login_next_cannot_leave_site(app, target):
    c = Client(app)
    r = c.c.post("/login?next=" + target, data={"username": "alice", "password": USER_PW,
                                                "csrf_token": c.token})
    assert r.status_code == 302 and r.headers["Location"] == "/"


@pytest.mark.parametrize("target", EVIL)
def test_language_switch_cannot_leave_site(app, target):
    r = app.test_client().get("/lang/ru", query_string={"next": target})
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_local_next_is_kept(app):
    c = Client(app)
    r = c.c.post("/login?next=/history", data={"username": "alice", "password": USER_PW,
                                               "csrf_token": c.token})
    assert r.headers["Location"] == "/history"
