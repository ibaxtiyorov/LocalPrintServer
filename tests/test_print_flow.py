"""End-to-end printing through the simulated spooler (no paper used).

Sheets "printed" by the simulator are PNG files; inspecting them proves that the
selected settings were really applied to the output, not just stored.
"""
import base64
import io
import threading
import time

import pytest
from PIL import Image

from lps import jobs as J
from lps.printer.sim_backend import SIM_DPI

from .conftest import make_pdf, make_png, upload, wait_status


def pixels(im):
    """Pixel values on Pillow 11 (getdata) and Pillow 12+ (get_flattened_data)."""
    return im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata()


def sheets_for(svc, job_id):
    d = [p for p in svc.cfg.sim_output_dir.iterdir() if p.name.startswith(f"LPS-{job_id}")]
    assert len(d) == 1, d
    return [Image.open(p).convert("RGB") for p in sorted(d[0].glob("sheet-*.png"))]


def submit(client, content, name, **opts):
    r = upload(client, content, name)
    assert r.status_code == 200, r.get_json()
    up = r.get_json()["upload"]["id"]
    r = client.post("/api/jobs", json={"upload_id": up, "options": opts})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["job"]["id"]


def dark_quadrant(im) -> str:
    """Which quadrant of the sheet holds the black marker (top-left of the source image)."""
    w, h = im.size
    best, name = None, None
    for qn, box in {"TL": (0, 0, w // 2, h // 2), "TR": (w // 2, 0, w, h // 2),
                    "BL": (0, h // 2, w // 2, h), "BR": (w // 2, h // 2, w, h)}.items():
        g = im.crop(box).convert("L")
        dark = sum(1 for v in pixels(g) if v < 40)
        if best is None or dark > best:
            best, name = dark, qn
    return name


def test_image_job_full_lifecycle(alice, svc):
    jid = submit(alice, make_png(), "photo.png", paper="A4", copies=1)
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["spooler_job_id"] and job["submitted_at"] and job["finished_at"]
    assert job["status_detail"]            # explains how completion was observed
    # timestamps are ordered
    assert job["created_at"] <= job["queued_at"] <= job["submitted_at"] <= job["finished_at"]
    sheets = sheets_for(svc, jid)
    assert len(sheets) == 1
    w_mm, h_mm = (s / SIM_DPI * 25.4 for s in sheets[0].size)
    assert w_mm == pytest.approx(210, abs=1) and h_mm == pytest.approx(297, abs=1)
    assert not (svc.cfg.jobs_dir / job["stored_name"]).exists()     # temp file cleaned up


def test_job_goes_through_submitted_not_directly_completed(alice, svc):
    """EndDoc returning must not be reported as printed (spec §26)."""
    svc.backend.print_seconds = 3
    jid = submit(alice, make_png(), "a.png")
    job = wait_status(svc, jid, {J.SUBMITTED, J.PRINTING})
    assert job["status"] in (J.SUBMITTED, J.PRINTING)
    wait_status(svc, jid, {J.COMPLETED})


def test_pdf_multi_page_reverse_and_nup(alice, svc):
    jid = submit(alice, make_pdf(5), "doc.pdf", nup=2, reverse=True, orientation="AUTO")
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["sheet_count"] == 3 and job["page_count"] == 5
    sheets = sheets_for(svc, jid)
    assert len(sheets) == 3
    assert sheets[0].width > sheets[0].height          # AUTO picked landscape for 2-up portrait pages
    # reverse: first output sheet holds only page 5 -> right half is empty
    right = sheets[0].crop((sheets[0].width // 2 + 10, 0, sheets[0].width, sheets[0].height)).convert("L")
    assert min(pixels(right)) > 250


def test_pdf_page_range(alice, svc):
    jid = submit(alice, make_pdf(6), "doc.pdf", page_range="2-3, 6")
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["sheet_count"] == 3
    assert len(sheets_for(svc, jid)) == 3


def test_landscape_and_paper_applied(alice, svc):
    jid = submit(alice, make_png(600, 400), "wide.png", paper="A5", orientation="LANDSCAPE")
    wait_status(svc, jid, {J.COMPLETED})
    im = sheets_for(svc, jid)[0]
    assert im.width / SIM_DPI * 25.4 == pytest.approx(210, abs=1)
    assert im.height / SIM_DPI * 25.4 == pytest.approx(148, abs=1)


def test_custom_paper(alice, svc):
    jid = submit(alice, make_png(), "c.png", paper="CUSTOM", custom_w_mm=200, custom_h_mm=250)
    wait_status(svc, jid, {J.COMPLETED})
    im = sheets_for(svc, jid)[0]
    assert (im.width / SIM_DPI * 25.4, im.height / SIM_DPI * 25.4) == (
        pytest.approx(200, abs=1), pytest.approx(250, abs=1))


@pytest.mark.parametrize("opts,quadrant", [({}, "TL"), ({"mirror": True}, "TR"),
                                           ({"rotate180": True}, "BR"),
                                           ({"mirror": True, "rotate180": True}, "BL")])
def test_mirror_and_rotation_applied_in_output(alice, svc, opts, quadrant):
    jid = submit(alice, make_png(400, 566), "m.png", scaling="FILL", orientation="PORTRAIT", **opts)
    wait_status(svc, jid, {J.COMPLETED})
    assert dark_quadrant(sheets_for(svc, jid)[0]) == quadrant


def test_monochrome_applied(alice, svc):
    jid = submit(alice, make_png(color=(220, 20, 20)), "r.png", color="MONO")
    wait_status(svc, jid, {J.COMPLETED})
    im = sheets_for(svc, jid)[0]
    assert all(r == g == b for r, g, b in pixels(im.resize((50, 70))))


def test_actual_size_100mm(alice, svc):
    jid = submit(alice, make_png(1181, 1181, marker=False, color=(0, 0, 0), dpi=(300, 300)),
                 "sq.png", scaling="ACTUAL")
    wait_status(svc, jid, {J.COMPLETED})
    im = sheets_for(svc, jid)[0].convert("L")
    bbox = Image.eval(im, lambda v: 255 if v < 128 else 0).getbbox()
    assert (bbox[2] - bbox[0]) / SIM_DPI * 25.4 == pytest.approx(100, abs=1)


def test_copies_driver_mode_vs_application_mode(alice, svc):
    jid = submit(alice, make_pdf(2), "d.pdf", copies=2, collate=True)
    wait_status(svc, jid, {J.COMPLETED})
    assert len(sheets_for(svc, jid)) == 2           # driver makes the copies (DEVMODE Copies)
    svc.settings.update({"copies_mode": "APPLICATION"}, "test")
    jid = submit(alice, make_pdf(2), "d.pdf", copies=2, collate=True)
    wait_status(svc, jid, {J.COMPLETED})
    assert len(sheets_for(svc, jid)) == 4           # 1,2,1,2 rendered by the app


def test_preview_matches_options(alice, svc):
    r = upload(alice, make_pdf(4), "p.pdf")
    up = r.get_json()["upload"]["id"]
    r = alice.post("/api/preview", json={"upload_id": up, "options": {"nup": 4, "paper": "A4"}})
    data = r.get_json()
    assert data["ok"] and data["meta"]["sheets"] == 1 and data["meta"]["pages_selected"] == 4
    img = Image.open(io.BytesIO(base64.b64decode(data["image"].split(",", 1)[1])))
    assert img.height > img.width
    r = alice.post("/api/preview", json={"upload_id": up, "options": {"orientation": "LANDSCAPE"}})
    meta = r.get_json()["meta"]
    assert meta["orientation"] == "LANDSCAPE" and meta["sheets"] == 4
    r = alice.post("/api/preview", json={"upload_id": up, "options": {"paper": "CUSTOM",
                                                                     "custom_w_mm": 50, "custom_h_mm": 50}})
    assert r.status_code == 400 and r.get_json()["fields"] == {"custom": "err.custom_size"}


def test_cancel_while_queued(alice, svc):
    svc.settings.update({"queue_paused": True}, "test")
    jid = submit(alice, make_png(), "a.png")
    assert svc.jobs.get(jid)["status"] == J.QUEUED
    r = alice.post(f"/api/jobs/{jid}/cancel")
    assert r.get_json()["result"] == "cancelled"
    assert svc.jobs.get(jid)["status"] == J.CANCELLED
    svc.settings.update({"queue_paused": False}, "test")
    time.sleep(1)
    assert svc.jobs.get(jid)["status"] == J.CANCELLED       # never printed
    assert not any(p.name.startswith(f"LPS-{jid}") for p in svc.cfg.sim_output_dir.iterdir())


def test_cancel_while_in_spooler(alice, svc):
    svc.backend.set_usb(False)                   # job will wait in the Windows queue
    jid = submit(alice, make_png(), "a.png")
    wait_status(svc, jid, {J.WAITING, J.SUBMITTED})
    r = alice.post(f"/api/jobs/{jid}/cancel")
    assert r.get_json()["result"] == "cancelling"
    job = wait_status(svc, jid, {J.CANCELLED})
    assert "may still print" in job["status_detail"]
    assert svc.backend.list_jobs() == []


def test_users_cannot_see_or_cancel_each_others_jobs(alice, bob, admin, svc):
    svc.settings.update({"queue_paused": True}, "test")
    jid = submit(alice, make_png(), "secret.png")
    assert bob.get(f"/api/jobs/{jid}").status_code == 404
    assert bob.post(f"/api/jobs/{jid}/cancel").status_code == 404
    assert all(j["id"] != jid for j in bob.get("/api/jobs").get_json()["jobs"])
    assert "secret.png" not in bob.get("/history").get_data(as_text=True)
    assert "secret.png" in alice.get("/history").get_data(as_text=True)
    assert "secret.png" in admin.get("/admin/jobs").get_data(as_text=True)
    assert admin.post(f"/admin/api/jobs/{jid}/cancel").get_json()["result"] == "cancelled"


def test_usb_disconnect_then_reconnect_prints(alice, svc):
    """Spec §25: job waits in the Windows queue while USB is unplugged, prints after reconnect."""
    svc.settings.update({"stall_seconds": 20}, "test")
    svc.backend.set_usb(False)
    jid = submit(alice, make_png(), "a.png")
    job = wait_status(svc, jid, {J.WAITING})
    assert "not ready" in job["status_detail"]
    st = svc.status.get(force=True)
    assert st["state"] in ("ERROR", "WAITING")           # never "READY" with status 0
    svc.backend.set_usb(True)
    wait_status(svc, jid, {J.COMPLETED})
    assert svc.status.get(force=True)["state"] == "READY"


def test_status_zero_never_means_ready_without_evidence(svc):
    svc.backend.usb_present = lambda force=False: None      # no USB evidence available
    st = svc.status.get(force=True)
    assert st["evidence"]["printer"]["status"] == 0
    assert st["state"] == "IDLE"
    assert "status.reason.idle_unverified" in st["reasons"]


def test_spooler_down_job_retries_then_prints(alice, svc):
    svc.backend.set_spooler(False)
    jid = submit(alice, make_png(), "a.png")
    time.sleep(1.5)
    job = svc.jobs.get(jid)
    assert job["status"] == J.QUEUED and job["error_code"] == "err.spooler_unavailable"
    assert svc.status.get(force=True)["state"] == "SPOOLER_ERROR"
    svc.backend.set_spooler(True)
    svc.jobs.update(jid, next_attempt_at=None)       # skip the back-off delay in the test
    svc.manager.wake()
    wait_status(svc, jid, {J.COMPLETED})


def test_spooler_restart_marks_in_flight_job_unknown(alice, admin, svc):
    svc.backend.set_usb(False)
    jid = submit(alice, make_png(), "a.png")
    wait_status(svc, jid, {J.WAITING, J.SUBMITTED})
    r = admin.post("/admin/api/spooler/restart")
    assert r.get_json()["ok"]
    job = wait_status(svc, jid, {J.UNKNOWN})
    assert "Spooler restart" in job["status_detail"]
    svc.backend.set_usb(True)
    jid2 = submit(alice, make_png(), "b.png")         # service recovers for new jobs
    wait_status(svc, jid2, {J.COMPLETED})
    rows, _ = svc.audit.search(action="spooler.restart")
    assert rows and rows[0]["actor"] == "admin"


def test_concurrent_users_keep_their_own_settings(app, alice, bob, svc):
    """Final req. §15: many simultaneous submissions; each job keeps its own settings."""
    from .conftest import Client, USER_PW
    results = {}
    specs = [("alice", "A4", "PORTRAIT"), ("bob", "A5", "LANDSCAPE"), ("alice", "P10X15", "PORTRAIT"),
             ("bob", "A6", "LANDSCAPE"), ("alice", "LETTER", "LANDSCAPE"), ("bob", "B5", "PORTRAIT")]

    pngs = {}

    def run(i, who, paper, orient):
        # separate client per thread to mimic separate devices
        c = Client(app)
        c.login(who, USER_PW)
        pngs[i] = make_png()
        results[i] = (submit(c, pngs[i], f"{who}-{i}.png", paper=paper, orientation=orient), paper, orient)

    threads = [threading.Thread(target=run, args=(i, *s)) for i, s in enumerate(specs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    from lps import catalog
    for i, (jid, paper, orient) in results.items():
        job = wait_status(svc, jid, {J.COMPLETED}, timeout=60)
        assert (job["paper"], job["orientation"]) == (paper, orient)
        im = sheets_for(svc, jid)[0]
        p = catalog.PAPER_BY_KEY[paper]
        exp = (p.width_mm, p.length_mm) if orient == "PORTRAIT" else (p.length_mm, p.width_mm)
        got = (im.width / SIM_DPI * 25.4, im.height / SIM_DPI * 25.4)
        assert got == (pytest.approx(exp[0], abs=1), pytest.approx(exp[1], abs=1)), (paper, orient)
    owners = {svc.jobs.get(jid)["username"] for jid, _, _ in results.values()}
    assert owners == {"alice", "bob"}


def test_restart_recovery_fails_interrupted_render(svc):
    from lps.options import PrintOptions
    jid = svc.jobs.create(user_id=None, username="x", owner_key="user:1", client_ip=None,
                          filename="f.png", doc_type="png", stored_name=None, file_size=1,
                          page_count=1, opts=PrintOptions(pages=[0]))
    svc.jobs.transition(jid, J.RENDERING)
    assert svc.jobs.recover_after_restart() == [jid]
    assert svc.jobs.get(jid)["error_code"] == "err.server_restarted"


def test_invalid_upload_messages(alice):
    r = upload(alice, b"MZ\x90\x00" + b"\x00" * 100, "virus.pdf")
    assert r.status_code == 400 and r.get_json()["error"] == "err.unsupported_type"
    r = upload(alice, b"", "empty.png")
    assert r.get_json()["error"] == "err.empty_file"


def test_admin_test_page_and_queue_controls(admin, svc):
    r = admin.post("/admin/api/test-page", json={"color": True})
    jid = r.get_json()["job_id"]
    wait_status(svc, jid, {J.COMPLETED})
    assert admin.post("/admin/api/queue/pause").get_json()["ok"]
    assert svc.settings.get("queue_paused") is True
    assert admin.post("/admin/api/queue/resume").get_json()["ok"]
    assert admin.post("/admin/api/printer/pause").get_json()["ok"]
    assert svc.status.get(force=True)["state"] == "PAUSED"
    assert admin.post("/admin/api/printer/resume").get_json()["ok"]
    q = admin.get("/admin/api/queue").get_json()
    assert "windows_jobs" in q and "app_jobs" in q
    actions = {r["action"] for r in svc.audit.search(limit=100)[0]}
    assert {"printer.test_page", "queue.pause", "queue.resume", "printer.pause", "printer.resume"} <= actions


def test_driver_check_uses_no_paper(admin, svc):
    r = admin.post("/admin/api/driver-check", json={})
    res = r.get_json()["result"]
    assert len(res["papers"]) == 16 and len(res["media"]) == 27
    assert res["summary"] == {"adjusted": 0, "errors": 0, "size_mismatches": 0}
    assert svc.backend.list_jobs() == []                      # no spooler job created
    assert not list(svc.cfg.sim_output_dir.iterdir())         # nothing "printed"


def test_cancel_arriving_after_last_page_check_is_not_lost(alice, svc, monkeypatch):
    """Race: cancel lands after EndDoc but before the job is marked SUBMITTED.
    The job must be removed from the Windows queue, not printed and shown as cancelled."""
    svc.backend.print_seconds = 30                 # keep it in the (fake) Windows queue
    real_open = svc.backend.open_session

    def open_session(ds, merge):
        sess = real_open(ds, merge)
        real_end = sess.end_doc

        def end_doc():
            real_end()
            jid = svc.manager.current_job
            svc.manager.cancel(jid, "alice")       # user clicks Cancel right now
        sess.end_doc = end_doc
        return sess
    monkeypatch.setattr(svc.backend, "open_session", open_session)
    jid = submit(alice, make_png(), "late.png")
    job = wait_status(svc, jid, {J.CANCELLED, J.COMPLETED})
    assert job["status"] == J.CANCELLED
    assert svc.backend.list_jobs() == []           # deleted from the Windows queue


def test_printer_error_after_cancel_request_reports_cancelled(alice, svc, monkeypatch):
    from lps.printer.base import PrinterError
    svc.settings.update({"queue_paused": True}, "test")
    jid = submit(alice, make_png(), "x.png")

    def failing_open(ds, merge):
        svc.jobs.update(jid, cancel_requested=1)
        raise PrinterError("err.spooler_unavailable", retryable=True)
    monkeypatch.setattr(svc.backend, "open_session", failing_open)
    svc.settings.update({"queue_paused": False}, "test")
    job = wait_status(svc, jid, {J.CANCELLED, J.FAILED, J.QUEUED}, timeout=15)
    for _ in range(50):
        job = svc.jobs.get(jid)
        if job["status"] != J.QUEUED:
            break
        time.sleep(0.1)
    assert job["status"] == J.CANCELLED
