"""
Image joiner — pure function, no I/O.

Reassembles a grid of grayscale PNG fragments into a single
edge-map image. All fragments must be the same size.
"""

from __future__ import annotations

import io

from PIL import Image


def reassemble_image(
    fragments: list[tuple[int, bytes]],
    total_width: int,
    total_height: int,
    grid_size: int = 4,
) -> bytes:
    """Reassemble a grid of PNG fragments into a single image.

    Args:
        fragments: List of (fragment_id, fragment_png_bytes) tuples.
                   Must contain exactly grid_size**2 entries with ids 0..N-1.
        total_width: Width of the final assembled image in pixels.
        total_height: Height of the final assembled image in pixels.
        grid_size: Grid dimension (default 4, producing 4x4 = 16 fragments).

    Returns:
        Raw PNG bytes of the reassembled image.

    Raises:
        ValueError: If fragment count doesn't match grid_size**2,
                    or if fragment ids don't cover 0..N-1.
    """
    expected_count = grid_size ** 2
    if len(fragments) != expected_count:
        raise ValueError(
            f"Expected {expected_count} fragments, got {len(fragments)}"
        )

    frag_w = total_width // grid_size
    frag_h = total_height // grid_size

    # Sort by fragment_id and verify completeness
    fragments.sort(key=lambda x: x[0])
    ids = [f[0] for f in fragments]
    expected_ids = list(range(expected_count))
    if ids != expected_ids:
        raise ValueError(
            f"Fragment ids {ids} don't cover {expected_ids}"
        )

    # Create blank canvas
    assembled = Image.new("L", (total_width, total_height))

    for fragment_id, png_bytes in fragments:
        row = fragment_id // grid_size
        col = fragment_id % grid_size

        # Decode fragment
        frag_image = Image.open(io.BytesIO(png_bytes))
        frag_image = frag_image.convert("L")

        # Paste into position
        left = col * frag_w
        upper = row * frag_h
        assembled.paste(frag_image, (left, upper))

    # Serialize to PNG bytes
    buf = io.BytesIO()
    assembled.save(buf, format="PNG")
    return buf.getvalue()
