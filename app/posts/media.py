"""Validation of an uploaded image from its CONTENT.

The browser's declared MIME type is never looked at. The file must (1) have a safe name with an allowed extension,
(2) start with a real PNG or JPEG signature that agrees with the extension, (3) have a structurally sound header with
sane dimensions that Telegram accepts for a photo, and (4) end the way such a file ends. The image is never decoded or
re-encoded here, so there is no image-library attack surface and no decompression-bomb risk. The original file name is
only inspected and then thrown away; it is never used to build a path.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass

from .limits import TELEGRAM_MAX_PHOTO_DIMENSION_SUM, TELEGRAM_MAX_PHOTO_RATIO

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
ALLOWED_EXTENSIONS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
_NAME = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,255}$")
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}

MESSAGES = {
    "empty": "The image file is empty.",
    "too_large": "The image is too large.",
    "bad_name": "The file name is not allowed. Rename the file (letters, digits, dot) and try again.",
    "bad_extension": "Only JPG and PNG images are supported.",
    "bad_content": "The file is not a valid JPG or PNG image.",
    "extension_mismatch": "The file extension does not match the file content.",
    "bad_dimensions": "The image dimensions are not accepted by Telegram for photos.",
}


class MediaError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or MESSAGES.get(code, "The image was rejected.")
        super().__init__(code)


@dataclass(frozen=True)
class ValidatedImage:
    content_type: str
    extension: str
    width: int
    height: int
    size: int
    sha256: str
    data: bytes


def check_filename(name: str) -> str:
    """Return the lower-case extension of a safe file name, or raise MediaError. The name is never used as a path."""
    if not isinstance(name, str) or not _NAME.match(name) or name.startswith(".") or ".." in name or name != name.strip():
        raise MediaError("bad_name")
    if "." not in name:
        raise MediaError("bad_extension")
    extension = name.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise MediaError("bad_extension")
    return extension


def _png_size(data: bytes) -> tuple[int, int]:
    # signature, then the first chunk must be IHDR (13 bytes) with a correct CRC, and the file must end with IEND
    if len(data) < 8 + 25 + 12 or data[12:16] != b"IHDR" or struct.unpack(">I", data[8:12])[0] != 13:
        raise MediaError("bad_content")
    if zlib.crc32(data[12:29]) & 0xFFFFFFFF != struct.unpack(">I", data[29:33])[0]:
        raise MediaError("bad_content")
    if not data.endswith(PNG_IEND):
        raise MediaError("bad_content")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise MediaError("bad_content")
    pos, end = 2, len(data)
    while pos + 4 <= end:
        if data[pos] != 0xFF:
            raise MediaError("bad_content")
        while pos < end and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= end:
            break
        marker = data[pos]
        pos += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
            continue
        if marker in (0xD9, 0xDA):  # image data / end reached without a frame header
            raise MediaError("bad_content")
        if pos + 2 > end:
            break
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if length < 2 or pos + length > end:
            raise MediaError("bad_content")
        if marker in _SOF:
            if length < 8:
                raise MediaError("bad_content")
            height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            return width, height
        pos += length
    raise MediaError("bad_content")


def validate_image(filename: str, data: bytes, max_bytes: int) -> ValidatedImage:
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise MediaError("empty")
    data = bytes(data)
    if len(data) > max_bytes:
        raise MediaError("too_large", f"The image is too large (the limit is {max_bytes // (1024 * 1024)} MB).")
    extension = check_filename(filename)
    if data.startswith(PNG_SIGNATURE):
        content_type, (width, height) = "image/png", _png_size(data)
    elif data.startswith(b"\xff\xd8\xff"):
        content_type, (width, height) = "image/jpeg", _jpeg_size(data)
    else:
        raise MediaError("bad_content")
    if ALLOWED_EXTENSIONS[extension] != content_type:
        raise MediaError("extension_mismatch")
    if width < 1 or height < 1 or width + height > TELEGRAM_MAX_PHOTO_DIMENSION_SUM \
            or max(width, height) / min(width, height) > TELEGRAM_MAX_PHOTO_RATIO:
        raise MediaError("bad_dimensions")
    return ValidatedImage(content_type, "png" if content_type == "image/png" else "jpg", width, height, len(data),
                          hashlib.sha256(data).hexdigest(), data)
