"""Application-side N-up composition: 1, 2, 4, 6, 9 and 16 pages per sheet."""
import pytest

from lps.layout import (PageInfo, estimate_geometry, grid_for, layout_sheet, plan_job,
                        resolve_orientation, split_cells)

A4P = estimate_geometry(210, 297, 150)        # portrait
A4L = estimate_geometry(297, 210, 150)        # landscape


@pytest.mark.parametrize("nup,landscape_grid", [(1, (1, 1)), (2, (2, 1)), (4, (2, 2)), (6, (3, 2)),
                                                (9, (3, 3)), (16, (4, 4))])
def test_grid_puts_more_cells_along_the_long_side(nup, landscape_grid):
    assert grid_for(nup, 300, 200) == landscape_grid
    assert grid_for(nup, 200, 300) == landscape_grid[::-1]


@pytest.mark.parametrize("nup", [2, 4, 6, 9, 16])
def test_cells_count_order_and_no_overlap(nup):
    cells = split_cells((0, 0, 900, 1200), nup, 10, 10)
    assert len(cells) == nup
    # reading order: rows top-to-bottom, each row left-to-right
    keys = [(round(c[1]), round(c[0])) for c in cells]
    assert keys == sorted(keys)
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            assert ox <= 1e-6 or oy <= 1e-6
        assert a[0] >= 0 and a[1] >= 0 and a[2] <= 900 and a[3] <= 1200


@pytest.mark.parametrize("nup", [2, 4, 6, 9, 16])
def test_every_page_placed_inside_its_cell_and_printable_area(nup):
    pages = [(i, PageInfo(210, 297)) for i in range(nup)]
    for g in (A4P, A4L):
        sp = layout_sheet(g, pages, scaling="FIT", margins_mm=5, nup=nup, gap_mm=3, auto_rotate=True)
        assert len(sp.placements) == nup
        assert [p.page_index for p in sp.placements] == list(range(nup))
        px = g.printable
        for p, cell in zip(sp.placements, sp.cells):
            d = p.dest
            assert d[0] >= cell[0] - 0.5 and d[2] <= cell[2] + 0.5
            assert d[0] >= px[0] - 0.5 and d[3] <= px[3] + 0.5


@pytest.mark.parametrize("nup,expected", [(6, "LANDSCAPE"), (9, "PORTRAIT"), (16, "PORTRAIT"),
                                          (2, "LANDSCAPE"), (4, "PORTRAIT")])
def test_auto_orientation_for_portrait_pages(nup, expected):
    assert resolve_orientation("AUTO", PageInfo(210, 297), 210, 297, nup) == expected


@pytest.mark.parametrize("nup,pages,sheets", [(6, 13, 3), (9, 9, 1), (9, 10, 2), (16, 33, 3)])
def test_page_grouping_into_sheets(nup, pages, sheets):
    plan = plan_job(list(range(pages)), nup, False, "PORTRAIT")
    assert len(plan.sheets) == sheets
    assert [p for s in plan.sheets for p in s] == list(range(pages))


def test_reverse_keeps_reading_order_within_a_sheet():
    plan = plan_job(list(range(10)), 4, True, "PORTRAIT")
    assert plan.sheets == [[8, 9], [4, 5, 6, 7], [0, 1, 2, 3]]
