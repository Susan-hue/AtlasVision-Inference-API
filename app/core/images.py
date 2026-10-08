from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

ALLOWED_CONTENT_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class ImageRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_image(data: bytes, content_type: str | None) -> None:
    """Raise ImageRejected unless `data` fully decodes as the format it claims to be."""
    expected = ALLOWED_CONTENT_TYPES.get((content_type or "").lower())
    if expected is None:
        raise ImageRejected("unsupported_media_type")
    try:
        with Image.open(io.BytesIO(data)) as im:
            actual = im.format
            im.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageRejected("invalid_image") from exc
    if actual != expected:
        raise ImageRejected("invalid_image")
