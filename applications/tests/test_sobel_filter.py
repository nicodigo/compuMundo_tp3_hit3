"""
Unit tests for the Sobel filter function.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from ..worker.sobel_filter import apply_sobel


def test_apply_sobel_black_square():
    """A solid black image should produce near-zero edges (no gradient)."""
    img = Image.new("L", (64, 64), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    result = apply_sobel(buf.getvalue())

    result_img = Image.open(io.BytesIO(result))
    arr = np.array(result_img)
    assert arr.max() < 10, f"Expected near-zero gradient in solid black, got max={arr.max()}"


def test_apply_sobel_produces_valid_png():
    """Output must be decodable as PNG."""
    img = Image.new("L", (128, 128), 128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    result = apply_sobel(buf.getvalue())
    result_img = Image.open(io.BytesIO(result))
    assert result_img.size == (128, 128)
    assert result_img.mode == "L"


def test_apply_sobel_preserves_dimensions():
    """Output image must have the same dimensions as input."""
    for size in [(128, 128), (256, 64), (100, 100)]:
        img = Image.new("L", size, 128)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = apply_sobel(buf.getvalue())
        result_img = Image.open(io.BytesIO(result))
        assert result_img.size == size, f"Size mismatch for {size}"
