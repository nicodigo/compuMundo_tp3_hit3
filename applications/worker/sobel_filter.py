"""
Sobel edge detection filter — pure function.

Applies the Sobel operator to a grayscale PNG image and returns
the edge-map magnitude as a PNG byte buffer.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image
from scipy.ndimage import sobel


def apply_sobel(image_bytes: bytes) -> bytes:
    """Apply Sobel edge detection to a grayscale PNG image.

    Args:
        image_bytes: Raw PNG bytes of a grayscale fragment.

    Returns:
        Raw PNG bytes of the Sobel edge-map (grayscale, 0-255).

    Process:
        1. Decode PNG to grayscale numpy array.
        2. Compute gradient in x direction (horizontal edges).
        3. Compute gradient in y direction (vertical edges).
        4. Compute magnitude = sqrt(gx^2 + gy^2).
        5. Normalize to 0-255 (clip, convert to uint8).
        6. Encode back to PNG.
    """
    # Decode PNG to grayscale array
    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    arr = np.array(image, dtype=np.float64)

    # Sobel gradients
    grad_x = sobel(arr, axis=0)  # horizontal edges
    grad_y = sobel(arr, axis=1)  # vertical edges

    # Magnitude
    magnitude = np.hypot(grad_x, grad_y)

    # Normalize to 0-255
    magnitude = np.clip(magnitude, 0, 255).astype(np.uint8)

    # Encode back to PNG
    result_image = Image.fromarray(magnitude, mode="L")
    buf = io.BytesIO()
    result_image.save(buf, format="PNG")
    return buf.getvalue()
