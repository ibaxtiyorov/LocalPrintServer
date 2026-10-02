import pytest

from lps.layout import (Geometry, PageInfo, estimate_geometry, layout_sheet, output_sequence,
                        plan_job, resolve_orientation, split_cells)

# Spec §13 example: custom 200x250 mm DC geometry measured on the real driver.
SPEC_CUSTOM = Geometry(phys_w=2975, phys_h=3720, off_x=44, off_y=44, horz=2887, vert=3631,
                       dpi_x=378, dpi_y=378)
A4_360 = estimate_geometry(210, 297, 360)


def inside(r, outer, tol=0.51):
    return (r[0] >= outer[0] - tol and r[1] >= outer[1] - tol and r[2] <= outer[2] + tol
            and r[3] <= outer[3] + tol)


def lay(g, pages, **kw):
    kw.setdefault("scaling", "FIT")
    kw.setdefault("margins_mm", 0)
    kw.setdefault("nup", 1)
    kw.setdefault("gap_mm", 4)
    kw.setdefault("auto_rotate", False)
    return layout_sheet(g, list(enumerate(pages)), **kw)


def test_fit_preserves_aspect_and_stays_in_printable():
    sp = lay(A4_360, [PageInfo(100, 50)])
    p = sp.placements[0]
    w, h = p.dest[2] - p.dest[0], p.dest[3] - p.dest[1]
    assert w / h == pytest.approx(2.0, rel=1e-6)
    assert inside(p.dest, A4_360.printable)
    assert p.crop == (0, 0, 1, 1)


def test_fill_covers_cell_and_crops_symmetrically():
    sp = lay(A4_360, [PageInfo(100, 50)], scaling="FILL")
    p = sp.placements[0]
    assert p.dest == pytest.approx(A4_360.printable)
    u0, v0, u1, v1 = p.crop
    assert v0 == pytest.approx(0) and v1 == pytest.approx(1)
    assert u0 == pytest.approx(1 - u1)          # symmetric horizontal crop
    # never stretched: visible source aspect equals destination aspect
    src_aspect = (u1 - u0) * 100 / 50
    dw, dh = p.dest[2] - p.dest[0], p.dest[3] - p.dest[1]
    assert src_aspect == pytest.approx(dw / dh, rel=1e-6)


def test_actual_size_100mm_is_100mm():
    g = SPEC_CUSTOM
    sp = lay(g, [PageInfo(100, 100)], scaling="ACTUAL")
    p = sp.placements[0]
    mm_w = (p.dest[2] - p.dest[0]) / g.dpi_x * 25.4
    mm_h = (p.dest[3] - p.dest[1]) / g.dpi_y * 25.4
    assert mm_w == pytest.approx(100, abs=0.1) and mm_h == pytest.approx(100, abs=0.1)
    assert not p.clipped


def test_actual_page_same_as_paper_maps_one_to_one_and_clips_to_printable():
    g = A4_360
    sp = lay(g, [PageInfo(210, 297)], scaling="ACTUAL")
    p = sp.placements[0]
    assert p.clipped
    assert p.dest == pytest.approx(g.printable)
    # crop removes exactly the non-printable margin (3 mm of 210 mm)
    assert p.crop[0] == pytest.approx(g.off_x / g.phys_w, abs=1e-3)


def test_margins_shrink_content():
    sp = lay(A4_360, [PageInfo(210, 297)], margins_mm=10)
    m = A4_360.mm_to_px_x(10)
    assert sp.content[0] == pytest.approx(m)
    assert inside(sp.placements[0].dest, sp.content)


def test_margins_too_large_raise():
    with pytest.raises(ValueError):
        lay(estimate_geometry(89, 127, 360), [PageInfo(10, 10)], margins_mm=50)


@pytest.mark.parametrize("nup,count", [(2, 2), (4, 4)])
def test_nup_cells_do_not_overlap(nup, count):
    cells = split_cells((0, 0, 1000, 1400), nup, 20, 20)
    assert len(cells) == count
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            assert overlap == 0


def test_nup_order_left_to_right_top_to_bottom():
    cells = split_cells((0, 0, 1000, 1000), 4, 0, 0)
    assert [c[:2] for c in cells] == [(0, 0), (500, 0), (0, 500), (500, 500)]


def test_nup_two_on_landscape_side_by_side():
    g = estimate_geometry(297, 210, 360)
    sp = lay(g, [PageInfo(210, 297), PageInfo(210, 297)], nup=2)
    a, b = sp.placements
    assert a.dest[2] <= b.dest[0]        # page 1 left of page 2


def test_mirror_flips_horizontally_and_stays_printable():
    g = Geometry(1000, 1400, 30, 20, 900, 1300, 100, 100)   # asymmetric margins
    plain = lay(g, [PageInfo(50, 50)], scaling="ACTUAL")
    mir = lay(g, [PageInfo(50, 50)], scaling="ACTUAL", mirror=True)
    p, m = plain.placements[0], mir.placements[0]
    assert m.flip_h and not m.flip_v
    assert m.dest[0] == pytest.approx(g.phys_w - p.dest[2])
    assert inside(m.dest, g.printable)


def test_rotate180_is_both_flips_and_stays_printable():
    g = Geometry(1000, 1400, 30, 20, 900, 1300, 100, 100)
    sp = lay(g, [PageInfo(210, 297)], scaling="FILL", rotate180=True)
    p = sp.placements[0]
    assert p.flip_h and p.flip_v
    assert inside(p.dest, g.printable)


def test_mirror_plus_rotate_is_vertical_flip():
    g = A4_360
    p = lay(g, [PageInfo(10, 10)], scaling="ACTUAL", mirror=True, rotate180=True).placements[0]
    assert p.flip_v and not p.flip_h


def test_auto_rotate_landscape_page_on_portrait_cell():
    p = lay(A4_360, [PageInfo(297, 210)], auto_rotate=True).placements[0]
    assert p.rotate == 90
    assert (p.dest[3] - p.dest[1]) > (p.dest[2] - p.dest[0])


def test_resolve_orientation():
    assert resolve_orientation("AUTO", PageInfo(297, 210), 210, 297, 1) == "LANDSCAPE"
    assert resolve_orientation("AUTO", PageInfo(210, 297), 210, 297, 1) == "PORTRAIT"
    assert resolve_orientation("AUTO", PageInfo(210, 297), 210, 297, 2) == "LANDSCAPE"
    assert resolve_orientation("PORTRAIT", PageInfo(297, 210), 210, 297, 1) == "PORTRAIT"


def test_plan_reverse_and_nup_grouping():
    plan = plan_job([0, 1, 2, 3, 4], 2, True, "PORTRAIT")
    assert plan.sheets == [[4], [2, 3], [0, 1]]


def test_output_sequence_collate():
    assert output_sequence(2, 2, True) == [0, 1, 0, 1]      # matches spec §21 physical test
    assert output_sequence(2, 2, False) == [0, 0, 1, 1]


def test_borderless_estimate_has_zero_offsets():
    g = estimate_geometry(210, 297, 720, borderless=True)
    assert (g.off_x, g.off_y) == (0, 0) and g.horz == g.phys_w
    # spec §14: ≈6026 x 8523 at 720 dpi
    assert g.phys_w == pytest.approx(6026, abs=15) and g.phys_h == pytest.approx(8523, abs=15)
