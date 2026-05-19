"""
Unit tests for the image splitter function.
"""

from __future__ import annotations

import io

from PIL import Image

from ..split.splitter import split_image


def _make_test_image(width: int = 128, height: int = 128) -> bytes:
    img = Image.new("L", (width, height), 100)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_split_4x4_produces_16_fragments():
    """A 128x128 image with grid_size=4 must produce exactly 16 fragments."""
    fragments = split_image(_make_test_image(), grid_size=4)
    assert len(fragments) == 16


def test_fragment_ids_are_0_to_15_row_major():
    """Fragment IDs must be 0-15 in row-major order."""
    fragments = split_image(_make_test_image(), grid_size=4)
    ids = [f["fragment_id"] for f in fragments]
    assert ids == list(range(16))


def test_fragment_coordinates():
    """Fragment at (row=2, col=1) must have fragment_id = 2*4 + 1 = 9."""
    fragments = split_image(_make_test_image(), grid_size=4)
    frag_9 = fragments[9]
    assert frag_9["fragment_id"] == 9
    assert frag_9["row"] == 2
    assert frag_9["col"] == 1


def test_fragment_dimensions():
    """Each fragment must be (width/grid, height/grid) pixels."""
    fragments = split_image(_make_test_image(128, 128), grid_size=4)
    for f in fragments:
        assert f["width"] == 32
        assert f["height"] == 32


def test_fragments_are_valid_png():
    """Each fragment's data must be decodable as PNG."""
    fragments = split_image(_make_test_image(128, 128), grid_size=4)
    for f in fragments:
        frag_img = Image.open(io.BytesIO(f["data"]))
        assert frag_img.size == (f["width"], f["height"])
        assert frag_img.mode == "L"


def test_rejects_non_divisible_dimensions():
    """A 100x100 image with grid=4 must raise ValueError."""
    data = _make_test_image(100, 100)
    try:
        split_image(data, grid_size=4)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
