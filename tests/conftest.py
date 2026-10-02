"""Shared fixtures. All tests use the simulated printer backend — no paper, no real spooler."""
from __future__ import annotations

import io

import re
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lps import create_app  # noqa: E402
from lps.config import load_config  # noqa: E402

ADMIN_PW = "Admin-Password-123"
USER_PW = "User-Password-456"


@pytest.fixture()
def app(tmp_path):
    cfg = load_config(path=tmp_path / "none.json", overrides={
        "printer_backend": "simulated", "office_converter": "simulated",
        "data_dir": str(tmp_path / "data"),
        "log_dir": str(tmp_path / "logs")})
    application = create_app(cfg, start_background=True)
    application.config["TESTING"] = True
    svc = application.extensions["lps"]
    svc.backend.print_seconds = 0.4
    svc.users.create("admin", ADMIN_PW, "ADMIN", "Admin")
    svc.users.create("alice", USER_PW, "USER", "Alice")
    svc.users.create("bob", USER_PW, "USER", "Bob")
    yield application
    svc.manager.stop()


@pytest.fixture()
def svc(app):
    return app.extensions["lps"]


def csrf_of(client) -> str:
    r = client.get("/login", follow_redirects=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', r.get_data(as_text=True))
    return m.group(1)


class Client:
    """Test client wrapper that remembers the CSRF token."""

    def __init__(self, app):
        self.c = app.test_client()
        self.token = csrf_of(self.c)

    def login(self, username, password):
        r = self.c.post("/login", data={"username": username, "password": password,
                                        "csrf_token": self.token})
        self.token = csrf_of(self.c) if r.status_code == 302 else self.token
        # login rotates the session; refresh token from an authenticated page
        page = self.c.get("/account")
        m = re.search(r'name="csrf-token" content="([^"]+)"', page.get_data(as_text=True))
        if m:
            self.token = m.group(1)
        return r

    def get(self, *a, **kw):
        return self.c.get(*a, **kw)

    def post(self, url, json=None, data=None, **kw):
        headers = kw.pop("headers", {})
        headers.setdefault("X-CSRF-Token", self.token)
        return self.c.post(url, json=json, data=data, headers=headers, **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers={"X-CSRF-Token": self.token}, **kw)


@pytest.fixture()
def admin(app):
    c = Client(app)
    assert c.login("admin", ADMIN_PW).status_code == 302
    return c


@pytest.fixture()
def alice(app):
    c = Client(app)
    assert c.login("alice", USER_PW).status_code == 302
    return c


@pytest.fixture()
def bob(app):
    c = Client(app)
    assert c.login("bob", USER_PW).status_code == 302
    return c


def make_png(w=400, h=600, color=(200, 30, 30), marker=True, dpi=None) -> bytes:
    im = Image.new("RGB", (w, h), color)
    if marker:   # black square in the top-left corner to detect orientation/mirroring
        for x in range(w // 5):
            for y in range(h // 5):
                im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG", **({"dpi": dpi} if dpi else {}))
    return buf.getvalue()


def make_pdf(pages=3, size=(595, 842)) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        w, h = size if i % 2 == 0 else size          # all same size by default
        p = doc.new_page(width=w, height=h)
        p.insert_text((72, 100), f"Page {i + 1}", fontsize=40)
    data = doc.tobytes()
    doc.close()
    return data


def upload(client, content: bytes, name: str):
    return client.post("/api/uploads", data={"file": (io.BytesIO(content), name)},
                       content_type="multipart/form-data")


def wait_status(svc, job_id, states, timeout=25.0):
    end = time.time() + timeout
    job = None
    while time.time() < end:
        job = svc.jobs.get(job_id)
        if job["status"] in states:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} stuck in {job and job['status']} ({job and job['status_detail']})")


# Distinct page colours: lets tests read back WHICH page landed on each printed sheet.
PAGE_COLORS = [(230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200), (245, 130, 48),
               (145, 30, 180), (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212),
               (0, 128, 128), (170, 110, 40), (128, 0, 0), (0, 0, 128), (128, 128, 0),
               (60, 60, 60), (255, 128, 128), (128, 255, 128), (128, 128, 255), (200, 200, 0)]


def make_color_pdf(n: int, start: int = 0, size=(595, 842)) -> bytes:
    """PDF whose page i is filled with PAGE_COLORS[start + i]."""
    import pymupdf
    doc = pymupdf.open()
    for i in range(n):
        p = doc.new_page(width=size[0], height=size[1])
        c = PAGE_COLORS[(start + i) % len(PAGE_COLORS)]
        p.draw_rect(p.rect, color=None, fill=tuple(v / 255 for v in c))
    data = doc.tobytes()
    doc.close()
    return data


def nearest_color(rgb) -> int:
    """Index in PAGE_COLORS closest to an observed pixel (or -1 for white/background)."""
    if min(rgb) > 245:
        return -1
    return min(range(len(PAGE_COLORS)),
               key=lambda i: sum((a - b) ** 2 for a, b in zip(PAGE_COLORS[i], rgb)))


def make_ole(stream_name: str) -> bytes:
    """Minimal OLE2 compound file whose 2nd directory entry is `stream_name`."""
    header = bytearray(512)
    header[:8] = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
    sector = bytearray(512)

    def entry(off, name, typ):
        enc = name.encode("utf-16-le")
        sector[off:off + len(enc)] = enc
        sector[off + 64:off + 66] = ((len(name) + 1) * 2).to_bytes(2, "little")
        sector[off + 66] = typ
    entry(0, "Root Entry", 5)
    entry(128, stream_name, 2)
    return bytes(header) + bytes(sector) + b"\x00" * 1024


def upload_ok(client, content: bytes, name: str) -> dict:
    r = upload(client, content, name)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["upload"]
