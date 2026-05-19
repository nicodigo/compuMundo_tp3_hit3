"""
Unit tests for the image joiner (reassemble) function.
"""

from __future__ import annotations

import io

from PIL import Image

from ..split.splitter import split_image
from ..joiner.joiner import reassemble_image


def _make_test_fragments(
    width: int = 128, height: int = 128, grid: int = 4
) -> list[tuple[int, bytes]]:
    img = Image.new("L", (width, height), 100)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    fragments = split_image(buf.getvalue(), grid_size=grid)
    return [(f["fragment_id"], f["data"]) for f in fragments]


def test_reassemble_produces_correct_dimensions():
    """Reassembled image must match original dimensions."""
    fragments = _make_test_fragments(128, 128, grid=4)
    result = reassemble_image(fragments, 128, 128, grid_size=4)
    result_img = Image.open(io.BytesIO(result))
    assert result_img.size == (128, 128)
    assert result_img.mode == "L"


def test_reassemble_with_full_set():
    """All 16 fragments present and correctly ordered."""
    fragments = _make_test_fragments(128, 128, grid=4)
    result = reassemble_image(fragments, 128, 128, grid_size=4)
    result_img = Image.open(io.BytesIO(result))
    assert result_img is not None
    assert result_img.size == (128, 128)


def test_reassemble_requires_exact_count():
    """Missing fragments must raise ValueError."""
    fragments = _make_test_fragments(128, 128, grid=4)
    fragments.pop()  # remove one
    try:
        reassemble_image(fragments, 128, 128, grid_size=4)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_reassemble_wrong_ids():
    """Fragment ids not covering 0-15 must raise ValueError."""
    fragments = _make_test_fragments(128, 128, grid=4)
    # Replace one with a wrong id
    fragments[0] = (99, fragments[0][1])
    try:
        reassemble_image(fragments, 128, 128, grid_size=4)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
