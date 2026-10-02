"""End-to-end: page selection, multiple documents, ordering, N-up, copies/collation,
cancellation and cleanup - through the API and the simulated printer (no paper).

Every test page is a unique solid colour, so the printed (simulated) sheets show
exactly which pages were printed and in what order."""
from PIL import Image

from lps import jobs as J

from .conftest import (make_color_pdf, make_png, nearest_color, upload, upload_ok,
                       wait_status)


def sheets(svc, job_id):
    d = [p for p in svc.cfg.sim_output_dir.iterdir() if p.name.startswith(f"LPS-{job_id}")]
    assert len(d) == 1
    return [Image.open(p).convert("RGB") for p in sorted(d[0].glob("sheet-*.png"))]


def centre_colors(svc, job_id):
    return [nearest_color(im.getpixel((im.width // 2, im.height // 2))) for im in sheets(svc, job_id)]


def colors_on_sheet(im, step=6):
    """Page colours present on a sheet; anti-aliased edge pixels (colour blended with
    white) are ignored by requiring a close match."""
    from .conftest import PAGE_COLORS
    small = im.resize((im.width // step, im.height // step), Image.NEAREST)
    data = small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata()
    found = set()
    for px in data:
        c = nearest_color(px)
        if c >= 0 and sum((a - b) ** 2 for a, b in zip(PAGE_COLORS[c], px)) < 3 * 12 ** 2:
            found.add(c)
    return found


def submit(client, docs, **options):
    r = client.post("/api/jobs", json={"documents": docs, "options": options})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["job"]["id"]


def test_only_selected_pages_are_printed(alice, svc):
    up = upload_ok(alice, make_color_pdf(6), "six.pdf")
    jid = submit(alice, [{"upload_id": up["id"], "page_range": "2,5"}])
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["sheet_count"] == 2
    assert centre_colors(svc, jid) == [1, 4]          # pages 2 and 5 only


def test_mixed_selection_spec_example(alice, svc):
    up = upload_ok(alice, make_color_pdf(20), "twenty.pdf")
    jid = submit(alice, [{"upload_id": up["id"], "page_range": "2,5-8,15,20"}])
    wait_status(svc, jid, {J.COMPLETED})
    assert centre_colors(svc, jid) == [1, 4, 5, 6, 7, 14, 19]


def test_multiple_documents_in_chosen_order(alice, svc):
    a = upload_ok(alice, make_color_pdf(3, start=0), "a.pdf")        # colours 0,1,2
    b = upload_ok(alice, make_color_pdf(2, start=3), "b.pdf")        # colours 3,4
    docs = [{"upload_id": b["id"], "page_range": "2"}, {"upload_id": a["id"], "page_range": "1,3"}]
    pv = alice.post("/api/preview", json={"documents": docs, "options": {}}).get_json()
    assert pv["ok"] and pv["meta"]["documents"] == 2
    assert pv["meta"]["order"] == [[[1, 2]], [[2, 1]], [[2, 3]]]     # exact print order
    jid = submit(alice, docs)
    wait_status(svc, jid, {J.COMPLETED})
    assert centre_colors(svc, jid) == [4, 0, 2]
    job = alice.get(f"/api/jobs/{jid}").get_json()["job"]
    assert [(p["filename"], p["page_range"], p["selected"]) for p in job["parts"]] == \
        [("b.pdf", "2", 1), ("a.pdf", "1,3", 2)]


def test_image_and_pdf_mixed(alice, svc):
    img = upload_ok(alice, make_png(color=(230, 25, 75), marker=False), "photo.png")
    pdf = upload_ok(alice, make_color_pdf(2, start=3), "doc.pdf")
    jid = submit(alice, [{"upload_id": pdf["id"], "page_range": ""}, {"upload_id": img["id"]}])
    wait_status(svc, jid, {J.COMPLETED})
    assert centre_colors(svc, jid) == [3, 4, 0]


def test_reverse_with_collated_and_uncollated_copies(alice, svc):
    svc.settings.update({"copies_mode": "APPLICATION"}, "test")   # copies visible in output
    up = upload_ok(alice, make_color_pdf(3), "abc.pdf")
    jid = submit(alice, [{"upload_id": up["id"]}], copies=2, collate=True, reverse=True)
    wait_status(svc, jid, {J.COMPLETED})
    assert centre_colors(svc, jid) == [2, 1, 0, 2, 1, 0]
    jid = submit(alice, [{"upload_id": up["id"]}], copies=2, collate=False, reverse=True)
    wait_status(svc, jid, {J.COMPLETED})
    assert centre_colors(svc, jid) == [2, 2, 1, 1, 0, 0]


def test_driver_copies_send_each_sheet_once(alice, svc):
    up = upload_ok(alice, make_color_pdf(3), "abc.pdf")
    jid = submit(alice, [{"upload_id": up["id"], "page_range": "1-2"}], copies=3, collate=True)
    job = wait_status(svc, jid, {J.COMPLETED})
    assert centre_colors(svc, jid) == [0, 1]        # DEVMODE.Copies=3 makes the copies
    assert job["copies"] == 3


def test_nine_up_composes_selected_pages(alice, svc):
    up = upload_ok(alice, make_color_pdf(12), "twelve.pdf")
    jid = submit(alice, [{"upload_id": up["id"], "page_range": "1-10"}], nup=9)
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["sheet_count"] == 2
    s1, s2 = sheets(svc, jid)
    assert colors_on_sheet(s1) == set(range(9))
    assert colors_on_sheet(s2) == {9}               # pages 11-12 never printed


def test_sixteen_up_and_six_up(alice, svc):
    up = upload_ok(alice, make_color_pdf(20), "twenty.pdf")
    for nup, expected in ((16, 2), (6, 4)):
        jid = submit(alice, [{"upload_id": up["id"]}], nup=nup)
        assert wait_status(svc, jid, {J.COMPLETED})["sheet_count"] == expected


def test_cancel_multi_document_job_and_cleanup(alice, svc):
    a = upload_ok(alice, make_color_pdf(2), "a.pdf")
    b = upload_ok(alice, make_color_pdf(2), "b.pdf")
    docs = [{"upload_id": a["id"]}, {"upload_id": b["id"]}]
    svc.settings.update({"queue_paused": True}, "test")
    jid = submit(alice, docs)
    parts = svc.jobs.parts(svc.jobs.get(jid))
    assert len(parts) == 2 and all((svc.cfg.jobs_dir / p["stored_name"]).exists() for p in parts)
    assert alice.post(f"/api/jobs/{jid}/cancel").get_json()["result"] == "cancelled"
    assert not any((svc.cfg.jobs_dir / p["stored_name"]).exists() for p in parts)
    # in the Windows queue
    svc.settings.update({"queue_paused": False}, "test")
    svc.backend.set_usb(False)
    jid = submit(alice, docs)
    wait_status(svc, jid, {J.WAITING, J.SUBMITTED})
    alice.post(f"/api/jobs/{jid}/cancel")
    wait_status(svc, jid, {J.CANCELLED})
    parts = svc.jobs.parts(svc.jobs.get(jid))
    assert not any((svc.cfg.jobs_dir / p["stored_name"]).exists() for p in parts)
    assert svc.backend.list_jobs() == []


def test_document_limit_and_ownership(alice, bob, svc):
    svc.settings.update({"max_documents_per_job": 2}, "test")
    ups = [upload_ok(alice, make_color_pdf(1), f"{i}.pdf") for i in range(3)]
    r = alice.post("/api/preview", json={"documents": [{"upload_id": u["id"]} for u in ups]})
    assert r.status_code == 400 and r.get_json()["error"] == "err.too_many_documents"
    other = upload_ok(bob, make_color_pdf(1), "bob.pdf")
    r = alice.post("/api/jobs", json={"documents": [{"upload_id": ups[0]["id"]},
                                                    {"upload_id": other["id"]}]})
    assert r.status_code == 404 and r.get_json()["document"] == 1


def test_invalid_selection_reported_per_document(alice, svc):
    a = upload_ok(alice, make_color_pdf(3), "a.pdf")
    b = upload_ok(alice, make_color_pdf(2), "b.pdf")
    r = alice.post("/api/preview", json={"documents": [{"upload_id": a["id"], "page_range": "1-3"},
                                                       {"upload_id": b["id"], "page_range": "3"}]})
    assert r.status_code == 400 and r.get_json()["fields"] == {"page_range.1": "err.page_range"}


def test_thumbnails(alice, bob, svc):
    up = upload_ok(alice, make_color_pdf(3), "a.pdf")
    r = alice.get(f"/api/uploads/{up['id']}/thumb/2")
    assert r.status_code == 200 and r.mimetype == "image/png"
    im = Image.open(__import__("io").BytesIO(r.data)).convert("RGB")
    assert max(im.size) <= 280 and nearest_color(im.getpixel((im.width // 2, im.height // 2))) == 1
    assert alice.get(f"/api/uploads/{up['id']}/thumb/4").status_code == 404
    assert bob.get(f"/api/uploads/{up['id']}/thumb/1").status_code == 404     # not his upload


def test_history_shows_files_pages_and_user(alice, svc):
    a = upload_ok(alice, make_color_pdf(5), "report.pdf")
    b = upload_ok(alice, make_color_pdf(2), "annex.pdf")
    jid = submit(alice, [{"upload_id": a["id"], "page_range": "2-4"}, {"upload_id": b["id"]}],
                 copies=2)
    wait_status(svc, jid, {J.COMPLETED})
    page = alice.get("/history").get_data(as_text=True)
    for text in ("report.pdf", "annex.pdf", "2-4", "alice", "×2"):
        assert text in page
    assert "report.pdf" in alice.get("/history?q=annex").get_data(as_text=True)


def test_old_single_upload_api_still_works(alice, svc):
    r = upload(alice, make_color_pdf(4), "legacy.pdf")
    up = r.get_json()["upload"]
    r = alice.post("/api/jobs", json={"upload_id": up["id"], "options": {"page_range": "3"}})
    jid = r.get_json()["job"]["id"]
    wait_status(svc, jid, {J.COMPLETED})
    assert centre_colors(svc, jid) == [2]
