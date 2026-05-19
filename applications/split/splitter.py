"""
Image splitter — pure function, no I/O.

Divides a PNG image into a grid_size x grid_size grid of fragments.
Each fragment is a separate PNG byte buffer.
"""

from __future__ import annotations

import io

from PIL import Image


def split_image(image_bytes: bytes, grid_size: int = 4) -> list[dict]:
    """Split a PNG image into a grid of fragments.

    Args:
        image_bytes: Raw PNG image bytes.
        grid_size: Number of rows and columns (default 4, producing 16 fragments).

    Returns:
        List of 16 dicts, each containing:
            - fragment_id (int, 0-15, row-major)
            - row (int, 0-3)
            - col (int, 0-3)
            - data (bytes, fragment PNG bytes)
            - width (int, fragment pixel width)
            - height (int, fragment pixel height)

    Raises:
        ValueError: If the image can't be decoded or dimensions aren't divisible
                    by grid_size.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        raise ValueError(f"Failed to decode PNG image: {exc}") from exc

    # Convert to grayscale before splitting
    image = image.convert("L")

    width, height = image.size

    if width % grid_size != 0 or height % grid_size != 0:
        raise ValueError(
            f"Image dimensions ({width}x{height}) must be evenly divisible "
            f"by grid_size ({grid_size})"
        )

    frag_w = width // grid_size
    frag_h = height // grid_size

    fragments: list[dict] = []
    for row in range(grid_size):
        for col in range(grid_size):
            left = col * frag_w
            upper = row * frag_h
            right = left + frag_w
            lower = upper + frag_h

            fragment = image.crop((left, upper, right, lower))

            buf = io.BytesIO()
            fragment.save(buf, format="PNG")
            fragment_data = buf.getvalue()

            fragment_id = row * grid_size + col
            fragments.append({
                "fragment_id": fragment_id,
                "row": row,
                "col": col,
                "data": fragment_data,
                "width": frag_w,
                "height": frag_h,
            })

    return fragments
