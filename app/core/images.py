from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

ALLOWED_CONTENT_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class ImageRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check_image(data: bytes, content_type: str | None) -> None:
    """Raise ImageRejected unless `data` fully decodes as the format it claims to be."""
    expected = ALLOWED_CONTENT_TYPES.get((content_type or "").lower())
    if expected is None:
        raise ImageRejected("unsupported_media_type", "Image must be JPEG, PNG or WebP.")
    try:
        with Image.open(io.BytesIO(data)) as im:
            actual = im.format
            im.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageRejected("invalid_image", "The file could not be decoded as an image.") from exc
    if actual != expected:
        raise ImageRejected("invalid_image", "The image content does not match its content type.")
